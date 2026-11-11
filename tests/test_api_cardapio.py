"""Testes de integração das rotas da API do cardápio semanal."""

from datetime import date

import pytest
from fastapi.testclient import TestClient

from mise_en_prato.config import config


def _app(tmp_path, monkeypatch, *, app_mode: str = "dev"):
    monkeypatch.setenv("DADOS", str(tmp_path))
    monkeypatch.setenv("EXIGIR_TAILNET", "false")
    monkeypatch.setenv("APP_MODE", app_mode)
    config.cache_clear()
    from mise_en_prato.main import app

    return app


@pytest.fixture
def cliente(tmp_path, monkeypatch):
    with TestClient(_app(tmp_path, monkeypatch, app_mode="dev")) as c:
        yield c
    config.cache_clear()


def _receita(cliente, nome="Feijoada", porcoes=6) -> int:
    r = cliente.post("/api/receitas", json={"nome": nome, "porcoes": porcoes})
    assert r.status_code == 200
    return r.json()["id"]


def _dia(semana: dict, data: str) -> dict:
    return next(d for d in semana["dias"] if d["data"] == data)


def test_ciclo_completo(cliente):
    rec_id = _receita(cliente)

    # cria com receita: título e porções vêm dela
    r = cliente.post(
        "/api/cardapio", json={"data": "2026-11-11", "refeicao": "almoco", "receita_id": rec_id}
    )
    assert r.status_code == 200
    com_receita = r.json()
    assert com_receita["titulo"] == "Feijoada"
    assert com_receita["porcoes"] == 6
    assert com_receita["receita_id"] == rec_id
    assert com_receita["receita_tem_foto"] is False
    assert com_receita["feito"] is False
    assert com_receita["morador"]  # quem criou fica gravado

    # cria texto livre
    r = cliente.post(
        "/api/cardapio", json={"data": "2026-11-11", "refeicao": "almoco", "titulo": "Salada"}
    )
    assert r.status_code == 200
    livre = r.json()
    assert livre["porcoes"] is None
    assert livre["receita_id"] is None

    # lista a semana
    r = cliente.get("/api/cardapio?semana=2026-11-13")
    assert r.status_code == 200
    semana = r.json()
    assert semana["inicio"] == "2026-11-09"
    assert semana["fim"] == "2026-11-15"
    assert len(semana["dias"]) == 7
    quarta = _dia(semana, "2026-11-11")
    assert [e["titulo"] for e in quarta["almoco"]] == ["Feijoada", "Salada"]
    assert quarta["jantar"] == []
    entrada = quarta["almoco"][0]
    for campo in ("id", "titulo", "porcoes", "feito", "feito_em", "receita_id", "receita_tem_foto"):
        assert campo in entrada

    # marca como feito
    r = cliente.put(f"/api/cardapio/{livre['id']}", json={"feito": True})
    assert r.status_code == 200
    assert r.json()["feito"] is True
    assert r.json()["feito_em"]

    # move para o jantar de outro dia
    r = cliente.put(
        f"/api/cardapio/{com_receita['id']}", json={"data": "2026-11-12", "refeicao": "jantar"}
    )
    assert r.status_code == 200
    semana = cliente.get("/api/cardapio?semana=2026-11-09").json()
    assert [e["titulo"] for e in _dia(semana, "2026-11-11")["almoco"]] == ["Salada"]
    assert [e["titulo"] for e in _dia(semana, "2026-11-12")["jantar"]] == ["Feijoada"]

    # remove
    r = cliente.delete(f"/api/cardapio/{livre['id']}")
    assert r.status_code == 200
    semana = cliente.get("/api/cardapio?semana=2026-11-09").json()
    assert _dia(semana, "2026-11-11")["almoco"] == []


def test_get_sem_parametro_usa_a_semana_de_hoje(cliente):
    r = cliente.get("/api/cardapio")
    assert r.status_code == 200
    dados = r.json()
    hoje = date.today().isoformat()
    assert dados["inicio"] <= hoje <= dados["fim"]
    assert date.fromisoformat(dados["inicio"]).weekday() == 0


@pytest.mark.parametrize("semana", ["2026-02-30", "ontem", "11/11/2026", "20261111"])
def test_get_com_data_invalida(cliente, semana):
    r = cliente.get(f"/api/cardapio?semana={semana}")
    assert r.status_code == 422
    assert "Data inválida" in r.json()["detail"]


def test_put_parcial_nao_apaga_titulo_nem_porcoes(cliente):
    rec_id = _receita(cliente, "Lasanha", 8)
    criada = cliente.post(
        "/api/cardapio",
        json={"data": "2026-11-11", "refeicao": "jantar", "receita_id": rec_id, "porcoes": 4},
    ).json()

    r = cliente.put(f"/api/cardapio/{criada['id']}", json={"feito": True})
    assert r.status_code == 200
    depois = r.json()
    assert depois["titulo"] == "Lasanha"
    assert depois["porcoes"] == 4
    assert depois["data"] == "2026-11-11"
    assert depois["refeicao"] == "jantar"
    assert depois["receita_id"] == rec_id

    r = cliente.put(f"/api/cardapio/{criada['id']}", json={"feito": False})
    assert r.json()["feito"] is False
    assert r.json()["feito_em"] is None
    assert r.json()["porcoes"] == 4


def test_put_porcoes_nulas_limpa(cliente):
    criada = cliente.post(
        "/api/cardapio",
        json={"data": "2026-11-11", "refeicao": "jantar", "titulo": "sobras", "porcoes": 2},
    ).json()
    r = cliente.put(f"/api/cardapio/{criada['id']}", json={"porcoes": None})
    assert r.status_code == 200
    assert r.json()["porcoes"] is None
    assert r.json()["titulo"] == "sobras"


def test_criar_ja_feito(cliente):
    r = cliente.post(
        "/api/cardapio",
        json={"data": "2026-10-01", "refeicao": "almoco", "titulo": "Omelete", "feito": True},
    )
    assert r.status_code == 200
    assert r.json()["feito"] is True


def test_apagar_receita_preserva_entrada(cliente):
    rec_id = _receita(cliente, "Strogonoff", 4)
    criada = cliente.post(
        "/api/cardapio", json={"data": "2026-11-11", "refeicao": "jantar", "receita_id": rec_id}
    ).json()
    assert cliente.delete(f"/api/receitas/{rec_id}").status_code == 200

    semana = cliente.get("/api/cardapio?semana=2026-11-11").json()
    entrada = _dia(semana, "2026-11-11")["jantar"][0]
    assert entrada["id"] == criada["id"]
    assert entrada["titulo"] == "Strogonoff"
    assert entrada["receita_id"] is None


# ── códigos de erro ──────────────────────────────────────────────────────────
def test_post_sem_receita_e_sem_titulo_422(cliente):
    r = cliente.post("/api/cardapio", json={"data": "2026-11-11", "refeicao": "almoco"})
    assert r.status_code == 422
    assert "receita ou um título" in r.json()["detail"]


def test_post_titulo_vazio_422(cliente):
    r = cliente.post(
        "/api/cardapio", json={"data": "2026-11-11", "refeicao": "almoco", "titulo": "   "}
    )
    assert r.status_code == 422


def test_post_receita_inexistente_404(cliente):
    r = cliente.post(
        "/api/cardapio", json={"data": "2026-11-11", "refeicao": "almoco", "receita_id": 9999}
    )
    assert r.status_code == 404


@pytest.mark.parametrize(
    "corpo",
    [
        {"data": "2026-02-30", "refeicao": "almoco", "titulo": "X"},
        {"data": "amanhã", "refeicao": "almoco", "titulo": "X"},
        {"data": "2026-11-11", "refeicao": "cafe", "titulo": "X"},
        {"data": "2026-11-11", "refeicao": "almoco", "titulo": "X", "porcoes": 0},
        {"data": "2026-11-11", "refeicao": "almoco", "titulo": "X", "porcoes": -1},
        {"data": "2026-11-11", "refeicao": "almoco", "titulo": "X", "porcoes": 2.5},
        {"data": "2026-11-11", "refeicao": "almoco", "titulo": "X", "porcoes": True},
    ],
)
def test_post_dados_invalidos_422(cliente, corpo):
    r = cliente.post("/api/cardapio", json=corpo)
    assert r.status_code == 422


def test_put_dados_invalidos_422_e_nada_muda(cliente):
    criada = cliente.post(
        "/api/cardapio", json={"data": "2026-11-11", "refeicao": "almoco", "titulo": "X"}
    ).json()
    for corpo in (
        {"data": "2026-13-01"},
        {"refeicao": "ceia"},
        {"porcoes": 0},
        {"titulo": ""},
        {"titulo": None},
    ):
        r = cliente.put(f"/api/cardapio/{criada['id']}", json=corpo)
        assert r.status_code == 422, corpo
    semana = cliente.get("/api/cardapio?semana=2026-11-11").json()
    assert [e["titulo"] for e in _dia(semana, "2026-11-11")["almoco"]] == ["X"]


def test_put_entrada_inexistente_404(cliente):
    r = cliente.put("/api/cardapio/9999", json={"feito": True})
    assert r.status_code == 404


def test_delete_entrada_inexistente_404(cliente):
    r = cliente.delete("/api/cardapio/9999")
    assert r.status_code == 404


# ── modo demo ────────────────────────────────────────────────────────────────
def test_bloqueio_escrita_em_modo_demo(tmp_path, monkeypatch):
    with TestClient(_app(tmp_path, monkeypatch, app_mode="demo")) as c:
        assert c.get("/api/cardapio?semana=2026-11-11").status_code == 200

        r = c.post(
            "/api/cardapio", json={"data": "2026-11-11", "refeicao": "almoco", "titulo": "X"}
        )
        assert r.status_code == 403
        assert "demonstração" in r.json()["detail"].lower()

        r = c.put("/api/cardapio/1", json={"feito": True})
        assert r.status_code == 403
        assert "demonstração" in r.json()["detail"].lower()

        r = c.delete("/api/cardapio/1")
        assert r.status_code == 403
        assert "demonstração" in r.json()["detail"].lower()
    config.cache_clear()
