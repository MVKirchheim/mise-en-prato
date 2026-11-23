#!/usr/bin/env python3
"""Leitor de encartes: a imagem entra, as ofertas saem como LeituraEncarte.

O leitor é uma interface (`Leitor`) para dar para trocar de modelo sem mexer no
laço: se o plano gratuito do Gemini acabar ou piorar, entra outro leitor com o
mesmo `ler`. Nos testes entra um leitor falso.

O `LeitorGemini` fala com a API REST do Gemini por httpx, sem SDK. A chave vai
no cabeçalho `x-goog-api-key`, nunca na URL: o httpx registra a URL no log e a
põe nas mensagens de erro, e a chave não pode aparecer em nenhum dos dois.

Os erros saem em três exceções, porque o laço decide a próxima tentativa pelo
tipo: `ErroTemporario` (tenta de novo mais tarde), `ErroPermanente` (desiste na
hora) e `RespostaInvalida` (o modelo respondeu, mas não o que se pediu). As
mensagens são curtas e nunca carregam a chave nem o conteúdo da resposta.
"""

import base64
import json
import math
from datetime import date
from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, ValidationError, field_validator

from .config import recusar_no_demo

API = "https://generativelanguage.googleapis.com/v1beta/models/{modelo}:generateContent"
TEMPO_LIMITE = 90.0  # segundos; encarte grande no plano gratuito leva uns 20–40 s

STATUS_TEMPORARIOS = {408, 429, 500, 502, 503, 504}
# Acima disto não é preço de supermercado, é leitura errada (ou 1e30, que nem
# cabe no INTEGER do sqlite): vira None, como o preço ilegível.
PRECO_ABSURDO = 1_000_000.0

PROMPT = """\
Você recebe a imagem de um encarte de ofertas de um supermercado brasileiro.
Extraia TODAS as ofertas e responda SOMENTE com um JSON válido, sem texto antes ou depois, \
neste formato:

{
  "mercado": "nome do mercado",
  "validade_inicio": "AAAA-MM-DD ou null",
  "validade_fim": "AAAA-MM-DD ou null",
  "observacoes": "inconsistências ou avisos relevantes do encarte, ou null",
  "ofertas": [
    {
      "produto": "nome do produto como aparece no encarte, incluindo a marca",
      "preco_normal": 0.00,
      "preco_clube": 0.00,
      "unidade": "kg ou un",
      "embalagem": "peso/volume/quantidade da embalagem, ou null",
      "condicao": "condição para o preço valer, ou null"
    }
  ]
}

Regras:
- Muitos encartes têm dois preços por produto: o preço normal (ex.: "Sem Clube", "Varejo") e o \
preço de programa de fidelidade (ex.: "Com Clube", "Clube Max", "ative oferta no app"). O preço \
normal é sempre o MAIOR dos dois. Se houver um só preço, use preco_normal e deixe preco_clube \
como null.
- Os centavos costumam aparecer em tamanho menor ou sobrescrito: "R$ 59,⁹⁰" significa 59.90.
- Use ponto como separador decimal nos números do JSON.
- unidade é "kg" quando o preço é por quilo, e "un" quando é por unidade/embalagem.
- condicao é a condição para o preço valer, como aparece no encarte (ex.: "leve 3 pague 2", \
"limite 5 por cliente", "ative a oferta no app"). Se não houver condição, use null.
- Se o encarte se contradisser (ex.: datas diferentes no topo e no rodapé, preço repetido com \
valores diferentes), registre a divergência em observacoes.
- Se houver datas divergentes, use em validade_inicio e validade_fim a data mais destacada \
(a do título ou do topo do encarte), não a do texto miúdo do rodapé.
- Não invente ofertas nem preços. Se não conseguir ler um valor, use null.
"""

PROMPT_LEGENDA = """\
A mensagem que trouxe este encarte tinha a legenda abaixo, entre as linhas de traços. Use-a só \
como contexto (ela pode trazer a validade ou a loja). Se a legenda e o encarte discordarem, vale \
o encarte, e a divergência vai em observacoes.
---
{legenda}
---
"""


def _anulavel(tipo: str, **extra: Any) -> dict:
    return {"type": tipo, "nullable": True, **extra}


# Subconjunto OpenAPI que o Gemini aceita em responseSchema.
ESQUEMA = {
    "type": "OBJECT",
    "properties": {
        "mercado": _anulavel("STRING"),
        "validade_inicio": _anulavel("STRING", description="AAAA-MM-DD"),
        "validade_fim": _anulavel("STRING", description="AAAA-MM-DD"),
        "observacoes": _anulavel("STRING"),
        "ofertas": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "produto": _anulavel("STRING"),
                    "preco_normal": _anulavel("NUMBER"),
                    "preco_clube": _anulavel("NUMBER"),
                    "unidade": _anulavel("STRING", enum=["kg", "un"]),
                    "embalagem": _anulavel("STRING"),
                    "condicao": _anulavel("STRING"),
                },
                "required": ["produto", "preco_normal", "preco_clube", "unidade"],
                "propertyOrdering": [
                    "produto",
                    "preco_normal",
                    "preco_clube",
                    "unidade",
                    "embalagem",
                    "condicao",
                ],
            },
        },
    },
    "required": ["mercado", "validade_inicio", "validade_fim", "observacoes", "ofertas"],
    "propertyOrdering": ["mercado", "validade_inicio", "validade_fim", "observacoes", "ofertas"],
}


# ── erros ────────────────────────────────────────────────────────────────────
class ErroLeitor(Exception):
    """Base dos erros do leitor. A mensagem é curta e segura para o log."""


class ErroTemporario(ErroLeitor):
    """Vale tentar de novo mais tarde: limite de uso, servidor caído, rede, timeout."""


class ErroPermanente(ErroLeitor):
    """Tentar de novo não muda nada: chave inválida, pedido recusado, imagem bloqueada."""


class RespostaInvalida(ErroLeitor):
    """O modelo respondeu, mas com texto que não é JSON ou não tem o formato pedido."""


# ── o que o leitor devolve ───────────────────────────────────────────────────
def _texto_ou_nada(valor: Any) -> Any:
    if isinstance(valor, str):
        return valor.strip() or None
    return valor


class OfertaLida(BaseModel):
    produto: str | None = None
    preco_normal: float | None = None
    preco_clube: float | None = None
    unidade: Literal["kg", "un"] | None = None
    embalagem: str | None = None
    condicao: str | None = None

    @field_validator("produto", "embalagem", "condicao", mode="before")
    @classmethod
    def _textos(cls, valor: Any) -> Any:
        return _texto_ou_nada(valor)

    @field_validator("preco_normal", "preco_clube", mode="before")
    @classmethod
    def _preco(cls, valor: Any) -> float | None:
        """Número, ou texto com vírgula ("59,90"). O resto vira None, não erro.

        Não finito ("NaN", "Infinity"), negativo ou absurdo também vira None:
        passaria pelo float e quebraria a conversão para centavos lá na frente.
        """
        if isinstance(valor, bool):
            return None
        if isinstance(valor, int | float):
            try:
                numero = float(valor)
            except OverflowError:  # inteiro grande demais até para float
                return None
        elif isinstance(valor, str):
            texto = valor.replace("R$", "").strip().replace(",", ".")
            try:
                numero = float(texto)
            except ValueError:
                return None
        else:
            return None
        if not math.isfinite(numero) or numero < 0 or numero > PRECO_ABSURDO:
            return None
        return numero

    @field_validator("unidade", mode="before")
    @classmethod
    def _unidade(cls, valor: Any) -> str | None:
        if isinstance(valor, str) and valor.strip().lower() in ("kg", "un"):
            return valor.strip().lower()
        return None


class LeituraEncarte(BaseModel):
    mercado: str | None = None
    validade_inicio: date | None = None
    validade_fim: date | None = None
    observacoes: str | None = None
    ofertas: list[OfertaLida] = []

    @field_validator("mercado", "observacoes", mode="before")
    @classmethod
    def _textos(cls, valor: Any) -> Any:
        return _texto_ou_nada(valor)

    @field_validator("validade_inicio", "validade_fim", mode="before")
    @classmethod
    def _data(cls, valor: Any) -> date | None:
        """Data fora do AAAA-MM-DD vira None: uma data torta não derruba a leitura."""
        if isinstance(valor, str):
            try:
                return date.fromisoformat(valor.strip())
            except ValueError:
                return None
        return valor if isinstance(valor, date) else None

    @field_validator("ofertas", mode="before")
    @classmethod
    def _lista(cls, valor: Any) -> Any:
        return [] if valor is None else valor


class Leitor(Protocol):
    nome: str

    async def ler(self, imagem: bytes, mime: str, legenda: str | None) -> LeituraEncarte: ...


# ── Gemini ───────────────────────────────────────────────────────────────────
def interpretar_resposta(corpo: Any) -> LeituraEncarte:
    """O corpo JSON do generateContent vira LeituraEncarte, ou a exceção certa."""
    if not isinstance(corpo, dict):
        raise RespostaInvalida("corpo da resposta sem JSON")
    feedback = corpo.get("promptFeedback")
    if isinstance(feedback, dict) and feedback.get("blockReason"):
        raise ErroPermanente(f"pedido bloqueado ({feedback['blockReason']})")
    candidatos = corpo.get("candidates")
    if not isinstance(candidatos, list) or not candidatos or not isinstance(candidatos[0], dict):
        raise ErroPermanente("resposta sem candidato")
    candidato = candidatos[0]
    motivo = candidato.get("finishReason")
    if motivo != "STOP":
        raise ErroPermanente(f"resposta interrompida ({motivo})")
    conteudo = candidato.get("content")
    partes = conteudo.get("parts") if isinstance(conteudo, dict) else None
    texto = "".join(
        p["text"]
        for p in (partes if isinstance(partes, list) else [])
        if isinstance(p, dict) and isinstance(p.get("text"), str) and not p.get("thought")
    )
    if not texto.strip():
        raise ErroPermanente("resposta sem texto")
    try:
        dados = json.loads(texto)
    except ValueError as erro:
        raise RespostaInvalida("JSON inválido") from erro
    try:
        return LeituraEncarte.model_validate(dados)
    except ValidationError as erro:
        # Só a contagem: a mensagem do pydantic cita os valores lidos.
        raise RespostaInvalida(f"formato inesperado ({erro.error_count()} erros)") from None


class LeitorGemini:
    """Leitor pela API do Gemini. `nome` é o modelo, que vai para encarte.leitor."""

    def __init__(self, chave: str, modelo: str, cliente: httpx.AsyncClient | None = None):
        self._chave = chave
        self.nome = modelo
        self._cliente = cliente

    def __repr__(self) -> str:  # sem a chave, nem por acidente
        return f"LeitorGemini(modelo={self.nome!r})"

    def _corpo(self, imagem: bytes, mime: str, legenda: str | None) -> dict:
        partes: list[dict] = [
            {"text": PROMPT},
            {"inline_data": {"mime_type": mime, "data": base64.b64encode(imagem).decode()}},
        ]
        if legenda:
            partes.append({"text": PROMPT_LEGENDA.format(legenda=legenda)})
        return {
            "contents": [{"role": "user", "parts": partes}],
            "generationConfig": {
                "temperature": 0,
                "responseMimeType": "application/json",
                "responseSchema": ESQUEMA,
            },
        }

    async def pedir(self, imagem: bytes, mime: str, legenda: str | None) -> Any:
        """Faz a chamada e devolve o corpo JSON cru. Erros de HTTP e rede já traduzidos."""
        recusar_no_demo("Gemini")
        cliente = self._cliente or httpx.AsyncClient(timeout=TEMPO_LIMITE)
        try:
            resposta = await cliente.post(
                API.format(modelo=self.nome),
                headers={"x-goog-api-key": self._chave},
                json=self._corpo(imagem, mime, legenda),
                timeout=TEMPO_LIMITE,
            )
        except httpx.TimeoutException:
            raise ErroTemporario("timeout") from None
        except httpx.TransportError as erro:
            raise ErroTemporario(f"erro de rede ({type(erro).__name__})") from None
        finally:
            if self._cliente is None:
                await cliente.aclose()

        if resposta.status_code in STATUS_TEMPORARIOS:
            raise ErroTemporario(f"HTTP {resposta.status_code}")
        if not resposta.is_success:
            raise ErroPermanente(f"HTTP {resposta.status_code}")
        try:
            return resposta.json()
        except ValueError:
            raise RespostaInvalida("corpo da resposta sem JSON") from None

    async def ler(self, imagem: bytes, mime: str, legenda: str | None) -> LeituraEncarte:
        return interpretar_resposta(await self.pedir(imagem, mime, legenda))
