"""Leitura dos encartes: o leitor Gemini, a fila com novas tentativas, os avisos e a limpeza.

Nada aqui fala com a rede: o Gemini entra por httpx.MockTransport, com as
respostas reais gravadas em fixtures, e o laço recebe um leitor falso.
"""

import asyncio
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from mise_en_prato import encartes, tarefas
from mise_en_prato.banco import conectar
from mise_en_prato.config import ModoDemo, config
from mise_en_prato.leitor_encartes import (
    ErroPermanente,
    ErroTemporario,
    LeitorGemini,
    LeituraEncarte,
    OfertaLida,
    RespostaInvalida,
    interpretar_resposta,
)
from tests.test_encartes import GRUPO_LAR, SEGREDO, _app, _enviar, _payload

FIXTURES = Path(__file__).parent / "fixtures" / "encartes"
CHAVE = "chave-falsa-de-teste-123"
FUSO = encartes.FUSO
# Recebido no sábado 21/11 às 10h; o processamento roda logo depois.
RECEBIDO = datetime(2026, 11, 21, 10, 0, tzinfo=FUSO)
AGORA = datetime(2026, 11, 21, 10, 5, tzinfo=FUSO)
DIA = date(2026, 11, 21)


def _fixture(nome: str) -> dict:
    return json.loads((FIXTURES / f"gemini_{nome}.json").read_text(encoding="utf-8"))


def _corpo_com_texto(texto: str, motivo: str = "STOP") -> dict:
    return {
        "candidates": [
            {"content": {"role": "model", "parts": [{"text": texto}]}, "finishReason": motivo}
        ]
    }


def _leitor(resposta, pedidos: list | None = None) -> LeitorGemini:
    """LeitorGemini com transporte falso. `resposta`: Response, exceção ou função."""

    def tratar(pedido: httpx.Request) -> httpx.Response:
        if pedidos is not None:
            pedidos.append(pedido)
        if isinstance(resposta, Exception):
            raise resposta
        if callable(resposta):
            return resposta(pedido)
        return resposta

    cliente = httpx.AsyncClient(transport=httpx.MockTransport(tratar))
    return LeitorGemini(CHAVE, "gemini-teste", cliente)


async def _ler(leitor: LeitorGemini, legenda: str | None = None) -> LeituraEncarte:
    return await leitor.ler(b"\x89PNG...", "image/png", legenda)


# ── leitor: respostas reais ──────────────────────────────────────────────────
def test_fixture_real_do_lar():
    leitura = interpretar_resposta(_fixture("lar"))
    assert len(leitura.ofertas) == 12
    assert leitura.validade_inicio == leitura.validade_fim == date(2026, 11, 21)
    alcatra = leitura.ofertas[0]
    assert "alcatra" in alcatra.produto.lower()
    assert (alcatra.preco_normal, alcatra.preco_clube, alcatra.unidade) == (59.9, 56.9, "kg")
    bacon = leitura.ofertas[1]
    assert (bacon.preco_normal, bacon.preco_clube, bacon.embalagem) == (30.9, 24.9, "1kg")


def test_fixture_real_do_max():
    leitura = interpretar_resposta(_fixture("max"))
    assert len(leitura.ofertas) == 3
    # O encarte diz 22/11 no topo e 22/10 no rodapé: o modelo registra a divergência.
    assert leitura.observacoes
    assert all(o.condicao for o in leitura.ofertas)
    assert all(o.preco_normal and o.preco_clube for o in leitura.ofertas)


def test_fixtures_nao_guardam_chave():
    for nome in ("lar", "max"):
        texto = (FIXTURES / f"gemini_{nome}.json").read_text(encoding="utf-8")
        assert "AIza" not in texto
        assert "key" not in texto.lower()


# ── leitor: formato ──────────────────────────────────────────────────────────
async def test_json_quebrado_e_resposta_invalida():
    leitor = _leitor(httpx.Response(200, json=_corpo_com_texto('{"ofertas": [')))
    with pytest.raises(RespostaInvalida, match="JSON inválido"):
        await _ler(leitor)


async def test_formato_errado_e_resposta_invalida_sem_vazar_conteudo():
    texto = json.dumps({"ofertas": [{"produto": ["segredo", "da", "resposta"]}]})
    leitor = _leitor(httpx.Response(200, json=_corpo_com_texto(texto)))
    with pytest.raises(RespostaInvalida) as erro:
        await _ler(leitor)
    assert "segredo" not in str(erro.value)


async def test_corpo_http_sem_json_e_resposta_invalida():
    leitor = _leitor(httpx.Response(200, text="<html>oi</html>"))
    with pytest.raises(RespostaInvalida):
        await _ler(leitor)


def test_campos_faltando_e_extras_sao_tolerados():
    texto = json.dumps(
        {
            "validade_inicio": "21/11/2026",  # fora do formato: vira None
            "validade_fim": "2026-11-23",
            "campo_novo": {"qualquer": 1},
            "ofertas": [
                {"produto": "  Arroz  ", "preco_normal": "5,99", "unidade": "UN", "extra": 1},
                {"preco_clube": 3, "unidade": "litro"},
            ],
        }
    )
    leitura = interpretar_resposta(_corpo_com_texto(texto))
    assert leitura.mercado is None and leitura.observacoes is None
    assert leitura.validade_inicio is None
    assert leitura.validade_fim == date(2026, 11, 23)
    arroz, sem_nome = leitura.ofertas
    assert (arroz.produto, arroz.preco_normal, arroz.unidade) == ("Arroz", 5.99, "un")
    assert arroz.preco_clube is None and arroz.condicao is None
    assert (sem_nome.produto, sem_nome.preco_clube, sem_nome.unidade) == (None, 3.0, None)


def test_ofertas_nulas_viram_lista_vazia():
    leitura = interpretar_resposta(_corpo_com_texto('{"ofertas": null}'))
    assert leitura.ofertas == []


# ── leitor: erros de HTTP e rede ─────────────────────────────────────────────
@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
async def test_status_temporarios(status):
    leitor = _leitor(httpx.Response(status, json={"error": {"message": "x"}}))
    with pytest.raises(ErroTemporario, match=f"HTTP {status}"):
        await _ler(leitor)


@pytest.mark.parametrize(
    "excecao",
    [httpx.ReadTimeout("lento"), httpx.ConnectTimeout("lento")],
)
async def test_timeout_e_temporario(excecao):
    with pytest.raises(ErroTemporario, match="timeout"):
        await _ler(_leitor(excecao))


async def test_erro_de_rede_e_temporario():
    with pytest.raises(ErroTemporario, match="rede"):
        await _ler(_leitor(httpx.ConnectError("sem rota")))


@pytest.mark.parametrize("status", [400, 403, 404])
async def test_chave_invalida_e_permanente(status):
    corpo = {"error": {"code": status, "message": "API key not valid", "status": "INVALID"}}
    leitor = _leitor(httpx.Response(status, json=corpo))
    with pytest.raises(ErroPermanente, match=f"HTTP {status}") as erro:
        await _ler(leitor)
    assert CHAVE not in str(erro.value)


async def test_pedido_bloqueado_e_permanente():
    corpo = {"promptFeedback": {"blockReason": "SAFETY"}}
    with pytest.raises(ErroPermanente, match="bloqueado"):
        await _ler(_leitor(httpx.Response(200, json=corpo)))


@pytest.mark.parametrize("motivo", ["SAFETY", "MAX_TOKENS", "RECITATION"])
async def test_resposta_interrompida_e_permanente(motivo):
    corpo = _corpo_com_texto('{"ofertas": []}', motivo=motivo)
    with pytest.raises(ErroPermanente, match=motivo):
        await _ler(_leitor(httpx.Response(200, json=corpo)))


@pytest.mark.parametrize("corpo", [{}, {"candidates": []}, _corpo_com_texto("   ")])
async def test_sem_candidato_ou_sem_texto_e_permanente(corpo):
    with pytest.raises(ErroPermanente):
        await _ler(_leitor(httpx.Response(200, json=corpo)))


# ── leitor: o pedido ─────────────────────────────────────────────────────────
async def test_chave_vai_no_cabecalho_e_nao_na_url():
    pedidos = []
    leitor = _leitor(httpx.Response(200, json=_fixture("lar")), pedidos)
    leitura = await _ler(leitor, legenda="Válido só hoje na loja Centro")
    assert len(leitura.ofertas) == 12

    (pedido,) = pedidos
    assert pedido.headers["x-goog-api-key"] == CHAVE
    assert CHAVE not in str(pedido.url)
    assert not pedido.url.query
    assert str(pedido.url) == (
        "https://generativelanguage.googleapis.com/v1beta/models/gemini-teste:generateContent"
    )
    corpo = json.loads(pedido.content)
    config_geracao = corpo["generationConfig"]
    assert config_geracao["temperature"] == 0
    assert config_geracao["responseMimeType"] == "application/json"
    assert (
        "condicao"
        in config_geracao["responseSchema"]["properties"]["ofertas"]["items"]["properties"]
    )
    partes = corpo["contents"][0]["parts"]
    assert "condicao" in partes[0]["text"]
    assert partes[1]["inline_data"]["mime_type"] == "image/png"
    assert "Válido só hoje na loja Centro" in partes[2]["text"]
    assert "vale o encarte" in partes[2]["text"]
    assert CHAVE not in repr(leitor)


async def test_sem_legenda_nao_manda_parte_de_legenda():
    pedidos = []
    await _ler(_leitor(httpx.Response(200, json=_fixture("max")), pedidos))
    partes = json.loads(pedidos[0].content)["contents"][0]["parts"]
    assert len(partes) == 2


async def test_demo_recusa_sem_chamar_o_transporte(monkeypatch):
    monkeypatch.setenv("APP_MODE", "demo")
    config.cache_clear()
    pedidos = []
    with pytest.raises(ModoDemo):
        await _ler(_leitor(httpx.Response(200, json=_fixture("lar")), pedidos))
    assert pedidos == []


# ── processamento: apoio ─────────────────────────────────────────────────────
class LeitorFalso:
    """Devolve, em ordem, as leituras ou levanta as exceções da lista."""

    nome = "leitor-falso"

    def __init__(self, *respostas, con=None):
        self.respostas = list(respostas)
        self.chamadas = []
        self.con = con
        self.em_transacao = []

    async def ler(self, imagem, mime, legenda):
        self.chamadas.append((imagem, mime, legenda))
        if self.con is not None:
            self.em_transacao.append(self.con.in_transaction)
        resposta = self.respostas.pop(0) if len(self.respostas) > 1 else self.respostas[0]
        if isinstance(resposta, Exception):
            raise resposta
        return resposta


@pytest.fixture
def con(tmp_path):
    con = conectar(tmp_path / "mise_en_prato.db")
    yield con
    con.close()


@pytest.fixture
def pasta(tmp_path):
    return tmp_path / "encartes"


def _fonte(con, n: int = 1) -> int:
    return encartes.adicionar_fonte(con, f"12036300000000000{n}@g.us", f"Loja {n}")


def _pendente(
    con,
    pasta: Path,
    fonte_id: int,
    *,
    recebido: datetime = RECEBIDO,
    nome: str | None = None,
    conteudo: bytes = b"imagem",
    legenda: str | None = None,
    status: str = "pendente",
) -> int:
    """Linha de encarte com o arquivo em disco, como o webhook deixaria."""
    nome = nome or f"{fonte_id}-{recebido.timestamp():.0f}-{len(conteudo)}"
    alvo = pasta / recebido.date().isoformat() / f"{nome}.png"
    alvo.parent.mkdir(parents=True, exist_ok=True)
    alvo.write_bytes(conteudo)
    encarte_id = encartes._inserir_encarte(
        con,
        fonte_id=fonte_id,
        mensagem_id=f"msg-{nome}",
        recebido_em=encartes._iso(recebido),
        legenda=legenda,
        sha256=nome,
        arquivo=alvo.relative_to(pasta.parent).as_posix(),
        mime="image/png",
        status=status,
    )
    con.commit()
    return encarte_id


def _encarte(con, encarte_id: int) -> dict:
    return dict(con.execute("SELECT * FROM encarte WHERE id = ?", (encarte_id,)).fetchone())


def _ofertas(con) -> list[dict]:
    return [dict(r) for r in con.execute("SELECT * FROM oferta ORDER BY id")]


def _leitura(*ofertas: OfertaLida, inicio=DIA, fim=DIA, observacoes=None) -> LeituraEncarte:
    return LeituraEncarte(
        mercado="Loja",
        validade_inicio=inicio,
        validade_fim=fim,
        observacoes=observacoes,
        ofertas=list(ofertas),
    )


def _oferta(produto="Arroz Tio João 5kg", normal=24.9, clube=21.9, **extra) -> OfertaLida:
    return OfertaLida(produto=produto, preco_normal=normal, preco_clube=clube, **extra)


async def _processar(con, leitor, pasta, agora=AGORA):
    return await encartes.processar_proximo(con, leitor, agora, pasta)


# ── processamento: caminho feliz ─────────────────────────────────────────────
async def test_caminho_feliz_grava_ofertas_em_centavos(con, pasta):
    fonte = _fonte(con)
    encarte_id = _pendente(con, pasta, fonte, conteudo=b"bytes-da-imagem", legenda="Só hoje")
    leitor = LeitorFalso(
        _leitura(
            _oferta("Miolo da Alcatra", 59.9, 56.9, unidade="kg"),
            _oferta("Bala", 0.29, None, unidade="un", embalagem="1un", condicao="leve 3 pague 2"),
            _oferta("Café", 19.99, 17.99),
        ),
        con=con,
    )
    assert await _processar(con, leitor, pasta) == encarte_id

    assert leitor.chamadas == [(b"bytes-da-imagem", "image/png", "Só hoje")]
    assert leitor.em_transacao == [False]  # nada aberto durante a leitura
    e = _encarte(con, encarte_id)
    assert e["status"] == "lido"
    assert e["leitor"] == "leitor-falso"
    assert e["tentativas"] == 1
    assert e["erro"] is None and e["proxima_tentativa_em"] is None
    assert (e["validade_inicio"], e["validade_fim"]) == ("2026-11-21", "2026-11-21")
    assert e["avisos"] is None

    alcatra, bala, cafe = _ofertas(con)
    assert (alcatra["preco_normal_centavos"], alcatra["preco_clube_centavos"]) == (5990, 5690)
    assert alcatra["unidade"] == "kg"
    assert (bala["preco_normal_centavos"], bala["preco_clube_centavos"]) == (29, None)
    assert (bala["embalagem"], bala["condicao"]) == ("1un", "leve 3 pague 2")
    assert (cafe["preco_normal_centavos"], cafe["preco_clube_centavos"]) == (1999, 1799)
    for o in (alcatra, bala, cafe):
        assert (o["encarte_id"], o["fonte_id"]) == (encarte_id, fonte)
        assert (o["validade_inicio"], o["validade_fim"]) == ("2026-11-21", "2026-11-21")
        assert o["avisos"] is None
    assert alcatra["chave_dedupe"] == "miolo da alcatra|5990|5690|2026-11-21|2026-11-21"


async def test_sem_pendente_devolve_none(con, pasta):
    _fonte(con)
    assert await _processar(con, LeitorFalso(_leitura()), pasta) is None


async def test_ignora_quem_nao_e_pendente(con, pasta):
    fonte = _fonte(con)
    _pendente(con, pasta, fonte, status="lido")
    leitor = LeitorFalso(_leitura())
    assert await _processar(con, leitor, pasta) is None
    assert leitor.chamadas == []


async def test_ordem_por_chegada(con, pasta):
    fonte = _fonte(con)
    depois = _pendente(con, pasta, fonte, recebido=RECEBIDO)
    antes = _pendente(con, pasta, fonte, recebido=RECEBIDO - timedelta(hours=1))
    leitor = LeitorFalso(_leitura(_oferta()))
    assert await _processar(con, leitor, pasta) == antes
    assert await _processar(con, leitor, pasta) == depois
    assert await _processar(con, leitor, pasta) is None


@pytest.mark.parametrize("apagar_linha", [False, True])
async def test_imagem_sumida_falha_na_hora(con, pasta, apagar_linha):
    fonte = _fonte(con)
    encarte_id = _pendente(con, pasta, fonte)
    if apagar_linha:
        con.execute("UPDATE encarte SET arquivo = NULL WHERE id = ?", (encarte_id,))
        con.commit()
    else:
        (pasta.parent / _encarte(con, encarte_id)["arquivo"]).unlink()
    leitor = LeitorFalso(_leitura(_oferta()))
    assert await _processar(con, leitor, pasta) == encarte_id
    e = _encarte(con, encarte_id)
    assert (e["status"], e["erro"]) == ("falhou", "imagem não encontrada")
    assert leitor.chamadas == []


async def test_zero_ofertas_vira_sem_ofertas(con, pasta):
    fonte = _fonte(con)
    encarte_id = _pendente(con, pasta, fonte)
    await _processar(con, LeitorFalso(_leitura(inicio=None, fim=None)), pasta)
    e = _encarte(con, encarte_id)
    assert e["status"] == "sem_ofertas"
    assert e["avisos"] is None
    assert _ofertas(con) == []


async def test_so_preco_de_clube_vira_normal(con, pasta):
    fonte = _fonte(con)
    _pendente(con, pasta, fonte)
    await _processar(con, LeitorFalso(_leitura(_oferta(normal=None, clube=9.99))), pasta)
    (o,) = _ofertas(con)
    assert (o["preco_normal_centavos"], o["preco_clube_centavos"]) == (999, None)


async def test_oferta_sem_preco_e_descartada_com_aviso(con, pasta):
    fonte = _fonte(con)
    encarte_id = _pendente(con, pasta, fonte)
    leitura = _leitura(_oferta(), _oferta("Feijão", None, None), _oferta("Sal", None, None))
    await _processar(con, LeitorFalso(leitura), pasta)
    assert [o["produto"] for o in _ofertas(con)] == ["Arroz Tio João 5kg"]
    e = _encarte(con, encarte_id)
    assert e["status"] == "lido"
    assert json.loads(e["avisos"]) == ["2 ofertas sem preço descartadas"]


async def test_todas_sem_preco_viram_sem_ofertas_com_aviso_do_descarte(con, pasta):
    fonte = _fonte(con)
    encarte_id = _pendente(con, pasta, fonte)
    await _processar(con, LeitorFalso(_leitura(_oferta("Feijão", None, None))), pasta)
    e = _encarte(con, encarte_id)
    assert e["status"] == "sem_ofertas"
    assert json.loads(e["avisos"]) == ["1 oferta sem preço descartada"]


async def test_dedupe_dentro_da_fonte_e_nao_entre_fontes(con, pasta):
    lar, max_ = _fonte(con, 1), _fonte(con, 2)
    _pendente(con, pasta, lar, nome="a")
    _pendente(con, pasta, lar, nome="b")  # a rede reenviou a mesma oferta
    _pendente(con, pasta, max_, nome="c")
    leitor = LeitorFalso(_leitura(_oferta("Arroz  TIO joão 5kg")))
    for _ in range(3):
        await _processar(con, leitor, pasta)
    ofertas = _ofertas(con)
    assert sorted(o["fonte_id"] for o in ofertas) == [lar, max_]
    # o segundo encarte da mesma loja leu a oferta, mesmo sem linha nova
    assert {r[0] for r in con.execute("SELECT status FROM encarte")} == {"lido"}


async def test_mesma_oferta_com_outro_preco_nao_e_repetida(con, pasta):
    fonte = _fonte(con)
    _pendente(con, pasta, fonte)
    leitura = _leitura(_oferta(normal=24.9), _oferta(normal=23.9))
    await _processar(con, LeitorFalso(leitura), pasta)
    assert len(_ofertas(con)) == 2


# ── processamento: falhas e novas tentativas ─────────────────────────────────
async def test_sequencia_de_esperas_e_falha_na_oitava(con, pasta):
    fonte = _fonte(con)
    encarte_id = _pendente(con, pasta, fonte)
    leitor = LeitorFalso(ErroTemporario("HTTP 503"))
    agora = AGORA
    esperas = []
    for tentativa in range(1, 8):
        assert await _processar(con, leitor, pasta, agora) == encarte_id
        e = _encarte(con, encarte_id)
        assert (e["status"], e["tentativas"], e["erro"]) == ("pendente", tentativa, "HTTP 503")
        proxima = datetime.fromisoformat(e["proxima_tentativa_em"])
        esperas.append(int((proxima - agora).total_seconds() // 60))
        agora = proxima
    assert esperas == [1, 5, 15, 60, 60, 60, 60]

    assert await _processar(con, leitor, pasta, agora) == encarte_id
    e = _encarte(con, encarte_id)
    assert (e["status"], e["tentativas"], e["proxima_tentativa_em"]) == ("falhou", 8, None)
    assert await _processar(con, leitor, pasta, agora + timedelta(days=1)) is None


async def test_respeita_proxima_tentativa(con, pasta):
    fonte = _fonte(con)
    encarte_id = _pendente(con, pasta, fonte)
    leitor = LeitorFalso(ErroTemporario("HTTP 429"), _leitura(_oferta()))
    await _processar(con, leitor, pasta)
    assert await _processar(con, leitor, pasta, AGORA + timedelta(seconds=59)) is None
    assert len(leitor.chamadas) == 1
    assert await _processar(con, leitor, pasta, AGORA + timedelta(minutes=1)) == encarte_id
    e = _encarte(con, encarte_id)
    assert (e["status"], e["tentativas"], e["erro"]) == ("lido", 2, None)
    assert e["proxima_tentativa_em"] is None


async def test_espera_de_um_nao_trava_o_seguinte(con, pasta):
    fonte = _fonte(con)
    primeiro = _pendente(con, pasta, fonte, recebido=RECEBIDO - timedelta(hours=1))
    segundo = _pendente(con, pasta, fonte)
    leitor = LeitorFalso(ErroTemporario("timeout"), _leitura(_oferta()))
    assert await _processar(con, leitor, pasta) == primeiro
    assert await _processar(con, leitor, pasta) == segundo
    assert _encarte(con, segundo)["status"] == "lido"


async def test_permanente_falha_na_hora(con, pasta):
    fonte = _fonte(con)
    encarte_id = _pendente(con, pasta, fonte)
    await _processar(con, LeitorFalso(ErroPermanente("HTTP 403")), pasta)
    e = _encarte(con, encarte_id)
    assert (e["status"], e["tentativas"], e["erro"]) == ("falhou", 1, "HTTP 403")
    assert e["proxima_tentativa_em"] is None


async def test_duas_respostas_invalidas_seguidas_falham(con, pasta):
    fonte = _fonte(con)
    encarte_id = _pendente(con, pasta, fonte)
    leitor = LeitorFalso(RespostaInvalida("JSON inválido"))
    await _processar(con, leitor, pasta)
    e = _encarte(con, encarte_id)
    assert (e["status"], e["erro"]) == ("pendente", "resposta inválida: JSON inválido")
    await _processar(con, leitor, pasta, AGORA + timedelta(minutes=1))
    e = _encarte(con, encarte_id)
    assert (e["status"], e["tentativas"]) == ("falhou", 2)


async def test_resposta_invalida_intercalada_nao_soma(con, pasta):
    fonte = _fonte(con)
    encarte_id = _pendente(con, pasta, fonte)
    leitor = LeitorFalso(
        RespostaInvalida("JSON inválido"),
        ErroTemporario("HTTP 503"),
        RespostaInvalida("JSON inválido"),
    )
    agora = AGORA
    for _ in range(3):
        await _processar(con, leitor, pasta, agora)
        agora += timedelta(hours=1)
    e = _encarte(con, encarte_id)
    assert (e["status"], e["tentativas"]) == ("pendente", 3)


async def test_erro_inesperado_do_leitor_conta_como_temporario(con, pasta):
    fonte = _fonte(con)
    encarte_id = _pendente(con, pasta, fonte)
    await _processar(con, LeitorFalso(KeyError("conteudo da resposta")), pasta)
    e = _encarte(con, encarte_id)
    assert (e["status"], e["erro"]) == ("pendente", "erro inesperado (KeyError)")


# ── avisos ───────────────────────────────────────────────────────────────────
async def _aviso_da_oferta(con, pasta, oferta: OfertaLida) -> list[str]:
    fonte = _fonte(con)
    _pendente(con, pasta, fonte)
    await _processar(con, LeitorFalso(_leitura(oferta)), pasta)
    (o,) = _ofertas(con)
    return json.loads(o["avisos"]) if o["avisos"] else []


async def test_aviso_clube_maior_que_normal(con, pasta):
    avisos = await _aviso_da_oferta(con, pasta, _oferta(normal=10.0, clube=12.0))
    assert avisos == ["preço de clube maior que o normal"]


async def test_aviso_preco_abaixo_de_dez_centavos(con, pasta):
    avisos = await _aviso_da_oferta(con, pasta, _oferta(normal=5.0, clube=0.09))
    assert avisos == ["preço abaixo de R$ 0,10"]


async def test_aviso_preco_acima_de_dois_mil(con, pasta):
    avisos = await _aviso_da_oferta(con, pasta, _oferta(normal=2000.01, clube=None))
    assert avisos == ["preço acima de R$ 2.000"]


async def test_limites_exatos_nao_avisam(con, pasta):
    avisos = await _aviso_da_oferta(con, pasta, _oferta(normal=2000.0, clube=0.10))
    assert avisos == []


@pytest.mark.parametrize("produto", [None, "", "   "])
async def test_aviso_produto_sem_nome(con, pasta, produto):
    avisos = await _aviso_da_oferta(con, pasta, _oferta(produto=produto))
    assert avisos == ["produto sem nome"]
    assert _ofertas(con)[0]["produto"] == "(sem nome)"


@pytest.mark.parametrize(
    ("inicio", "fim", "esperado"),
    [
        (None, None, ("2026-11-21", "2026-11-21")),
        (None, date(2026, 11, 24), ("2026-11-21", "2026-11-24")),
        (date(2026, 11, 20), None, ("2026-11-20", "2026-11-21")),
        # só o início, e no futuro: o fim acompanha, o intervalo não inverte
        (date(2026, 11, 27), None, ("2026-11-27", "2026-11-27")),
    ],
)
async def test_aviso_sem_validade_usa_o_dia_do_recebimento(con, pasta, inicio, fim, esperado):
    fonte = _fonte(con)
    encarte_id = _pendente(con, pasta, fonte)
    await _processar(con, LeitorFalso(_leitura(_oferta(), inicio=inicio, fim=fim)), pasta)
    e = _encarte(con, encarte_id)
    assert json.loads(e["avisos"]) == ["validade não encontrada"]
    assert (e["validade_inicio"], e["validade_fim"]) == esperado
    (o,) = _ofertas(con)
    assert (o["validade_inicio"], o["validade_fim"]) == esperado


async def test_dia_do_recebimento_e_o_local(con, pasta):
    # 01:30 UTC de domingo ainda é sábado à noite em Medianeira.
    fonte = _fonte(con)
    recebido = datetime.fromisoformat("2026-11-22T01:30:00+00:00").astimezone(FUSO)
    encarte_id = _pendente(con, pasta, fonte, recebido=recebido)
    await _processar(con, LeitorFalso(_leitura(_oferta(), inicio=None, fim=None)), pasta)
    assert _encarte(con, encarte_id)["validade_fim"] == "2026-11-21"


async def test_aviso_vencido_ao_chegar(con, pasta):
    fonte = _fonte(con)
    encarte_id = _pendente(con, pasta, fonte)
    leitura = _leitura(_oferta(), inicio=date(2026, 10, 22), fim=date(2026, 10, 22))
    await _processar(con, LeitorFalso(leitura), pasta)
    e = _encarte(con, encarte_id)
    assert e["status"] == "lido"
    assert json.loads(e["avisos"]) == ["vencido ao chegar"]


async def test_observacoes_do_modelo_vao_como_vieram(con, pasta):
    fonte = _fonte(con)
    encarte_id = _pendente(con, pasta, fonte)
    texto = "Topo diz 22/11, rodapé diz 22/10."
    await _processar(con, LeitorFalso(_leitura(_oferta(), observacoes=texto)), pasta)
    e = _encarte(con, encarte_id)
    assert e["observacoes"] == texto
    assert e["avisos"] is None


async def test_fixture_real_do_max_ponta_a_ponta(con, pasta):
    """A leitura real do Max: 3 ofertas, a divergência nas observações, e vencido."""
    fonte = _fonte(con)
    recebido = datetime(2026, 11, 22, 8, 0, tzinfo=FUSO)
    encarte_id = _pendente(con, pasta, fonte, recebido=recebido)
    leitor = _leitor(httpx.Response(200, json=_fixture("max")))
    await encartes.processar_proximo(con, leitor, recebido + timedelta(minutes=1), pasta)
    e = _encarte(con, encarte_id)
    assert e["status"] == "lido"
    assert e["leitor"] == "gemini-teste"
    assert e["observacoes"]
    assert len(_ofertas(con)) == 3
    assert all(o["condicao"] for o in _ofertas(con))


# ── limpeza ──────────────────────────────────────────────────────────────────
DEPOIS = RECEBIDO + timedelta(days=8)


async def test_limpeza_apaga_imagem_velha_e_as_ofertas_ficam(con, pasta):
    fonte = _fonte(con)
    encarte_id = _pendente(con, pasta, fonte)
    await _processar(con, LeitorFalso(_leitura(_oferta())), pasta)
    arquivo = pasta.parent / _encarte(con, encarte_id)["arquivo"]
    assert arquivo.exists()

    assert encartes.limpar_imagens(con, pasta, DEPOIS) == 1
    assert not arquivo.exists()
    assert not arquivo.parent.exists()  # a pasta do dia, vazia, vai junto
    assert pasta.exists()
    assert _encarte(con, encarte_id)["arquivo"] is None
    assert len(_ofertas(con)) == 1
    # de novo, nada a fazer
    assert encartes.limpar_imagens(con, pasta, DEPOIS) == 0


async def test_limpeza_respeita_os_sete_dias(con, pasta):
    fonte = _fonte(con)
    encarte_id = _pendente(con, pasta, fonte, status="lido")
    assert encartes.limpar_imagens(con, pasta, RECEBIDO + timedelta(days=6, hours=23)) == 0
    assert _encarte(con, encarte_id)["arquivo"] is not None


async def test_limpeza_nao_mexe_em_pendente(con, pasta):
    fonte = _fonte(con)
    encarte_id = _pendente(con, pasta, fonte)
    assert encartes.limpar_imagens(con, pasta, DEPOIS) == 0
    e = _encarte(con, encarte_id)
    assert e["arquivo"] is not None
    assert (pasta.parent / e["arquivo"]).exists()


async def test_limpeza_nao_apaga_arquivo_ainda_usado(con, pasta):
    lar, outra = _fonte(con, 1), _fonte(con, 2)
    lido = _pendente(con, pasta, lar, nome="mesma-arte", status="lido")
    # A outra loja recebeu a mesma arte e ainda não leu: o arquivo é o mesmo.
    pendente = _pendente(con, pasta, outra, nome="mesma-arte")
    arquivo = pasta.parent / _encarte(con, pendente)["arquivo"]

    assert encartes.limpar_imagens(con, pasta, DEPOIS) == 0
    assert arquivo.exists()
    assert _encarte(con, lido)["arquivo"] is None
    assert _encarte(con, pendente)["arquivo"] is not None

    # Lida a outra, o arquivo some na limpeza seguinte.
    con.execute("UPDATE encarte SET status = 'lido' WHERE id = ?", (pendente,))
    con.commit()
    assert encartes.limpar_imagens(con, pasta, DEPOIS) == 1
    assert not arquivo.exists()


async def test_limpeza_com_arquivo_ja_ausente_nao_quebra(con, pasta):
    fonte = _fonte(con)
    encarte_id = _pendente(con, pasta, fonte, status="falhou")
    (pasta.parent / _encarte(con, encarte_id)["arquivo"]).unlink()
    assert encartes.limpar_imagens(con, pasta, DEPOIS) == 0
    assert _encarte(con, encarte_id)["arquivo"] is None


async def test_limpeza_mantem_pasta_do_dia_com_outros_arquivos(con, pasta):
    fonte = _fonte(con)
    velho = _pendente(con, pasta, fonte, nome="velho", status="lido")
    _pendente(con, pasta, fonte, nome="novo")  # mesmo dia, ainda pendente
    dia = (pasta.parent / _encarte(con, velho)["arquivo"]).parent
    assert encartes.limpar_imagens(con, pasta, DEPOIS) == 1
    assert dia.exists()


# ── laço ─────────────────────────────────────────────────────────────────────
async def _esperar(condicao, limite: float = 2.0):
    fim = asyncio.get_running_loop().time() + limite
    while not condicao():
        assert asyncio.get_running_loop().time() < fim, "o laço não chegou lá"
        await asyncio.sleep(0.01)


async def test_laco_le_os_pendentes_e_acorda_com_o_evento(con, pasta):
    fonte = _fonte(con)
    _pendente(con, pasta, fonte, nome="um")
    _pendente(con, pasta, fonte, nome="dois")
    leitor = LeitorFalso(_leitura(_oferta()))
    evento = asyncio.Event()
    tarefa = asyncio.create_task(
        tarefas.laco_de_encartes(con, leitor, pasta, evento, pausa=0, espera=30)
    )
    try:
        await _esperar(lambda: len(leitor.chamadas) == 2)
        # Chega outro; o evento acorda o laço sem esperar os 30 s.
        _pendente(con, pasta, fonte, nome="tres")
        evento.set()
        await _esperar(lambda: len(leitor.chamadas) == 3)
    finally:
        tarefa.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tarefa
    assert {r[0] for r in con.execute("SELECT status FROM encarte")} == {"lido"}


async def test_laco_sobrevive_a_erro_inesperado(con, pasta, monkeypatch):
    chamadas = []

    async def quebra(*args):
        chamadas.append(1)
        raise RuntimeError("banco sumiu")

    monkeypatch.setattr(encartes, "processar_proximo", quebra)
    evento = asyncio.Event()
    tarefa = asyncio.create_task(
        tarefas.laco_de_encartes(con, LeitorFalso(_leitura()), pasta, evento, pausa=0, espera=30)
    )
    try:
        await _esperar(lambda: len(chamadas) == 1)
        evento.set()
        await _esperar(lambda: len(chamadas) == 2)
        assert not tarefa.done()
    finally:
        tarefa.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tarefa


# ── ciclo de vida ────────────────────────────────────────────────────────────
def _laco_de_encartes_subiu(app) -> bool:
    return any(t.get_name() == "laco_de_encartes" for t in app.state.tarefas)


def test_laco_nao_sobe_em_demo(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", CHAVE)
    with TestClient(_app(tmp_path, monkeypatch, app_mode="demo")) as c:
        assert not _laco_de_encartes_subiu(c.app)
        assert isinstance(c.app.state.evento_encartes, asyncio.Event)


def test_laco_nao_sobe_sem_chave(tmp_path, monkeypatch, caplog):
    logger = logging.getLogger("mise_en_prato.main")
    logger.addHandler(caplog.handler)
    try:
        with TestClient(_app(tmp_path, monkeypatch)) as c:
            assert not _laco_de_encartes_subiu(c.app)
    finally:
        logger.removeHandler(caplog.handler)
    assert "GEMINI_API_KEY vazia" in caplog.text
    assert "pendentes" in caplog.text


def test_laco_sobe_com_chave(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", CHAVE)
    with TestClient(_app(tmp_path, monkeypatch)) as c:
        assert _laco_de_encartes_subiu(c.app)


def test_webhook_guardado_sinaliza_o_evento(tmp_path, monkeypatch):
    with TestClient(_app(tmp_path, monkeypatch)) as c:
        encartes.adicionar_fonte(c.app.state.con, GRUPO_LAR, "LAR Centro")
        evento = c.app.state.evento_encartes
        assert _enviar(c, _payload(tipo="conversation")).json() == {"resultado": "ignorado_tipo"}
        assert not evento.is_set()
        assert _enviar(c, _payload(), segredo=SEGREDO).json() == {"resultado": "guardado"}
        assert evento.is_set()
