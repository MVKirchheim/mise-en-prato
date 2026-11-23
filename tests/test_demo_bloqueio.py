"""O modo demo é somente leitura e não sai da casa (AGENTS.md §3).

- Toda escrita sob /api/ (POST/PUT/PATCH/DELETE) responde 403 na porta, pelo
  middleware, antes de gravar no banco ou em disco. O teste enumera as rotas de
  `app.routes`: rota nova já entra na conta sem ninguém lembrar.
- Os GET que saem da casa (SEFAZ, Menor Preço, Open Food Facts) respondem 403.
- Segunda trava: os próprios clientes de rede levantam ModoDemo no demo, e o
  main.py a traduz em 403 — rota que esquecer o guarda ainda não sai da casa.
- A tarefa de backup não sobe no demo.

As portas de rede estão todas trocadas por armadilhas: se alguma for chamada,
o teste quebra.
"""

import re
from pathlib import Path

import httpx
import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from mise_en_prato.config import ModoDemo, config

CHAVE = "41260876430438001224650010002137971000839549"  # 44 dígitos, só formato
GTIN = "7891000100103"
ALTERACOES = "Alterações desativadas na demonstração pública."
SEFAZ = "Consulta de notas na SEFAZ desativada na demonstração pública."
MENOR_PRECO = "Consulta ao Menor Preço desativada na demonstração pública."
OPEN_FOOD_FACTS = "Consulta ao Open Food Facts desativada na demonstração pública."


def _app(tmp_path, monkeypatch, *, app_mode: str = "dev"):
    monkeypatch.setenv("DADOS", str(tmp_path))
    monkeypatch.setenv("EXIGIR_TAILNET", "false")
    monkeypatch.setenv("APP_MODE", app_mode)
    config.cache_clear()
    from mise_en_prato.main import app

    return app


def _armar(monkeypatch, chamadas: list, alvos) -> None:
    def armadilha(nome):
        def chamada(*args, **kwargs):
            chamadas.append(nome)
            raise AssertionError(f"{nome} foi chamado no modo demo")

        return chamada

    for modulo, nome in alvos:
        monkeypatch.setattr(modulo, nome, armadilha(f"{modulo.__name__}.{nome}"))


def _armar_rede(monkeypatch, chamadas: list) -> None:
    """Só a rede de verdade: o que os clientes usariam por baixo."""
    import urllib.request

    _armar(monkeypatch, chamadas, [(httpx, "get"), (httpx, "Client"), (urllib.request, "urlopen")])


@pytest.fixture
def armadilhas(monkeypatch):
    """Troca toda porta de saída por uma que reprova o teste se for usada."""
    from mise_en_prato import catalogo, main, menor_preco

    chamadas = []
    _armar_rede(monkeypatch, chamadas)
    _armar(
        monkeypatch,
        chamadas,
        [
            (main, "buscar"),
            (main, "abrir_desafio"),
            (main, "consultar"),
            (main, "ler_qr"),
            (main, "interpretar_cupom_ocr"),
            (menor_preco, "MenorPreco"),
            (catalogo, "consultar_openfoodfacts_api"),
        ],
    )
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


def _arquivos(pasta: Path) -> set[Path]:
    """Os arquivos comuns de DADOS, fora o próprio banco (o WAL mexe nele sozinho)."""
    return {
        p for p in pasta.rglob("*") if p.is_file() and not p.name.startswith("mise_en_prato.db")
    }


# ── toda escrita, enumerada de app.routes ────────────────────────────────────
def _rotas_de_escrita() -> list:
    from mise_en_prato.main import app

    rotas = []
    for rota in app.routes:
        if not isinstance(rota, APIRoute) or not rota.path.startswith("/api/"):
            continue
        for metodo in sorted(rota.methods & {"POST", "PUT", "PATCH", "DELETE"}):
            caminho = re.sub(r"\{[^}]+\}", "1", rota.path)
            rotas.append(pytest.param(metodo, caminho, id=f"{metodo} {rota.path}"))
    return rotas


ROTAS_DE_ESCRITA = _rotas_de_escrita()


def test_enumeracao_pega_as_rotas_de_escrita():
    """Se a enumeração quebrar e voltar vazia, o teste de baixo passaria à toa."""
    ids = {p.id for p in ROTAS_DE_ESCRITA}
    assert len(ids) >= 30
    for esperado in (
        "POST /api/cupom",
        "POST /api/cupom/html",
        "POST /api/lista/{lista_id}/pesquisar",
        "PUT /api/receitas/{receita_id}",
        "DELETE /api/cardapio/{entrada_id}",
        "POST /api/encartes/webhook",
    ):
        assert esperado in ids


@pytest.mark.parametrize(("metodo", "caminho"), ROTAS_DE_ESCRITA)
def test_toda_escrita_barrada_no_demo(demo, armadilhas, tmp_path, metodo, caminho):
    from mise_en_prato import main

    antes = _contagens(demo.app.state.con)
    arquivos_antes = _arquivos(tmp_path)
    desafios_antes = dict(main._desafios)

    r = demo.request(metodo, caminho, json={})

    assert r.status_code == 403
    assert r.json()["detail"] == ALTERACOES
    assert armadilhas == []
    assert _contagens(demo.app.state.con) == antes
    assert _arquivos(tmp_path) == arquivos_antes
    assert main._desafios == desafios_antes


# ── GET que sairiam da casa ──────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("caminho", "mensagem"),
    [
        pytest.param("/api/chave/desafio", SEFAZ, id="captcha-sefaz"),
        pytest.param("/api/produto/1/candidatos", MENOR_PRECO, id="candidatos-menor-preco"),
        pytest.param(f"/api/catalogo/ean/{GTIN}", OPEN_FOOD_FACTS, id="ean-open-food-facts"),
    ],
)
def test_get_externo_barrado_no_demo(demo, armadilhas, caminho, mensagem):
    from mise_en_prato import main

    antes = _contagens(demo.app.state.con)
    desafios_antes = dict(main._desafios)

    r = demo.get(caminho)

    assert r.status_code == 403
    assert r.json()["detail"] == mensagem
    assert armadilhas == []
    assert _contagens(demo.app.state.con) == antes
    assert main._desafios == desafios_antes


@pytest.mark.parametrize(
    "caminho",
    [
        "/api/saude",
        "/api/cupons",
        "/api/produtos",
        "/api/listas",
        "/api/gastos",
        "/api/enriquecer",
        "/api/receitas",
        "/api/cardapio?semana=2026-11-11",
        "/api/cardapio/lista?semana=2026-11-11",
    ],
)
def test_leituras_continuam_no_demo(demo, armadilhas, caminho):
    r = demo.get(caminho)

    assert r.status_code == 200, r.text
    assert armadilhas == []


# ── segunda trava: os clientes de rede ───────────────────────────────────────
def test_clientes_de_rede_recusam_no_demo(tmp_path, monkeypatch):
    """Chamados direto, sem rota nenhuma na frente: levantam ModoDemo, sem rede."""
    from mise_en_prato import catalogo, consulta_chave, menor_preco, nfce_pr
    from mise_en_prato.banco import conectar

    rede = []
    _armar_rede(monkeypatch, rede)
    monkeypatch.setenv("DADOS", str(tmp_path))
    monkeypatch.setenv("APP_MODE", "demo")
    config.cache_clear()
    con = conectar(tmp_path / "x.db")

    def http_proibido(parametros):
        rede.append("MenorPreco.http")
        raise AssertionError("o Menor Preço foi chamado no modo demo")

    try:
        with pytest.raises(ModoDemo, match="SEFAZ"):
            nfce_pr.buscar(f"https://www.fazenda.pr.gov.br/nfce/qrcode?p={CHAVE}|2|1|1|abc")
        with pytest.raises(ModoDemo, match="SEFAZ"):
            consulta_chave.abrir_desafio()
        with pytest.raises(ModoDemo, match="Menor Preço"):
            menor_preco.MenorPreco(con, http=http_proibido).buscar(termo="arroz")
        with pytest.raises(ModoDemo, match="Menor Preço"):
            menor_preco.MenorPreco(con, http=http_proibido).canario()
        with pytest.raises(ModoDemo, match="Open Food Facts"):
            catalogo.consultar_openfoodfacts_api(GTIN)
        assert con.execute("SELECT COUNT(*) FROM consulta_menor_preco").fetchone()[0] == 0
    finally:
        con.close()
        config.cache_clear()
    assert rede == []


def test_rota_sem_guarda_ainda_responde_403_pelo_cliente(tmp_path, monkeypatch):
    """Simula a rota que esqueceu o guarda: o ModoDemo do cliente vira 403."""
    from mise_en_prato import main

    rede = []
    _armar_rede(monkeypatch, rede)
    monkeypatch.setattr(main, "_bloquear_no_demo", lambda o_que: None)
    with TestClient(_app(tmp_path, monkeypatch, app_mode="demo")) as c:
        r = c.get(f"/api/catalogo/ean/{GTIN}")
    config.cache_clear()

    assert r.status_code == 403
    assert (
        r.json()["detail"]
        == "Chamada externa (Open Food Facts) desativada na demonstração pública."
    )
    assert rede == []


# ── tarefas de fundo ─────────────────────────────────────────────────────────
@pytest.mark.parametrize(("modo", "quantas"), [("demo", 0), ("dev", 1), ("home", 1)])
def test_backup_so_sobe_fora_do_demo(tmp_path, monkeypatch, modo, quantas):
    with TestClient(_app(tmp_path, monkeypatch, app_mode=modo)) as c:
        tarefas = list(c.app.state.tarefas)
    config.cache_clear()

    assert len(tarefas) == quantas


# ── fora do demo, nada muda ──────────────────────────────────────────────────
def test_fora_do_demo_a_rota_nao_e_barrada(tmp_path, monkeypatch, armadilhas):
    """Em dev os guardas não agem: o pedido chega à porta de rede (a armadilha)."""
    with TestClient(
        _app(tmp_path, monkeypatch, app_mode="dev"), raise_server_exceptions=False
    ) as c:
        r = c.get("/api/chave/desafio")
        escrita = c.post("/api/listas", json={"nome": "Feira"})
    config.cache_clear()

    assert r.status_code != 403
    assert armadilhas == ["mise_en_prato.main.abrir_desafio"]
    assert escrita.status_code == 200, escrita.text
