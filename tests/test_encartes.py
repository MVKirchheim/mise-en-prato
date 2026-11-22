"""Webhook de encartes: só imagem de fonte cadastrada é guardada; o resto some."""

import base64
import hashlib
import io
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from mise_en_prato import encartes
from mise_en_prato.banco import conectar
from mise_en_prato.config import config

FIXTURES = Path(__file__).parent / "fixtures" / "encartes"
LAR = (FIXTURES / "lar_2026-11-21_super_promocao.png").read_bytes()
MAX = (FIXTURES / "max_2026-11-22_esquenta_black_friday.png").read_bytes()

SEGREDO = "segredo-de-teste"
GRUPO_LAR = "120363000000000001@g.us"
GRUPO_ESTRANHO = "120363000000000999@g.us"
CONVERSA = "5545999999999@s.whatsapp.net"
# 2026-11-22 01:30 UTC = 2026-11-21 22:30 em Medianeira: o dia é o de lá.
MOMENTO = int(datetime(2026, 11, 22, 1, 30, tzinfo=UTC).timestamp())
AGORA = datetime(2026, 11, 22, 9, 0, tzinfo=encartes.FUSO)


def _app(tmp_path, monkeypatch, *, app_mode: str = "dev", segredo: str = SEGREDO):
    monkeypatch.setenv("DADOS", str(tmp_path))
    monkeypatch.setenv("EXIGIR_TAILNET", "false")
    monkeypatch.setenv("APP_MODE", app_mode)
    monkeypatch.setenv("ENCARTE_WEBHOOK_SEGREDO", segredo)
    config.cache_clear()
    from mise_en_prato.main import app

    return app


@pytest.fixture
def cliente(tmp_path, monkeypatch):
    with TestClient(_app(tmp_path, monkeypatch)) as c:
        yield c
    config.cache_clear()


@pytest.fixture
def con(tmp_path):
    con = conectar(tmp_path / "mise_en_prato.db")
    yield con
    con.close()


def _payload(
    *,
    jid: str = GRUPO_LAR,
    tipo: str = "imageMessage",
    mensagem_id: str = "3EB0AAA",
    imagem: bytes | None = LAR,
    legenda: str | None = "Super Promoção LAR",
    evento: str = "messages.upsert",
    proprio: bool = False,
) -> dict:
    if tipo == "imageMessage":
        mensagem = {
            "imageMessage": {
                "url": "https://mmg.whatsapp.net/x",
                "mimetype": "image/jpeg",
                "fileLength": "123456",
                "height": 1280,
                "width": 960,
            }
        }
        if legenda is not None:
            mensagem["imageMessage"]["caption"] = legenda
        if imagem is not None:
            mensagem["base64"] = base64.b64encode(imagem).decode()
    elif tipo == "stickerMessage":
        mensagem = {"stickerMessage": {"mimetype": "image/webp"}}
    else:
        mensagem = {"conversation": "oi, tudo bem?"}
    return {
        "event": evento,
        "instance": "minha",
        "data": {
            "key": {
                "remoteJid": jid,
                "fromMe": proprio,
                "id": mensagem_id,
                "participant": "5545988888888@s.whatsapp.net",
            },
            "pushName": "Fulano",
            "status": "DELIVERY_ACK",
            "message": mensagem,
            "messageType": tipo,
            "messageTimestamp": MOMENTO,
            "instanceId": "uuid",
            "source": "android",
            "campo_novo_da_evolution": {"qualquer": "coisa"},
        },
        "destination": "http://localhost:8090/api/encartes/webhook",
        "date_time": "2026-11-21T22:30:00.000-03:00",
        "sender": "5545977777777@s.whatsapp.net",
        "server_url": "http://evolution",
        "apikey": "token",
    }


def _enviar(cliente, payload, segredo: str | None = SEGREDO):
    cabecalhos = {"X-Webhook-Segredo": segredo} if segredo is not None else {}
    return cliente.post("/api/encartes/webhook", json=payload, headers=cabecalhos)


def _cadastrar_lar(cliente) -> int:
    return encartes.adicionar_fonte(cliente.app.state.con, GRUPO_LAR, "LAR Centro")


def _contagens(con) -> dict:
    return {
        t: con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("encarte", "oferta", "encarte_grupo_visto", "encarte_fonte")
    }


def _arquivos(pasta: Path) -> list[Path]:
    return [p for p in pasta.rglob("*") if p.is_file()] if pasta.exists() else []


# ── autenticação e modo ──────────────────────────────────────────────────────
@pytest.mark.parametrize("segredo", [None, "", "errado"])
def test_segredo_errado_ou_ausente_401(cliente, segredo):
    _cadastrar_lar(cliente)
    r = _enviar(cliente, _payload(), segredo=segredo)
    assert r.status_code == 401
    assert _contagens(cliente.app.state.con)["encarte"] == 0


def test_segredo_nao_configurado_403(tmp_path, monkeypatch):
    with TestClient(_app(tmp_path, monkeypatch, segredo="")) as c:
        r = _enviar(c, _payload(), segredo="")
        assert r.status_code == 403
        assert "não configurado" in r.json()["detail"]
    config.cache_clear()


def test_demo_403(tmp_path, monkeypatch):
    with TestClient(_app(tmp_path, monkeypatch, app_mode="demo")) as c:
        r = _enviar(c, _payload())
        assert r.status_code == 403
        assert "demonstração" in r.json()["detail"].lower()
    config.cache_clear()


# ── descartes ────────────────────────────────────────────────────────────────
def test_evento_que_nao_e_upsert(cliente):
    _cadastrar_lar(cliente)
    r = _enviar(cliente, _payload(evento="messages.update"))
    assert r.json() == {"resultado": "ignorado_evento"}
    assert _contagens(cliente.app.state.con)["encarte"] == 0


def test_mensagem_propria_ignorada(cliente):
    _cadastrar_lar(cliente)
    r = _enviar(cliente, _payload(proprio=True))
    assert r.json() == {"resultado": "ignorado_proprio"}
    assert _contagens(cliente.app.state.con)["encarte"] == 0


def test_conversa_pessoal_nao_deixa_rastro(cliente, tmp_path, caplog):
    caplog.set_level(logging.INFO, logger="mise_en_prato.encartes")
    logging.getLogger("mise_en_prato.encartes").addHandler(caplog.handler)
    try:
        r = _enviar(cliente, _payload(jid=CONVERSA, legenda="foto da família"))
    finally:
        logging.getLogger("mise_en_prato.encartes").removeHandler(caplog.handler)
    assert r.status_code == 200
    assert r.json() == {"resultado": "ignorado_nao_grupo"}
    assert _contagens(cliente.app.state.con) == {
        "encarte": 0,
        "oferta": 0,
        "encarte_grupo_visto": 0,
        "encarte_fonte": 0,
    }
    assert _arquivos(tmp_path / "encartes") == []
    texto = caplog.text
    assert "ignorado_nao_grupo" in texto
    for pessoal in ("5545", "Fulano", "família"):
        assert pessoal not in texto


def test_grupo_desconhecido_so_entra_na_descoberta(cliente, tmp_path, caplog):
    con = cliente.app.state.con
    caplog.set_level(logging.INFO, logger="mise_en_prato.encartes")
    logging.getLogger("mise_en_prato.encartes").addHandler(caplog.handler)
    try:
        r = _enviar(cliente, _payload(jid=GRUPO_ESTRANHO))
        assert r.json() == {"resultado": "grupo_desconhecido"}
        r = _enviar(cliente, _payload(jid=GRUPO_ESTRANHO, mensagem_id="3EB0BBB", imagem=MAX))
        assert r.json() == {"resultado": "grupo_desconhecido"}
    finally:
        logging.getLogger("mise_en_prato.encartes").removeHandler(caplog.handler)

    vistos = encartes.listar_grupos_vistos(con)
    assert len(vistos) == 1
    assert vistos[0]["grupo_jid"] == GRUPO_ESTRANHO
    assert vistos[0]["imagens"] == 2
    assert vistos[0]["primeira_vez"] <= vistos[0]["ultima_vez"]
    # Só o jid e as datas: nada da mensagem vira coluna.
    colunas = {c[1] for c in con.execute("PRAGMA table_info(encarte_grupo_visto)")}
    assert colunas == {"grupo_jid", "primeira_vez", "ultima_vez", "imagens"}
    assert _contagens(con)["encarte"] == 0
    assert _arquivos(tmp_path / "encartes") == []
    assert GRUPO_ESTRANHO not in caplog.text
    assert "Super Promoção" not in caplog.text


def test_texto_de_grupo_desconhecido_nao_registra_nada(cliente):
    r = _enviar(cliente, _payload(jid=GRUPO_ESTRANHO, tipo="conversation"))
    assert r.json() == {"resultado": "grupo_desconhecido"}
    assert _contagens(cliente.app.state.con)["encarte_grupo_visto"] == 0


@pytest.mark.parametrize("tipo", ["conversation", "stickerMessage"])
def test_fonte_cadastrada_texto_e_figurinha_ignorados(cliente, tmp_path, tipo):
    _cadastrar_lar(cliente)
    r = _enviar(cliente, _payload(tipo=tipo))
    assert r.json() == {"resultado": "ignorado_tipo"}
    assert _contagens(cliente.app.state.con)["encarte"] == 0
    assert _arquivos(tmp_path / "encartes") == []


def test_fonte_inativa_e_tratada_como_desconhecida(cliente, tmp_path):
    con = cliente.app.state.con
    fonte_id = _cadastrar_lar(cliente)
    assert encartes.desativar_fonte(con, fonte_id)
    r = _enviar(cliente, _payload())
    assert r.json() == {"resultado": "grupo_desconhecido"}
    assert _contagens(con)["encarte"] == 0
    assert [g["grupo_jid"] for g in encartes.listar_grupos_vistos(con)] == [GRUPO_LAR]
    assert _arquivos(tmp_path / "encartes") == []


# ── caminho feliz e repetições ───────────────────────────────────────────────
def test_caminho_feliz_guarda_imagem_pendente(cliente, tmp_path, caplog):
    con = cliente.app.state.con
    fonte_id = _cadastrar_lar(cliente)
    caplog.set_level(logging.INFO, logger="mise_en_prato.encartes")
    logging.getLogger("mise_en_prato.encartes").addHandler(caplog.handler)
    try:
        r = _enviar(cliente, _payload())
    finally:
        logging.getLogger("mise_en_prato.encartes").removeHandler(caplog.handler)
    assert r.status_code == 200
    assert r.json() == {"resultado": "guardado"}

    linha = con.execute("SELECT * FROM encarte").fetchone()
    sha = hashlib.sha256(LAR).hexdigest()
    assert linha["fonte_id"] == fonte_id
    assert linha["mensagem_id"] == "3EB0AAA"
    assert linha["status"] == "pendente"
    assert linha["tentativas"] == 0
    assert linha["legenda"] == "Super Promoção LAR"
    assert linha["sha256"] == sha
    assert linha["mime"] == "image/png"  # o conteúdo manda, não o mimetype declarado
    assert linha["recebido_em"] == "2026-11-21T22:30:00-03:00"
    assert linha["arquivo"] == f"encartes/2026-11-21/{sha}.png"
    arquivo = tmp_path / linha["arquivo"]
    assert arquivo.read_bytes() == LAR
    assert hashlib.sha256(arquivo.read_bytes()).hexdigest() == sha
    # sem temporário esquecido ao lado
    assert _arquivos(tmp_path / "encartes") == [arquivo]
    assert f"guardado (LAR Centro, encarte {linha['id']})" in caplog.text


def test_sem_legenda_grava_nulo(cliente):
    _cadastrar_lar(cliente)
    _enviar(cliente, _payload(legenda="   "))
    assert cliente.app.state.con.execute("SELECT legenda FROM encarte").fetchone()[0] is None


def test_mesmo_id_repetido_grava_uma_vez(cliente):
    _cadastrar_lar(cliente)
    assert _enviar(cliente, _payload()).json() == {"resultado": "guardado"}
    assert _enviar(cliente, _payload()).json() == {"resultado": "repetido"}
    assert _contagens(cliente.app.state.con)["encarte"] == 1


def test_mesma_imagem_com_outro_id_e_repetida(cliente):
    _cadastrar_lar(cliente)
    assert _enviar(cliente, _payload()).json() == {"resultado": "guardado"}
    r = _enviar(cliente, _payload(mensagem_id="3EB0OUTRO"))
    assert r.json() == {"resultado": "repetido"}
    assert _contagens(cliente.app.state.con)["encarte"] == 1


def test_outra_imagem_da_mesma_fonte_e_guardada(cliente, tmp_path):
    _cadastrar_lar(cliente)
    _enviar(cliente, _payload())
    assert _enviar(cliente, _payload(mensagem_id="3EB0MAX", imagem=MAX)).json() == {
        "resultado": "guardado"
    }
    assert _contagens(cliente.app.state.con)["encarte"] == 2
    assert len(_arquivos(tmp_path / "encartes")) == 2


def test_mesma_arte_em_duas_fontes_divide_o_arquivo(cliente, tmp_path):
    con = cliente.app.state.con
    _cadastrar_lar(cliente)
    outro = "120363000000000002@g.us"
    encartes.adicionar_fonte(con, outro, "LAR São Cristóvão")
    assert _enviar(cliente, _payload()).json() == {"resultado": "guardado"}
    assert _enviar(cliente, _payload(jid=outro)).json() == {"resultado": "guardado"}
    caminhos = {r[0] for r in con.execute("SELECT arquivo FROM encarte")}
    assert len(caminhos) == 1
    assert len(_arquivos(tmp_path / "encartes")) == 1


def test_sem_base64_vira_falha_sem_arquivo(cliente, tmp_path):
    _cadastrar_lar(cliente)
    r = _enviar(cliente, _payload(imagem=None))
    assert r.json() == {"resultado": "sem_midia"}
    linha = cliente.app.state.con.execute("SELECT * FROM encarte").fetchone()
    assert linha["status"] == "falhou"
    assert linha["erro"] == "midia_ausente"
    assert linha["arquivo"] is None
    assert linha["sha256"] is None
    assert linha["legenda"] == "Super Promoção LAR"
    assert _arquivos(tmp_path / "encartes") == []
    # a mesma mensagem reenviada não duplica a falha
    assert _enviar(cliente, _payload(imagem=None)).json() == {"resultado": "repetido"}


def test_bytes_que_nao_sao_imagem_nao_gravam_nada(cliente, tmp_path):
    _cadastrar_lar(cliente)
    falso = b"\xff\xd8\xff\xe0" + b"isto nao e um jpeg de verdade" * 100
    r = _enviar(cliente, _payload(imagem=falso))
    assert r.json() == {"resultado": "imagem_invalida"}
    assert _contagens(cliente.app.state.con)["encarte"] == 0
    assert _arquivos(tmp_path / "encartes") == []


def test_formato_fora_da_lista_e_invalido(cliente):
    _cadastrar_lar(cliente)
    gif = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(gif, format="GIF")
    r = _enviar(cliente, _payload(imagem=gif.getvalue()))
    assert r.json() == {"resultado": "imagem_invalida"}


def test_jpeg_e_webp_reais_sao_aceitos(cliente, tmp_path):
    _cadastrar_lar(cliente)
    for i, (formato, mime, ext) in enumerate(
        [("JPEG", "image/jpeg", "jpg"), ("WEBP", "image/webp", "webp")]
    ):
        saida = io.BytesIO()
        Image.new("RGB", (16, 16), (i * 80, 10, 10)).save(saida, format=formato)
        r = _enviar(cliente, _payload(mensagem_id=f"3EB0{formato}", imagem=saida.getvalue()))
        assert r.json() == {"resultado": "guardado"}
        linha = cliente.app.state.con.execute(
            "SELECT mime, arquivo FROM encarte WHERE mensagem_id = ?", (f"3EB0{formato}",)
        ).fetchone()
        assert linha["mime"] == mime
        assert linha["arquivo"].endswith(f".{ext}")
        assert (tmp_path / linha["arquivo"]).exists()


def test_imagem_acima_de_10mb_nao_grava_nada(cliente, tmp_path):
    _cadastrar_lar(cliente)
    grande = LAR + b"\0" * (encartes.LIMITE_IMAGEM + 1 - len(LAR))
    r = _enviar(cliente, _payload(imagem=grande))
    assert r.json() == {"resultado": "grande_demais"}
    assert _contagens(cliente.app.state.con)["encarte"] == 0
    assert _arquivos(tmp_path / "encartes") == []


def test_corpo_acima_de_20mb_nem_e_lido(cliente):
    _cadastrar_lar(cliente)
    corpo = b"{" + b" " * (21 * 1024 * 1024) + b"}"
    r = cliente.post(
        "/api/encartes/webhook",
        content=corpo,
        headers={"X-Webhook-Segredo": SEGREDO, "Content-Type": "application/json"},
    )
    assert r.status_code == 200
    assert r.json() == {"resultado": "grande_demais"}


# ── payload torto ────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "payload",
    [
        {"event": "messages.upsert"},
        {"event": "messages.upsert", "data": []},
        {"event": "messages.upsert", "data": {"message": {}}},
        {"event": "messages.upsert", "data": {"key": "texto"}},
        {"event": "messages.upsert", "data": {"key": {"remoteJid": 123}}},
        [1, 2, 3],
        "texto",
        None,
    ],
)
def test_payload_malformado_responde_200(cliente, payload):
    _cadastrar_lar(cliente)
    r = _enviar(cliente, payload)
    assert r.status_code == 200
    assert r.json() == {"resultado": "malformado"}


def test_json_invalido_responde_200(cliente):
    r = cliente.post(
        "/api/encartes/webhook",
        content=b"{isto nao e json",
        headers={"X-Webhook-Segredo": SEGREDO, "Content-Type": "application/json"},
    )
    assert r.status_code == 200
    assert r.json() == {"resultado": "malformado"}


def test_fonte_cadastrada_com_campos_tortos_nao_quebra(cliente):
    _cadastrar_lar(cliente)
    sem_id = _payload()
    del sem_id["data"]["key"]["id"]
    assert _enviar(cliente, sem_id).json() == {"resultado": "malformado"}

    mensagem_lista = _payload()
    mensagem_lista["data"]["message"] = ["x"]
    assert _enviar(cliente, mensagem_lista).json() == {"resultado": "malformado"}

    base64_numero = _payload(mensagem_id="3EB0NUM")
    base64_numero["data"]["message"]["base64"] = 42
    assert _enviar(cliente, base64_numero).json() == {"resultado": "sem_midia"}

    legenda_e_hora_tortas = _payload(mensagem_id="3EB0TORTO")
    legenda_e_hora_tortas["data"]["message"]["imageMessage"] = "não é dict"
    legenda_e_hora_tortas["data"]["messageTimestamp"] = {"low": 1, "high": 0}
    assert _enviar(cliente, legenda_e_hora_tortas).json() == {"resultado": "guardado"}
    linha = cliente.app.state.con.execute(
        "SELECT legenda, recebido_em FROM encarte WHERE mensagem_id = '3EB0TORTO'"
    ).fetchone()
    assert linha["legenda"] is None
    assert linha["recebido_em"].endswith("-03:00")


def test_timestamp_em_texto_e_aceito(con, tmp_path):
    encartes.adicionar_fonte(con, GRUPO_LAR, "LAR Centro")
    payload = _payload()
    payload["data"]["messageTimestamp"] = str(MOMENTO)
    assert encartes.processar_webhook(con, payload, tmp_path / "encartes", AGORA) == "guardado"
    recebido = con.execute("SELECT recebido_em FROM encarte").fetchone()[0]
    assert recebido == "2026-11-21T22:30:00-03:00"


def test_sem_timestamp_vale_a_hora_da_chegada(con, tmp_path):
    encartes.adicionar_fonte(con, GRUPO_LAR, "LAR Centro")
    payload = _payload()
    del payload["data"]["messageTimestamp"]
    assert encartes.processar_webhook(con, payload, tmp_path / "encartes", AGORA) == "guardado"
    linha = con.execute("SELECT recebido_em, arquivo FROM encarte").fetchone()
    assert linha["recebido_em"] == "2026-11-22T09:00:00-03:00"
    assert linha["arquivo"].startswith("encartes/2026-11-22/")


# ── falha de banco depois do arquivo ─────────────────────────────────────────
def test_falha_do_banco_nao_deixa_arquivo_orfao(con, tmp_path, monkeypatch):
    encartes.adicionar_fonte(con, GRUPO_LAR, "LAR Centro")

    def quebrar(*_args, **_kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(encartes, "_inserir_encarte", quebrar)
    pasta = tmp_path / "encartes"
    with pytest.raises(sqlite3.OperationalError):
        encartes.processar_webhook(con, _payload(), pasta, AGORA)
    assert _arquivos(pasta) == []
    assert con.execute("SELECT COUNT(*) FROM encarte").fetchone()[0] == 0


def test_falha_do_banco_vira_500_para_a_evolution_reenviar(tmp_path, monkeypatch):
    def quebrar(*_args, **_kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    app = _app(tmp_path, monkeypatch)
    with TestClient(app, raise_server_exceptions=False) as c:
        _cadastrar_lar(c)
        monkeypatch.setattr(encartes, "_inserir_encarte", quebrar)
        r = _enviar(c, _payload())
        assert r.status_code == 500
        assert _arquivos(tmp_path / "encartes") == []
    config.cache_clear()


def test_falha_do_banco_nao_apaga_arquivo_de_outra_fonte(con, tmp_path, monkeypatch):
    pasta = tmp_path / "encartes"
    encartes.adicionar_fonte(con, GRUPO_LAR, "LAR Centro")
    outro = "120363000000000002@g.us"
    encartes.adicionar_fonte(con, outro, "LAR São Cristóvão")
    assert encartes.processar_webhook(con, _payload(), pasta, AGORA) == "guardado"
    [arquivo] = _arquivos(pasta)

    def quebrar(*_args, **_kwargs):
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(encartes, "_inserir_encarte", quebrar)
    with pytest.raises(sqlite3.OperationalError):
        encartes.processar_webhook(con, _payload(jid=outro), pasta, AGORA)
    assert arquivo.read_bytes() == LAR


# ── fontes ───────────────────────────────────────────────────────────────────
def test_adicionar_fonte_tira_da_descoberta(con, tmp_path):
    pasta = tmp_path / "encartes"
    payload = _payload(jid=GRUPO_ESTRANHO)
    assert encartes.processar_webhook(con, payload, pasta, AGORA) == "grupo_desconhecido"
    assert len(encartes.listar_grupos_vistos(con)) == 1

    fonte_id = encartes.adicionar_fonte(con, f"  {GRUPO_ESTRANHO} ", "Max Atacadista")
    assert encartes.listar_grupos_vistos(con) == []
    [fonte] = encartes.listar_fontes(con)
    assert fonte["id"] == fonte_id
    assert fonte["grupo_jid"] == GRUPO_ESTRANHO
    assert fonte["nome"] == "Max Atacadista"
    assert fonte["ativo"] is True
    assert fonte["cnpj"] is None
    assert encartes.processar_webhook(con, payload, pasta, AGORA) == "guardado"


@pytest.mark.parametrize("jid", ["", "@g.us", CONVERSA, "120363000000000001", "abc@g.us.x"])
def test_jid_que_nao_e_de_grupo_e_recusado(con, jid):
    with pytest.raises(ValueError, match="@g.us"):
        encartes.adicionar_fonte(con, jid, "Loja")
    assert encartes.listar_fontes(con) == []


def test_fonte_sem_nome_e_recusada(con):
    with pytest.raises(ValueError):
        encartes.adicionar_fonte(con, GRUPO_LAR, "  ")


def test_cnpj_desconhecido_e_recusado(con):
    with pytest.raises(ValueError, match="cnpj"):
        encartes.adicionar_fonte(con, GRUPO_LAR, "LAR Centro", cnpj="00.000.000/0001-00")
    assert encartes.listar_fontes(con) == []


def test_cnpj_conhecido_e_aceito(con):
    con.execute(
        "INSERT INTO estabelecimento (cnpj, razao) VALUES ('76123456000199', 'COOPERATIVA LAR')"
    )
    con.commit()
    encartes.adicionar_fonte(con, GRUPO_LAR, "LAR Centro", cnpj="76.123.456/0001-99")
    assert encartes.listar_fontes(con)[0]["cnpj"] == "76123456000199"


def test_desativar_por_id_e_por_jid_e_reativar(con):
    a = encartes.adicionar_fonte(con, GRUPO_LAR, "LAR Centro")
    b = encartes.adicionar_fonte(con, GRUPO_ESTRANHO, "Max")
    assert encartes.desativar_fonte(con, str(a))
    assert encartes.desativar_fonte(con, GRUPO_ESTRANHO)
    assert [f["ativo"] for f in encartes.listar_fontes(con)] == [False, False]
    assert not encartes.desativar_fonte(con, 999)
    assert not encartes.desativar_fonte(con, "120363000000000777@g.us")
    with pytest.raises(ValueError):
        encartes.desativar_fonte(con, "nao-e-jid")

    # cadastrar de novo reativa a mesma fonte, sem duplicar
    assert encartes.adicionar_fonte(con, GRUPO_ESTRANHO, "Max Atacadista") == b
    fontes = encartes.listar_fontes(con)
    assert len(fontes) == 2
    assert fontes[1]["ativo"] is True
    assert fontes[1]["nome"] == "Max Atacadista"


def test_grupos_vistos_mais_recente_primeiro(con, tmp_path):
    pasta = tmp_path / "encartes"
    cedo = datetime(2026, 11, 20, 8, 0, tzinfo=encartes.FUSO)
    encartes.processar_webhook(con, _payload(jid=GRUPO_ESTRANHO), pasta, cedo)
    encartes.processar_webhook(con, _payload(jid=GRUPO_LAR), pasta, AGORA)
    assert [g["grupo_jid"] for g in encartes.listar_grupos_vistos(con)] == [
        GRUPO_LAR,
        GRUPO_ESTRANHO,
    ]


def test_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("DADOS", str(tmp_path))
    config.cache_clear()
    try:
        assert encartes.main(["adicionar", GRUPO_LAR, "LAR Centro"]) == 0
        assert encartes.main(["fontes"]) == 0
        saida = capsys.readouterr().out
        assert "LAR Centro" in saida
        assert GRUPO_LAR in saida
        assert "ativa" in saida

        assert encartes.main(["adicionar", CONVERSA, "Pessoa"]) == 1
        assert "@g.us" in capsys.readouterr().out

        assert encartes.main(["desativar", GRUPO_LAR]) == 0
        assert encartes.main(["desativar", "999"]) == 1
        assert encartes.main(["fontes"]) == 0
        assert "inativa" in capsys.readouterr().out

        assert encartes.main(["grupos"]) == 0
        assert "Nenhum grupo" in capsys.readouterr().out
    finally:
        config.cache_clear()
