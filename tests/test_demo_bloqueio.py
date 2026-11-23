"""No modo demo, nada sai da casa: nem SEFAZ, nem Menor Preço (AGENTS.md §3).

Cada rota que fala com o portal do estado ou com a API do Menor Preço responde
403 com a mensagem da demonstração, ANTES de abrir sessão de captcha, gravar no
banco ou tocar a rede. As portas de rede estão todas trocadas por armadilhas:
se alguma for chamada, o teste quebra.
"""

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from mise_en_prato.config import config

FIXTURE = Path(__file__).parent / "fixtures" / "cupom_lar_2026-08-14.html"
CHAVE = "41260876430438001224650010002137971000839549"  # 44 dígitos, só formato


def _app(tmp_path, monkeypatch, *, app_mode: str = "dev"):
    monkeypatch.setenv("DADOS", str(tmp_path))
    monkeypatch.setenv("EXIGIR_TAILNET", "false")
    monkeypatch.setenv("APP_MODE", app_mode)
    config.cache_clear()
    from mise_en_prato.main import app

    return app


@pytest.fixture
def armadilhas(monkeypatch):
    """Troca toda porta de saída por uma que reprova o teste se for usada."""
    from mise_en_prato import main, menor_preco

    chamadas = []

    def armadilha(nome):
        def chamada(*args, **kwargs):
            chamadas.append(nome)
            raise AssertionError(f"{nome} foi chamado no modo demo")

        return chamada

    for nome in ("buscar", "abrir_desafio", "consultar", "ler_qr", "interpretar_cupom_ocr"):
        monkeypatch.setattr(main, nome, armadilha(f"main.{nome}"))
    monkeypatch.setattr(menor_preco, "MenorPreco", armadilha("MenorPreco"))
    monkeypatch.setattr(httpx, "get", armadilha("httpx.get"))
    monkeypatch.setattr(httpx, "Client", armadilha("httpx.Client"))
    monkeypatch.setattr("urllib.request.urlopen", armadilha("urlopen"))
    return chamadas


@pytest.fixture
def demo(tmp_path, monkeypatch, armadilhas):
    with TestClient(_app(tmp_path, monkeypatch, app_mode="demo")) as c:
        yield c
    config.cache_clear()


def _contagens(con) -> dict[str, int]:
    """Linhas de cada tabela do banco: o retrato para provar que nada foi gravado."""
    tabelas = [
        r[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    return {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tabelas}


# (método, caminho, kwargs do pedido, trecho esperado na mensagem)
SEFAZ = "Consulta de notas na SEFAZ desativada na demonstração pública."
MENOR_PRECO = "Consulta ao Menor Preço desativada na demonstração pública."
ROTAS_BLOQUEADAS = [
    pytest.param(
        "post",
        "/api/cupom",
        {"json": {"url": f"https://www.fazenda.pr.gov.br/nfce/qrcode?p={CHAVE}|2|1|1|abc"}},
        SEFAZ,
        id="cupom-qr",
    ),
    pytest.param(
        "post", "/api/cupom", {"json": {"chave": CHAVE}}, SEFAZ, id="cupom-chave-guardada"
    ),
    pytest.param(
        "post",
        "/api/cupom/foto",
        {"files": {"foto": ("qr.jpg", b"nao-e-imagem", "image/jpeg")}},
        SEFAZ,
        id="cupom-foto",
    ),
    pytest.param("get", "/api/chave/desafio", {}, SEFAZ, id="captcha"),
    pytest.param(
        "post",
        "/api/cupom/chave",
        {"json": {"chave": CHAVE, "desafio": "x", "captcha": "abc", "reconsultar": True}},
        SEFAZ,
        id="cupom-pela-chave",
    ),
    pytest.param("post", "/api/lista/1/pesquisar", {}, MENOR_PRECO, id="lista-pesquisar"),
    pytest.param("get", "/api/produto/1/candidatos", {}, MENOR_PRECO, id="candidatos-gtin"),
    pytest.param("post", "/api/enriquecer?quantos=5", {}, MENOR_PRECO, id="enriquecer"),
]


@pytest.mark.parametrize(("metodo", "caminho", "pedido", "mensagem"), ROTAS_BLOQUEADAS)
def test_rota_externa_bloqueada_no_demo(demo, armadilhas, metodo, caminho, pedido, mensagem):
    from mise_en_prato import main

    antes = _contagens(demo.app.state.con)
    desafios_antes = dict(main._desafios)
    r = getattr(demo, metodo)(caminho, **pedido)

    assert r.status_code == 403
    assert r.json()["detail"] == mensagem
    assert armadilhas == []
    assert _contagens(demo.app.state.con) == antes
    assert main._desafios == desafios_antes
    assert not list(config().pasta_cupons.glob("*.html"))
    assert not (config().dados / "falhas").exists()


def test_upload_de_html_continua_no_demo(demo, armadilhas):
    """O HTML que o morador baixou do portal é lido aqui dentro, sem rede.

    Não é raspagem: quem foi à SEFAZ foi a pessoa, no navegador dela. A rota só
    interpreta o arquivo enviado, por isso fica aberta no demo.
    """
    corpo = FIXTURE.read_bytes()
    r = demo.post("/api/cupom/html", files={"arquivo": ("nota.html", corpo, "text/html")})

    assert r.status_code == 200, r.text
    assert r.json()["cupom"]["chave"]
    assert armadilhas == []


def test_fora_do_demo_a_rota_nao_e_barrada(tmp_path, monkeypatch, armadilhas):
    """Em dev o guarda não age: o pedido chega à porta de rede (aqui, a armadilha)."""
    with (
        TestClient(_app(tmp_path, monkeypatch, app_mode="dev"), raise_server_exceptions=False) as c,
    ):
        r = c.get("/api/chave/desafio")
    config.cache_clear()

    assert r.status_code != 403
    assert armadilhas == ["main.abrir_desafio"]
