#!/usr/bin/env python3
"""Encartes de mercado: a porta de entrada das imagens que vêm do WhatsApp.

A Evolution API manda para o webhook TODA mensagem que o número recebe,
inclusive as pessoais. Este módulo é o filtro: só imagem de comunidade
cadastrada como fonte vira arquivo em disco e linha `pendente` no banco. O
resto é descartado sem gravar nada — nem no banco, nem no log. A única
exceção é o modo descoberta: grupo desconhecido que manda imagem deixa o jid,
as datas e um contador em `encarte_grupo_visto`, para dar para cadastrar a
comunidade nova sem ligar log de conversa.

Ler a imagem fica para o laço de fundo (`processar_proximo`, chamado pelo
laço em tarefas.py); o webhook não chama o leitor e responde em
milissegundos. A limpeza das imagens com mais de 7 dias também mora aqui.

Cada descarte devolve um código curto, que vai para o log e para os testes.
Nenhum descarte é erro: a Evolution só reenvia o que falhou com 5xx, e uma
mensagem pessoal reenviada dez vezes seria dez vezes pior.

    python -m mise_en_prato.encartes fontes
    python -m mise_en_prato.encartes adicionar 120363...@g.us "LAR Centro" --cnpj ...
    python -m mise_en_prato.encartes desativar 2
    python -m mise_en_prato.encartes grupos
    python -m mise_en_prato.encartes importar 1 /dados/importar/foto.jpg --recebido-em 2026-11-24
    python -m mise_en_prato.encartes reler 42
"""

import argparse
import base64
import binascii
import hashlib
import io
import json
import logging
import os
import sqlite3
import tempfile
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from PIL import Image

from .banco import centavos
from .leitor_encartes import (
    ErroPermanente,
    ErroTemporario,
    Leitor,
    LeituraEncarte,
    RespostaInvalida,
)

log = logging.getLogger("mise_en_prato.encartes")

FUSO = ZoneInfo("America/Sao_Paulo")

# Encarte de verdade tem 1–2 MB; 10 MB já é folga larga. Acima disso é outra
# coisa (vídeo renomeado, arte em resolução de gráfica) e não vale o disco.
LIMITE_IMAGEM = 10 * 1024 * 1024

# O formato vem do que o Pillow reconhece nos bytes, nunca do mimetype que a
# mensagem declara: quem decide a extensão do arquivo em disco é o conteúdo.
FORMATOS = {
    "JPEG": ("image/jpeg", "jpg"),
    "PNG": ("image/png", "png"),
    "WEBP": ("image/webp", "webp"),
}

SUFIXO_GRUPO = "@g.us"

# ── códigos de resultado ─────────────────────────────────────────────────────
GUARDADO = "guardado"
SEM_MIDIA = "sem_midia"
REPETIDO = "repetido"
GRUPO_DESCONHECIDO = "grupo_desconhecido"
IGNORADO_EVENTO = "ignorado_evento"
IGNORADO_PROPRIO = "ignorado_proprio"
IGNORADO_NAO_GRUPO = "ignorado_nao_grupo"
IGNORADO_TIPO = "ignorado_tipo"
IMAGEM_INVALIDA = "imagem_invalida"
GRANDE_DEMAIS = "grande_demais"
MALFORMADO = "malformado"


def _iso(instante: datetime) -> str:
    return instante.astimezone(FUSO).isoformat(timespec="seconds")


def _instante(valor: Any, agora: datetime) -> datetime:
    """O messageTimestamp (segundos) no fuso da casa; sem ele, a hora da chegada.

    Vem como número, às vezes como texto, e o Baileys já mandou objeto Long.
    Nada disso pode derrubar o recebimento: na dúvida, vale a hora de agora.
    """
    if isinstance(valor, bool):
        return agora.astimezone(FUSO)
    try:
        segundos = float(valor)
        return datetime.fromtimestamp(segundos, FUSO)
    except (TypeError, ValueError, OverflowError, OSError):
        return agora.astimezone(FUSO)


def _formato(bruto: bytes) -> tuple[str, str] | None:
    """(mime, extensão) se os bytes forem mesmo JPEG, PNG ou WebP.

    `formats` limita os decodificadores que o Pillow sequer tenta: o arquivo
    vem de fora, e não há por que expor o parser de TIFF ou de PSD a ele. O
    `verify` confere a estrutura sem decodificar a imagem inteira.
    """
    try:
        with Image.open(io.BytesIO(bruto), formats=list(FORMATOS)) as imagem:
            formato = imagem.format
            imagem.verify()
    except Exception:
        return None
    return FORMATOS.get(formato or "")


def _fonte_ativa(con: sqlite3.Connection, jid: str) -> sqlite3.Row | None:
    return con.execute(
        "SELECT id, nome FROM encarte_fonte WHERE grupo_jid = ? AND ativo = 1", (jid,)
    ).fetchone()


def _registrar_grupo_visto(con: sqlite3.Connection, jid: str, agora: datetime) -> None:
    quando = _iso(agora)
    con.execute(
        """INSERT INTO encarte_grupo_visto (grupo_jid, primeira_vez, ultima_vez, imagens)
           VALUES (?, ?, ?, 1)
           ON CONFLICT(grupo_jid) DO UPDATE SET ultima_vez = excluded.ultima_vez,
                                                imagens = imagens + 1""",
        (jid, quando, quando),
    )
    con.commit()


def _inserir_encarte(con: sqlite3.Connection, **campos: Any) -> int:
    colunas = ", ".join(campos)
    marcas = ", ".join("?" for _ in campos)
    cursor = con.execute(
        f"INSERT INTO encarte ({colunas}) VALUES ({marcas})", tuple(campos.values())
    )
    return cursor.lastrowid


def _gravar_arquivo(alvo: Path, bruto: bytes) -> None:
    """Grava num temporário da mesma pasta e renomeia: ninguém vê arquivo pela metade."""
    alvo.parent.mkdir(parents=True, exist_ok=True)
    descritor, temporario = tempfile.mkstemp(dir=alvo.parent, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(descritor, "wb") as saida:
            saida.write(bruto)
        os.replace(temporario, alvo)
    except BaseException:
        Path(temporario).unlink(missing_ok=True)
        raise


def processar_webhook(con: sqlite3.Connection, payload: Any, pasta: Path, agora: datetime) -> str:
    """Decide o destino de um `messages.upsert` e devolve o código do resultado.

    `pasta` é a pasta de encartes dentro de dados (config().pasta_encartes); o
    caminho gravado no banco é relativo à pasta-mãe dela, os dados.

    Para no primeiro "não", e a ordem importa: tudo o que não é de fonte
    cadastrada sai antes de qualquer coisa ser lida além do jid.

    Erro de banco ou de disco sobe de propósito: vira 5xx, e a Evolution
    reenvia. Payload torto não é erro dela que valha reenviar: vira MALFORMADO.
    """
    codigo, fonte, encarte_id = _processar(con, payload, pasta, agora)
    # Só fonte cadastrada aparece no log com nome. De resto, só o código: nada
    # de jid, nome de contato, legenda ou telefone de conversa pessoal.
    if fonte is not None and encarte_id is not None:
        log.info("webhook de encartes: %s (%s, encarte %d)", codigo, fonte, encarte_id)
    elif fonte is not None:
        log.info("webhook de encartes: %s (%s)", codigo, fonte)
    else:
        log.info("webhook de encartes: %s", codigo)
    return codigo


def _processar(
    con: sqlite3.Connection, payload: Any, pasta: Path, agora: datetime
) -> tuple[str, str | None, int | None]:
    if not isinstance(payload, dict):
        return MALFORMADO, None, None
    if payload.get("event") != "messages.upsert":
        return IGNORADO_EVENTO, None, None

    dados = payload.get("data")
    chave = dados.get("key") if isinstance(dados, dict) else None
    if not isinstance(chave, dict):
        return MALFORMADO, None, None
    if chave.get("fromMe") is True:
        return IGNORADO_PROPRIO, None, None
    jid = chave.get("remoteJid")
    if not isinstance(jid, str):
        return MALFORMADO, None, None
    if not jid.endswith(SUFIXO_GRUPO):
        return IGNORADO_NAO_GRUPO, None, None

    tipo = dados.get("messageType")
    fonte = _fonte_ativa(con, jid)
    if fonte is None:
        if tipo == "imageMessage":
            _registrar_grupo_visto(con, jid, agora)
        return GRUPO_DESCONHECIDO, None, None
    nome = fonte["nome"]

    if tipo != "imageMessage":
        return IGNORADO_TIPO, nome, None

    mensagem_id = chave.get("id")
    mensagem = dados.get("message")
    if not isinstance(mensagem_id, str) or not mensagem_id or not isinstance(mensagem, dict):
        return MALFORMADO, nome, None
    if con.execute(
        "SELECT 1 FROM encarte WHERE fonte_id = ? AND mensagem_id = ?", (fonte["id"], mensagem_id)
    ).fetchone():
        return REPETIDO, nome, None

    imagem = mensagem.get("imageMessage")
    legenda = imagem.get("caption") if isinstance(imagem, dict) else None
    legenda = (legenda.strip() or None) if isinstance(legenda, str) else None
    recebido = _instante(dados.get("messageTimestamp"), agora)

    codificado = mensagem.get("base64")
    if not isinstance(codificado, str) or not codificado.strip():
        # A Evolution não conseguiu baixar a mídia. Fica a linha como falha, para
        # a tela mostrar que um encarte chegou e se perdeu.
        encarte_id = _inserir_encarte(
            con,
            fonte_id=fonte["id"],
            mensagem_id=mensagem_id,
            recebido_em=_iso(recebido),
            legenda=legenda,
            status="falhou",
            erro="midia_ausente",
        )
        con.commit()
        return SEM_MIDIA, nome, encarte_id

    # Barra pelo tamanho do texto antes de decodificar: 4 caracteres de base64
    # para cada 3 bytes, com folga para quebra de linha.
    if len(codificado) > LIMITE_IMAGEM * 4 // 3 + 1024:
        return GRANDE_DEMAIS, nome, None
    try:
        bruto = base64.b64decode(codificado)
    except (binascii.Error, ValueError):
        return IMAGEM_INVALIDA, nome, None

    codigo, encarte_id = guardar_imagem(con, fonte, bruto, mensagem_id, legenda, recebido, pasta)
    # No REPETIDO o id é o do encarte que já existia: o log do webhook não o cita.
    return codigo, nome, encarte_id if codigo == GUARDADO else None


def guardar_imagem(
    con: sqlite3.Connection,
    fonte: sqlite3.Row,
    bruto: bytes,
    mensagem_id: str,
    legenda: str | None,
    recebido: datetime,
    pasta: Path,
) -> tuple[str, int | None]:
    """Guarda a imagem da fonte em disco e como encarte `pendente`.

    O trecho comum ao webhook e ao `importar` da CLI: limite de tamanho,
    formato pelo conteúdo, dedupe pelo hash na fonte e gravação atômica.
    Devolve (código, id): o id do encarte criado ou, em REPETIDO, o do que já
    tinha a mesma imagem; nos outros descartes, None.
    """
    if len(bruto) > LIMITE_IMAGEM:
        return GRANDE_DEMAIS, None

    formato = _formato(bruto)
    if formato is None:
        return IMAGEM_INVALIDA, None
    mime, extensao = formato

    sha256 = hashlib.sha256(bruto).hexdigest()
    existente = con.execute(
        "SELECT id FROM encarte WHERE fonte_id = ? AND sha256 = ?", (fonte["id"], sha256)
    ).fetchone()
    if existente:
        return REPETIDO, existente["id"]

    alvo = pasta / recebido.date().isoformat() / f"{sha256}.{extensao}"
    # A mesma arte chega no mesmo dia por duas lojas da rede, e o caminho é o
    # mesmo (o hash). Se já estava lá, é da outra fonte: não se regrava, e na
    # falha não se apaga.
    ja_existia = alvo.exists()
    if not ja_existia:
        _gravar_arquivo(alvo, bruto)
    try:
        encarte_id = _inserir_encarte(
            con,
            fonte_id=fonte["id"],
            mensagem_id=mensagem_id,
            recebido_em=_iso(recebido),
            legenda=legenda,
            sha256=sha256,
            arquivo=alvo.relative_to(pasta.parent).as_posix(),
            mime=mime,
            status="pendente",
        )
        con.commit()
    except BaseException:
        con.rollback()
        if not ja_existia:
            alvo.unlink(missing_ok=True)
        raise
    return GUARDADO, encarte_id


# ── leitura dos pendentes ────────────────────────────────────────────────────
# Esperas depois de cada erro temporário: 1, 5, 15 e 60 min, e então de hora
# em hora. Na 8ª tentativa sem sucesso, o encarte desiste e vira `falhou`.
ESPERAS_MIN = (1, 5, 15, 60)
LIMITE_TENTATIVAS = 8
# O texto do `erro` marca a resposta inválida: duas seguidas e desiste.
PREFIXO_RESPOSTA_INVALIDA = "resposta inválida"

PRECO_MINIMO = 10  # centavos: abaixo de R$ 0,10 é leitura errada
PRECO_MAXIMO = 200_000  # centavos: acima de R$ 2.000 também

AVISO_CLUBE_MAIOR = "preço de clube maior que o normal"
AVISO_PRECO_BAIXO = "preço abaixo de R$ 0,10"
AVISO_PRECO_ALTO = "preço acima de R$ 2.000"
AVISO_SEM_NOME = "produto sem nome"
AVISO_SEM_VALIDADE = "validade não encontrada"
AVISO_INVERTIDA = "validade invertida no encarte"
# Seguido de "(dd/mm/aaaa); assumido o dia do recebimento". O prefixo identifica o aviso.
AVISO_VENCIDO = "validade lida já vencida ao chegar"
SEM_NOME = "(sem nome)"


def _espera(tentativas: int) -> timedelta:
    """A espera depois da n-ésima tentativa falha (n começa em 1)."""
    return timedelta(minutes=ESPERAS_MIN[min(tentativas, len(ESPERAS_MIN)) - 1])


def _json_ou_nulo(avisos: list[str]) -> str | None:
    return json.dumps(avisos, ensure_ascii=False) if avisos else None


def normalizar_produto(produto: str) -> str:
    """Minúsculas, sem acento, espaço colapsado: a parte do produto na chave_dedupe."""
    sem_acento = unicodedata.normalize("NFKD", produto)
    sem_acento = "".join(c for c in sem_acento if not unicodedata.combining(c))
    return " ".join(sem_acento.lower().split())


def _avisos_de_preco(normal: int, clube: int | None) -> list[str]:
    avisos = []
    if clube is not None and clube > normal:
        avisos.append(AVISO_CLUBE_MAIOR)
    precos = [p for p in (normal, clube) if p is not None]
    if any(p < PRECO_MINIMO for p in precos):
        avisos.append(AVISO_PRECO_BAIXO)
    if any(p > PRECO_MAXIMO for p in precos):
        avisos.append(AVISO_PRECO_ALTO)
    return avisos


def _pendente_da_vez(con: sqlite3.Connection, agora: datetime) -> sqlite3.Row | None:
    return con.execute(
        """SELECT id, fonte_id, recebido_em, legenda, arquivo, mime, tentativas, erro
             FROM encarte
            WHERE status = 'pendente'
              AND (proxima_tentativa_em IS NULL OR proxima_tentativa_em <= ?)
            ORDER BY recebido_em, id
            LIMIT 1""",
        (_iso(agora),),
    ).fetchone()


def _gravar_falha(
    con: sqlite3.Connection,
    encarte: sqlite3.Row,
    agora: datetime,
    erro: str,
    *,
    temporaria: bool,
    contar: bool = True,
) -> None:
    tentativas = encarte["tentativas"] + (1 if contar else 0)
    if temporaria and tentativas < LIMITE_TENTATIVAS:
        status, proxima = "pendente", _iso(agora + _espera(tentativas))
    else:
        status, proxima = "falhou", None
    con.execute(
        """UPDATE encarte SET status = ?, tentativas = ?, proxima_tentativa_em = ?, erro = ?
            WHERE id = ?""",
        (status, tentativas, proxima, erro, encarte["id"]),
    )
    con.commit()
    log.info("encarte %d: %s na tentativa %d (%s)", encarte["id"], status, tentativas, erro)


def _validade(leitura: LeituraEncarte, dia: date) -> tuple[date, date, list[str]]:
    """A validade do encarte, completada com o dia do recebimento, e os avisos dela."""
    avisos = []
    inicio, fim = leitura.validade_inicio, leitura.validade_fim
    if inicio is None or fim is None:
        avisos.append(AVISO_SEM_VALIDADE)
    elif inicio > fim:
        # Início depois do fim: o modelo trocou as pontas. Desfaz e avisa.
        inicio, fim = fim, inicio
        avisos.append(AVISO_INVERTIDA)
    # O que faltar é o dia do recebimento, sem inverter o intervalo: um encarte
    # que só diz "a partir de sexta" vale de sexta a sexta, não de hoje a sexta.
    if inicio is None:
        inicio = min(dia, fim) if fim is not None else dia
    if fim is None:
        fim = max(dia, inicio)
    if fim < dia:
        # Data já vencida quase sempre é leitura errada (o rodapé com o mês
        # antigo), não encarte velho: vale o dia do recebimento, com o aviso
        # para conferir, e a oferta aparece hoje em vez de sumir.
        avisos.append(f"{AVISO_VENCIDO} ({fim:%d/%m/%Y}); assumido o dia do recebimento")
        inicio = fim = dia
    return inicio, fim, avisos


def _gravar_leitura(
    con: sqlite3.Connection, encarte: sqlite3.Row, leitura: LeituraEncarte, nome_leitor: str
) -> tuple[str, int, int]:
    """Grava ofertas e encarte numa transação só. Devolve (status, válidas, novas)."""
    dia = datetime.fromisoformat(encarte["recebido_em"]).astimezone(FUSO).date()
    inicio, fim, avisos_encarte = _validade(leitura, dia)

    linhas = []
    descartadas = 0
    for lida in leitura.ofertas:
        # Preço zero é preço que não foi lido, não oferta de graça.
        normal = centavos(lida.preco_normal) or None
        clube = centavos(lida.preco_clube) or None
        if normal is None:
            # Um preço só, lido como de clube: é o preço da oferta.
            normal, clube = clube, None
        if normal is None:
            descartadas += 1
            continue
        avisos = _avisos_de_preco(normal, clube)
        produto = lida.produto
        if not produto:
            avisos.append(AVISO_SEM_NOME)
            produto = SEM_NOME
        chave = "|".join(
            (
                normalizar_produto(produto),
                str(normal),
                "" if clube is None else str(clube),
                inicio.isoformat(),
                fim.isoformat(),
            )
        )
        linhas.append(
            (
                encarte["id"],
                encarte["fonte_id"],
                produto,
                lida.embalagem,
                lida.unidade,
                normal,
                clube,
                lida.condicao,
                inicio.isoformat(),
                fim.isoformat(),
                _json_ou_nulo(avisos),
                chave,
            )
        )

    if descartadas:
        avisos_encarte.append(
            "1 oferta sem preço descartada"
            if descartadas == 1
            else f"{descartadas} ofertas sem preço descartadas"
        )
    if linhas:
        status = "lido"
    else:
        # Imagem sem oferta (aviso de horário, arte institucional): a falta de
        # validade ali não é notícia. Só o descarte, se houve, fica registrado.
        status = "sem_ofertas"
        avisos_encarte = [
            a
            for a in avisos_encarte
            if a not in (AVISO_SEM_VALIDADE, AVISO_INVERTIDA) and not a.startswith(AVISO_VENCIDO)
        ]

    try:
        novas = 0
        for linha in linhas:
            novas += con.execute(
                """INSERT OR IGNORE INTO oferta
                       (encarte_id, fonte_id, produto, embalagem, unidade,
                        preco_normal_centavos, preco_clube_centavos, condicao,
                        validade_inicio, validade_fim, avisos, chave_dedupe)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                linha,
            ).rowcount
        con.execute(
            """UPDATE encarte
                  SET status = ?, tentativas = ?, proxima_tentativa_em = NULL, erro = NULL,
                      leitor = ?, validade_inicio = ?, validade_fim = ?, observacoes = ?,
                      avisos = ?
                WHERE id = ?""",
            (
                status,
                encarte["tentativas"] + 1,
                nome_leitor,
                inicio.isoformat(),
                fim.isoformat(),
                leitura.observacoes,
                _json_ou_nulo(avisos_encarte),
                encarte["id"],
            ),
        )
        con.commit()
    except BaseException:
        con.rollback()
        raise
    return status, len(linhas), novas


async def processar_proximo(
    con: sqlite3.Connection, leitor: Leitor, agora: datetime, pasta: Path
) -> int | None:
    """Lê o pendente mais antigo que já pode ser tentado. Devolve o id, ou None.

    `pasta` é a de encartes (config().pasta_encartes), como no webhook: o
    `arquivo` gravado é relativo à pasta-mãe dela.

    Nenhuma transação fica aberta durante a leitura, que leva dezenas de
    segundos: lê a linha e o arquivo, solta, chama o leitor e só então grava.
    """
    encarte = _pendente_da_vez(con, agora)
    if encarte is None:
        return None

    imagem = None
    if encarte["arquivo"]:
        try:
            imagem = (pasta.parent / encarte["arquivo"]).read_bytes()
        except FileNotFoundError:
            pass
    if imagem is None:
        _gravar_falha(con, encarte, agora, "imagem não encontrada", temporaria=False, contar=False)
        return encarte["id"]

    try:
        leitura = await leitor.ler(imagem, encarte["mime"] or "image/jpeg", encarte["legenda"])
    except ErroPermanente as erro:
        _gravar_falha(con, encarte, agora, str(erro), temporaria=False)
    except RespostaInvalida as erro:
        anterior = encarte["erro"] or ""
        _gravar_falha(
            con,
            encarte,
            agora,
            f"{PREFIXO_RESPOSTA_INVALIDA}: {erro}",
            temporaria=not anterior.startswith(PREFIXO_RESPOSTA_INVALIDA),
        )
    except ErroTemporario as erro:
        _gravar_falha(con, encarte, agora, str(erro), temporaria=True)
    except Exception as erro:
        # Bug do leitor não pode travar a fila nem vazar a resposta no log: só
        # o tipo, e conta como tentativa temporária.
        _gravar_falha(
            con, encarte, agora, f"erro inesperado ({type(erro).__name__})", temporaria=True
        )
    else:
        try:
            status, validas, novas = _gravar_leitura(con, encarte, leitura, leitor.nome)
        except Exception as erro:
            # Leitura que não grava (já com rollback) não pode deixar o encarte
            # pendente sem tentativa contada: a fila chamaria o Gemini para sempre.
            _gravar_falha(
                con, encarte, agora, f"erro ao gravar ({type(erro).__name__})", temporaria=True
            )
        else:
            log.info("encarte %d: %s, %d ofertas (%d novas)", encarte["id"], status, validas, novas)
    return encarte["id"]


# ── limpeza ──────────────────────────────────────────────────────────────────
def limpar_imagens(con: sqlite3.Connection, pasta: Path, agora: datetime, dias: int = 7) -> int:
    """Esquece as imagens já tratadas com mais de `dias` e apaga as que ninguém usa.

    As ofertas ficam: só a imagem some. A mesma arte de duas lojas divide o
    arquivo, então ele só é apagado quando nenhuma linha (nem a pendente da
    outra loja) ainda aponta para ele. Devolve quantos arquivos apagou.
    """
    velhos = con.execute(
        """SELECT id, arquivo FROM encarte
            WHERE status != 'pendente' AND arquivo IS NOT NULL AND recebido_em < ?""",
        (_iso(agora - timedelta(days=dias)),),
    ).fetchall()
    if not velhos:
        return 0
    try:
        con.executemany(
            "UPDATE encarte SET arquivo = NULL WHERE id = ?", [(v["id"],) for v in velhos]
        )
        con.commit()
    except BaseException:
        con.rollback()
        raise

    raiz = pasta.resolve()
    apagados = 0
    for arquivo in sorted({v["arquivo"] for v in velhos}):
        if con.execute("SELECT 1 FROM encarte WHERE arquivo = ?", (arquivo,)).fetchone():
            continue
        alvo = (pasta.parent / arquivo).resolve()
        if not alvo.is_relative_to(raiz):
            log.warning("limpeza de encartes: caminho fora da pasta, ignorado")
            continue
        try:
            alvo.unlink()
            apagados += 1
        except FileNotFoundError:
            pass
        # A pasta do dia vazia vai junto; com outro arquivo dentro, fica.
        if alvo.parent != raiz:
            try:
                alvo.parent.rmdir()
            except OSError:
                pass
    log.info(
        "limpeza de encartes: %d imagens esquecidas, %d arquivos apagados", len(velhos), apagados
    )
    return apagados


# ── tela de ofertas ──────────────────────────────────────────────────────────
HORAS_SEM_ENCARTE = 48  # passou disso sem chegar encarte, a linha de saúde acende
DIAS_RECENTES = 7  # a janela das falhas e das imagens sem ofertas


def _avisos(texto: str | None) -> list[str]:
    """A coluna `avisos` (JSON ou NULL) como lista. JSON torto vira lista vazia."""
    if not texto:
        return []
    try:
        valor = json.loads(texto)
    except ValueError:
        return []
    return [str(a) for a in valor] if isinstance(valor, list) else []


def _caminho_seguro(pasta: Path, arquivo: str | None) -> Path | None:
    """O arquivo do encarte, se existe e mora dentro da pasta de encartes.

    O `arquivo` é relativo à pasta-mãe (os dados), como o webhook o grava. Um
    caminho que escape da pasta não é servido, nem conta como imagem.
    """
    if not arquivo:
        return None
    alvo = (pasta.parent / arquivo).resolve()
    if not alvo.is_relative_to(pasta.resolve()):
        log.warning("imagem de encarte: caminho fora da pasta, ignorado")
        return None
    return alvo if alvo.is_file() else None


def imagem_do_encarte(con: sqlite3.Connection, encarte_id: int, pasta: Path) -> tuple[Path, str]:
    """(caminho, mime) da imagem do encarte. LookupError se não há o que servir.

    Não há quando o encarte não existe, quando a imagem já foi podada (arquivo
    NULL, depois de 7 dias), quando o arquivo sumiu ou quando o caminho escapa
    da pasta de encartes.
    """
    linha = con.execute("SELECT arquivo, mime FROM encarte WHERE id = ?", (encarte_id,)).fetchone()
    caminho = _caminho_seguro(pasta, linha["arquivo"]) if linha else None
    if caminho is None:
        raise LookupError(f"encarte {encarte_id} sem imagem")
    return caminho, linha["mime"] or "image/jpeg"


def _saude(con: sqlite3.Connection, agora: datetime) -> dict:
    """O estado da entrada: o último encarte, a fila e o que falhou na semana."""
    ultimo = con.execute(
        """SELECT MAX(e.recebido_em) FROM encarte e
             JOIN encarte_fonte f ON f.id = e.fonte_id
            WHERE f.ativo = 1"""
    ).fetchone()[0]
    desde = _iso(agora - timedelta(days=DIAS_RECENTES))
    contagens = con.execute(
        """SELECT
               COALESCE(SUM(status = 'pendente'), 0),
               COALESCE(SUM(status = 'falhou' AND recebido_em >= ?), 0),
               COALESCE(SUM(status = 'sem_ofertas' AND recebido_em >= ?), 0)
             FROM encarte""",
        (desde, desde),
    ).fetchone()
    pendentes, falhas, sem_ofertas = (int(c) for c in contagens)
    # Nenhum encarte ainda não é alerta: a tela já diz "nenhum recebido".
    atrasado = ultimo is not None and agora - datetime.fromisoformat(ultimo) > timedelta(
        hours=HORAS_SEM_ENCARTE
    )
    return {
        "ultimo_recebido_em": ultimo,
        "pendentes": pendentes,
        "falhas_recentes": falhas,
        "sem_ofertas_recentes": sem_ofertas,
        "alerta": atrasado or falhas > 0,
    }


def ofertas_vigentes(con: sqlite3.Connection, agora: datetime, pasta: Path) -> dict:
    """O que a tela "Ofertas" mostra: as ofertas que valem hoje, loja por loja.

    "Hoje" é o dia em Medianeira, não o do UTC: às 23h30 de lá, o UTC já está
    no dia seguinte. Toda fonte ativa aparece, mesmo sem oferta hoje; a inativa
    some. `pasta` é a de encartes, para saber se a imagem ainda está no disco.
    """
    hoje = agora.astimezone(FUSO).date().isoformat()
    lojas = [
        {"id": f["id"], "nome": f["nome"], "encartes": [], "ofertas": []}
        for f in con.execute(
            "SELECT id, nome FROM encarte_fonte WHERE ativo = 1 ORDER BY nome COLLATE NOCASE, id"
        )
    ]
    por_id = {loja["id"]: loja for loja in lojas}

    ofertas = con.execute(
        """SELECT o.id, o.encarte_id, o.fonte_id, o.produto, o.embalagem, o.unidade,
                  o.preco_normal_centavos, o.preco_clube_centavos, o.condicao,
                  o.validade_inicio, o.validade_fim, o.avisos
             FROM oferta o
             JOIN encarte e ON e.id = o.encarte_id
            WHERE e.status = 'lido' AND o.validade_inicio <= ? AND o.validade_fim >= ?""",
        (hoje, hoje),
    ).fetchall()
    # Ordem por produto sem maiúsculas nem acento: "Água" fica entre os "a".
    ofertas = sorted(ofertas, key=lambda o: (normalizar_produto(o["produto"]), o["id"]))

    encartes_vistos: dict[int, set[int]] = {}
    for o in ofertas:
        loja = por_id.get(o["fonte_id"])
        if loja is None:
            continue
        oferta = {k: o[k] for k in o.keys() if k != "fonte_id"}
        oferta["avisos"] = _avisos(o["avisos"])
        loja["ofertas"].append(oferta)
        encartes_vistos.setdefault(loja["id"], set()).add(o["encarte_id"])

    for fonte_id, ids in encartes_vistos.items():
        marcas = ", ".join("?" for _ in ids)
        for e in con.execute(
            f"""SELECT id, recebido_em, validade_inicio, validade_fim, observacoes, avisos,
                       arquivo
                  FROM encarte WHERE id IN ({marcas}) ORDER BY recebido_em, id""",
            tuple(ids),
        ):
            por_id[fonte_id]["encartes"].append(
                {
                    "id": e["id"],
                    "recebido_em": e["recebido_em"],
                    "validade_inicio": e["validade_inicio"],
                    "validade_fim": e["validade_fim"],
                    "observacoes": e["observacoes"],
                    "avisos": _avisos(e["avisos"]),
                    "tem_imagem": _caminho_seguro(pasta, e["arquivo"]) is not None,
                }
            )

    return {"hoje": hoje, "lojas": lojas, "saude": _saude(con, agora)}


# ── fontes ───────────────────────────────────────────────────────────────────
def _validar_jid(jid: str) -> str:
    jid = (jid or "").strip()
    if not jid.endswith(SUFIXO_GRUPO) or len(jid) == len(SUFIXO_GRUPO):
        raise ValueError(f"jid de grupo inválido: {jid!r} (precisa terminar em {SUFIXO_GRUPO})")
    return jid


def adicionar_fonte(con: sqlite3.Connection, jid: str, nome: str, cnpj: str | None = None) -> int:
    """Cadastra a comunidade como fonte, ou reativa e renomeia a que já existia.

    Sai da tabela de descoberta: dali em diante ela é fonte, não desconhecida.
    """
    jid = _validar_jid(jid)
    nome = (nome or "").strip()
    if not nome:
        raise ValueError("a fonte precisa de um nome")
    cnpj = "".join(c for c in (cnpj or "") if c.isdigit()) or None

    try:
        existente = con.execute(
            "SELECT id FROM encarte_fonte WHERE grupo_jid = ?", (jid,)
        ).fetchone()
        if existente:
            fonte_id = existente["id"]
            con.execute(
                "UPDATE encarte_fonte SET nome = ?, cnpj = ?, ativo = 1 WHERE id = ?",
                (nome, cnpj, fonte_id),
            )
        else:
            fonte_id = con.execute(
                """INSERT INTO encarte_fonte (grupo_jid, nome, cnpj, ativo, criado_em)
                   VALUES (?, ?, ?, 1, ?)""",
                (jid, nome, cnpj, _iso(datetime.now(FUSO))),
            ).lastrowid
        con.execute("DELETE FROM encarte_grupo_visto WHERE grupo_jid = ?", (jid,))
        con.commit()
    except sqlite3.IntegrityError as erro:
        con.rollback()
        raise ValueError(f"cnpj {cnpj} não está entre os estabelecimentos conhecidos") from erro
    return fonte_id


def listar_fontes(con: sqlite3.Connection) -> list[dict]:
    return [
        {**dict(linha), "ativo": bool(linha["ativo"])}
        for linha in con.execute(
            "SELECT id, grupo_jid, nome, cnpj, ativo, criado_em FROM encarte_fonte ORDER BY id"
        )
    ]


def _resolver_fonte(con: sqlite3.Connection, id_ou_jid: int | str) -> sqlite3.Row | None:
    """A fonte pelo id numérico ou pelo jid, ativa ou não. None se não existe."""
    if isinstance(id_ou_jid, int) or str(id_ou_jid).strip().isdigit():
        return con.execute(
            "SELECT id, nome, ativo FROM encarte_fonte WHERE id = ?", (int(id_ou_jid),)
        ).fetchone()
    return con.execute(
        "SELECT id, nome, ativo FROM encarte_fonte WHERE grupo_jid = ?", (_validar_jid(id_ou_jid),)
    ).fetchone()


def desativar_fonte(con: sqlite3.Connection, id_ou_jid: int | str) -> bool:
    """Para de aceitar a fonte, sem apagar os encartes dela. False se não existe."""
    fonte = _resolver_fonte(con, id_ou_jid)
    if fonte is None:
        return False
    con.execute("UPDATE encarte_fonte SET ativo = 0 WHERE id = ?", (fonte["id"],))
    con.commit()
    return True


def listar_grupos_vistos(con: sqlite3.Connection) -> list[dict]:
    """Os grupos desconhecidos que mandaram imagem, o mais recente primeiro."""
    return [
        dict(linha)
        for linha in con.execute(
            """SELECT grupo_jid, primeira_vez, ultima_vez, imagens
                 FROM encarte_grupo_visto ORDER BY ultima_vez DESC, grupo_jid"""
        )
    ]


# ── importar e reler (CLI) ───────────────────────────────────────────────────
# O WhatsApp só entrega à Evolution o que chega depois do pareamento, e uma
# leitura errada não tem outra chance sozinha: estes dois põem o encarte na
# fila à mão. O laço do app pega o pendente na volta seguinte (até 60 s).
def _recusar_gravacao_no_demo() -> None:
    from .config import ModoDemo, config

    if config().app_mode == "demo":
        raise ModoDemo(
            "Importar e reler encartes gravam dados: desativado na demonstração pública."
        )


def importar_imagem(
    con: sqlite3.Connection,
    id_ou_jid: int | str,
    caminho: Path,
    pasta: Path,
    recebido: datetime,
    legenda: str | None = None,
) -> tuple[str, int | None, str]:
    """Põe na fila uma imagem do disco, pelas mesmas regras do webhook.

    Devolve (código, id, nome da fonte); o id é o do encarte criado ou, em
    REPETIDO, o do que já tinha a mesma imagem. ValueError se a fonte não
    existe ou está inativa.
    """
    _recusar_gravacao_no_demo()
    fonte = _resolver_fonte(con, id_ou_jid)
    if fonte is None:
        raise ValueError(f"fonte {id_ou_jid} não encontrada")
    if not fonte["ativo"]:
        raise ValueError(f"fonte {id_ou_jid} está inativa")
    # Lê um byte além do limite: o que passar dele é GRANDE_DEMAIS sem carregar
    # o arquivo inteiro.
    try:
        with caminho.open("rb") as entrada:
            bruto = entrada.read(LIMITE_IMAGEM + 1)
    except OSError as erro:
        raise ValueError(f"não deu para ler {caminho} ({type(erro).__name__})") from None
    legenda = (legenda or "").strip() or None
    # "manual:" não colide com os ids de mensagem do WhatsApp.
    mensagem_id = "manual:" + hashlib.sha256(bruto).hexdigest()
    codigo, encarte_id = guardar_imagem(
        con, fonte, bruto, mensagem_id, legenda, recebido.astimezone(FUSO), pasta
    )
    return codigo, encarte_id, fonte["nome"]


def reler_encarte(con: sqlite3.Connection, encarte_id: int, pasta: Path) -> bool:
    """Volta o encarte para a fila, sem as ofertas da leitura anterior.

    True se voltou para pendente; False se já estava pendente, caso em que só
    zera as tentativas, para tentar já. LookupError se o encarte não existe;
    ValueError se a imagem já foi apagada (nada muda).

    Oferta idêntica (mesma chave_dedupe) gravada por outro encarte da mesma
    fonte fica com ele: a releitura a ignora no INSERT OR IGNORE.
    """
    _recusar_gravacao_no_demo()
    linha = con.execute(
        "SELECT status, arquivo FROM encarte WHERE id = ?", (encarte_id,)
    ).fetchone()
    if linha is None:
        raise LookupError(f"encarte {encarte_id} não encontrado")
    if _caminho_seguro(pasta, linha["arquivo"]) is None:
        raise ValueError("imagem já apagada; reimporte com `importar`")
    try:
        if linha["status"] == "pendente":
            con.execute(
                """UPDATE encarte SET tentativas = 0, proxima_tentativa_em = NULL
                    WHERE id = ?""",
                (encarte_id,),
            )
        else:
            con.execute("DELETE FROM oferta WHERE encarte_id = ?", (encarte_id,))
            con.execute(
                """UPDATE encarte
                      SET status = 'pendente', tentativas = 0, proxima_tentativa_em = NULL,
                          erro = NULL, leitor = NULL, validade_inicio = NULL,
                          validade_fim = NULL, observacoes = NULL, avisos = NULL
                    WHERE id = ?""",
                (encarte_id,),
            )
        con.commit()
    except BaseException:
        con.rollback()
        raise
    return linha["status"] != "pendente"


def _data_de_recebimento(valor: str) -> datetime:
    """`AAAA-MM-DD` (meio-dia local) ou `AAAA-MM-DDTHH:MM`, no fuso da casa."""
    try:
        if len(valor) == len("AAAA-MM-DD"):
            dia = date.fromisoformat(valor)
            return datetime(dia.year, dia.month, dia.day, 12, 0, tzinfo=FUSO)
        return datetime.strptime(valor, "%Y-%m-%dT%H:%M").replace(tzinfo=FUSO)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"data inválida: {valor!r} (use AAAA-MM-DD ou AAAA-MM-DDTHH:MM)"
        ) from None


def main(argv: list[str] | None = None) -> int:
    """CLI para cadastrar as comunidades que mandam encarte e pôr encartes na fila."""
    from .banco import conectar
    from .config import config

    parser = argparse.ArgumentParser(description="Fontes de encartes do mise_en_prato")
    sub = parser.add_subparsers(dest="comando")

    sub.add_parser("fontes", help="Lista as fontes cadastradas")

    p_adicionar = sub.add_parser("adicionar", help="Cadastra (ou reativa) uma comunidade")
    p_adicionar.add_argument("jid", help="O jid do grupo, terminado em @g.us")
    p_adicionar.add_argument("nome", help='O nome da loja, como "LAR Centro"')
    p_adicionar.add_argument("--cnpj", help="O CNPJ da loja, se já houver cupom dela")

    p_desativar = sub.add_parser("desativar", help="Para de aceitar encartes da fonte")
    p_desativar.add_argument("fonte", help="O id ou o jid da fonte")

    sub.add_parser("grupos", help="Lista os grupos desconhecidos que mandaram imagem")

    p_importar = sub.add_parser("importar", help="Põe na fila uma imagem de encarte do disco")
    p_importar.add_argument("fonte", help="O id ou o jid da fonte (ativa)")
    p_importar.add_argument("arquivo", type=Path, help="A imagem (JPEG, PNG ou WebP)")
    p_importar.add_argument("--legenda", help="A legenda, se a mensagem tinha uma")
    p_importar.add_argument(
        "--recebido-em",
        type=_data_de_recebimento,
        help="AAAA-MM-DD (vale 12:00) ou AAAA-MM-DDTHH:MM, hora local; o padrão é agora",
    )

    p_reler = sub.add_parser("reler", help="Volta um encarte para a fila de leitura")
    p_reler.add_argument("encarte", type=int, help="O id do encarte")

    args = parser.parse_args(argv)
    if args.comando is None:
        parser.print_help()
        return 0

    # Antes de abrir o banco: conectar já migra, e no demo nada se grava.
    if args.comando in ("importar", "reler") and config().app_mode == "demo":
        print(f"Erro: {args.comando} grava dados e está desativado na demonstração pública.")
        return 1

    con = conectar(config().banco)
    try:
        if args.comando == "fontes":
            fontes = listar_fontes(con)
            if not fontes:
                print("Nenhuma fonte cadastrada.")
            for f in fontes:
                estado = "ativa" if f["ativo"] else "inativa"
                cnpj = f["cnpj"] or "s/ cnpj"
                print(f"  [{f['id']}] {f['nome']} — {f['grupo_jid']} ({cnpj}, {estado})")
        elif args.comando == "adicionar":
            fonte_id = adicionar_fonte(con, args.jid, args.nome, args.cnpj)
            print(f"Fonte {fonte_id} cadastrada: {args.nome.strip()}")
        elif args.comando == "desativar":
            if not desativar_fonte(con, args.fonte):
                print(f"Fonte {args.fonte} não encontrada.")
                return 1
            print(f"Fonte {args.fonte} desativada.")
        elif args.comando == "grupos":
            grupos = listar_grupos_vistos(con)
            if not grupos:
                print("Nenhum grupo desconhecido mandou imagem.")
            for g in grupos:
                print(
                    f"  {g['grupo_jid']}  primeira: {g['primeira_vez']}  "
                    f"última: {g['ultima_vez']}  imagens: {g['imagens']}"
                )
        elif args.comando == "importar":
            codigo, encarte_id, nome = importar_imagem(
                con,
                args.fonte,
                args.arquivo,
                config().pasta_encartes,
                args.recebido_em or datetime.now(FUSO),
                args.legenda,
            )
            if codigo == GUARDADO:
                print(f"Encarte {encarte_id} pendente ({nome})")
            elif codigo == REPETIDO:
                print(f"Já importado: encarte {encarte_id} ({nome})")
            else:
                print(f"Descartado: {codigo.upper()} ({nome})")
                return 1
        elif args.comando == "reler":
            if reler_encarte(con, args.encarte, config().pasta_encartes):
                print(f"Encarte {args.encarte} de volta à fila (pendente).")
            else:
                print(f"Encarte {args.encarte} já estava pendente: tentativas zeradas.")
    except (ValueError, LookupError) as erro:
        print(f"Erro: {erro}")
        return 1
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
