"""A aba Gastos navegando entre meses: janela do gráfico, cupons e detalhe.

Os cupons reais das fixtures entram pela camada de gravação; os de meses
antigos são cópias do mesmo cupom com outra chave e outra data.
"""

import dataclasses
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mise_en_prato import relatorios
from mise_en_prato.config import config
from mise_en_prato.consulta_chave import interpretar_resposta
from mise_en_prato.cupons import gravar
from mise_en_prato.nfce_pr import interpretar

FIXTURES = Path(__file__).parent / "fixtures"
LAR = FIXTURES / "cupom_lar_2026-08-14.html"
LAR_CHAVE = FIXTURES / "cupom_lar_consulta_chave_2026-08-27.html"


def _app(tmp_path, monkeypatch, *, app_mode: str = "dev"):
    monkeypatch.setenv("DADOS", str(tmp_path))
    monkeypatch.setenv("EXIGIR_TAILNET", "false")
    monkeypatch.setenv("APP_MODE", app_mode)
    config.cache_clear()
    from mise_en_prato.main import app

    return app


def _semear(con) -> None:
    """Agosto/2026 com os dois cupons reais e um incompleto; março/2024 com um."""
    lar = interpretar(LAR.read_text(encoding="utf-8"))
    gravar(con, lar, morador="Miguel")
    gravar(con, interpretar_resposta(LAR_CHAVE.read_text(encoding="utf-8")), morador="Ana")
    incompleto = dataclasses.replace(
        lar,
        chave="9" * 44,
        emitido_em=datetime(2026, 8, 30, 18, 0),
        itens=[],
        desconto=Decimal("0"),
        forma_pagamento="",
        valor_pago=Decimal("10.01"),
        avisos=["cupom de teste sem itens"],
    )
    gravar(con, incompleto, morador="Miguel", origem="manual")
    antigo = dataclasses.replace(lar, chave="1" * 44, emitido_em=datetime(2024, 3, 10, 9, 30))
    gravar(con, antigo, morador="Ana")


@pytest.fixture
def cliente(tmp_path, monkeypatch):
    with TestClient(_app(tmp_path, monkeypatch)) as c:
        _semear(c.app.state.con)
        yield c
    config.cache_clear()


# ── janela do gráfico ────────────────────────────────────────────────────────
def test_fim_da_janela():
    hoje = date(2026, 11, 11)
    assert relatorios.fim_da_janela(None, hoje=hoje) == "2026-11"
    assert relatorios.fim_da_janela("2026-08", hoje=hoje) == "2026-11"
    assert relatorios.fim_da_janela("2025-12", hoje=hoje) == "2026-11"  # 12º mês, ainda cabe
    assert relatorios.fim_da_janela("2025-11", hoje=hoje) == "2025-11"  # 13º, a janela anda
    assert relatorios.fim_da_janela("2024-03", hoje=hoje) == "2024-03"
    assert relatorios.fim_da_janela("2027-01", hoje=hoje) == "2027-01"  # relógio adiantado


def test_somar_meses_atravessa_o_ano():
    assert relatorios.somar_meses("2026-01", -1) == "2025-12"
    assert relatorios.somar_meses("2025-12", 1) == "2026-01"
    assert relatorios.somar_meses("2026-11", -11) == "2025-12"


def test_mes_em_foco_antigo_leva_a_janela_junto(cliente):
    meses = cliente.get("/api/gastos?mes=2024-03").json()["meses"]
    assert len(meses) == 12
    assert [m["mes"] for m in meses][:3] == ["2024-03", "2024-02", "2024-01"]
    assert meses[-1]["mes"] == "2023-04"
    assert meses[0] == {"mes": "2024-03", "total": "106.74", "cupons": 1}
    # mês sem cupom aparece zerado, como string
    assert meses[1] == {"mes": "2024-02", "total": "0.00", "cupons": 0}


def test_janela_sem_buracos_contem_o_mes_em_foco(cliente):
    meses = cliente.get("/api/gastos?mes=2026-08").json()["meses"]
    rotulos = [m["mes"] for m in meses]
    assert "2026-08" in rotulos
    assert len(rotulos) == 12 == len(set(rotulos))
    # do mais novo para o mais antigo, mês a mês
    for k in range(1, 12):
        assert relatorios.somar_meses(rotulos[k - 1], -1) == rotulos[k]
    agosto = next(m for m in meses if m["mes"] == "2026-08")
    assert agosto["cupons"] == 3


def test_meses_com_gasto_preenche_os_vazios(cliente):
    serie = relatorios.meses_com_gasto(cliente.app.state.con, ate="2026-09")
    assert [m["mes"] for m in serie][:3] == ["2026-09", "2026-08", "2026-07"]
    assert serie[0]["total"] == Decimal("0.00") and serie[0]["cupons"] == 0
    assert serie[1]["cupons"] == 3


def test_sem_mes_continua_no_mes_corrente(cliente):
    corpo = cliente.get("/api/gastos").json()
    atual = relatorios.mes_corrente()
    assert corpo["resumo"]["mes"] == atual
    assert corpo["meses"][0]["mes"] == atual
    assert len(corpo["meses"]) == 12
    assert len(corpo["resumo"]["maiores"]) <= 15


@pytest.mark.parametrize("mes", ["2026-13", "2026-00", "agosto", "2026-8", "2026/08", "26-08"])
def test_mes_invalido_vira_422(cliente, mes):
    for rota in ("/api/gastos", "/api/gastos/cupons"):
        r = cliente.get(rota, params={"mes": mes})
        assert r.status_code == 422
        assert "AAAA-MM" in r.json()["detail"]


# ── cupons do mês ────────────────────────────────────────────────────────────
def test_soma_dos_cupons_e_o_total_do_mes(cliente):
    total = Decimal(cliente.get("/api/gastos?mes=2026-08").json()["resumo"]["total"])
    corpo = cliente.get("/api/gastos/cupons?mes=2026-08").json()
    assert corpo["mes"] == "2026-08"
    cupons = corpo["cupons"]
    assert len(cupons) == 3
    assert sum(Decimal(c["valor_pago"]) for c in cupons) == total
    # do mais novo para o mais antigo
    datas = [c["emitido_em"] for c in cupons]
    assert datas == sorted(datas, reverse=True)


def test_cupom_incompleto_vem_marcado_e_conta_no_total(cliente):
    cupons = cliente.get("/api/gastos/cupons?mes=2026-08").json()["cupons"]
    incompleto = next(c for c in cupons if c["chave"] == "9" * 44)
    assert incompleto["completo"] is False
    assert incompleto["valor_pago"] == "10.01"
    assert all(c["completo"] for c in cupons if c is not incompleto)
    total = cliente.get("/api/gastos?mes=2026-08").json()["resumo"]["total"]
    assert total == str(Decimal("106.74") + Decimal("63.94") + Decimal("10.01"))


def test_cupons_trazem_loja_morador_e_desconto(cliente):
    cupons = cliente.get("/api/gastos/cupons?mes=2026-08").json()["cupons"]
    lar = next(c for c in cupons if c["valor_pago"] == "106.74")
    assert lar["loja"] == "LAR COOPERATIVA AGROINDUSTRIAL"
    assert lar["morador"] == "Miguel"
    assert lar["desconto"] == "2.00"
    assert lar["itens"] == 16
    # os itens saem da rota que já existe, sob demanda
    detalhe = cliente.get(f"/api/cupom/{lar['chave']}").json()
    assert len(detalhe["itens"]) == 16


def test_cupons_de_mes_vazio(cliente):
    assert cliente.get("/api/gastos/cupons?mes=2025-01").json()["cupons"] == []


# ── todos os produtos ────────────────────────────────────────────────────────
def test_ver_todos_mantem_os_15_primeiros(cliente):
    padrao = cliente.get("/api/gastos?mes=2026-08").json()["resumo"]["maiores"]
    todos = cliente.get("/api/gastos?mes=2026-08&limite=0").json()["resumo"]["maiores"]
    assert len(padrao) == 15
    assert len(todos) > 15
    assert todos[:15] == padrao
    totais = [Decimal(p["total"]) for p in todos]
    assert totais == sorted(totais, reverse=True)


def test_limite_negativo_vira_422(cliente):
    assert cliente.get("/api/gastos?mes=2026-08&limite=-1").status_code == 422


# ── pagamento e descontos ────────────────────────────────────────────────────
def test_pagamento_e_desconto_nao_mexem_no_total(cliente):
    resumo = cliente.get("/api/gastos?mes=2026-08").json()["resumo"]
    assert resumo["total"] == "180.69"
    assert resumo["desconto_total"] == "2.00"
    formas = resumo["por_forma_pagamento"]
    # o "Troco" da consulta completa não vira forma de pagamento
    assert formas == [{"forma": "Cartão de Crédito", "total": "170.68", "cupons": 2}]
    # o cupom digitado não tem forma de pagamento: fica contado à parte
    assert resumo["cupons_sem_forma_pagamento"] == 1


def test_pagamento_sai_como_string_e_nao_como_float(cliente):
    bruto = cliente.get("/api/gastos?mes=2026-08").text
    assert '"desconto_total": "2.00"' in bruto
    assert '"total": "170.68"' in bruto
    assert '"desconto_total": 2.0' not in bruto


@pytest.mark.parametrize(
    ("bruto", "esperado"),
    [
        ("Cartão de Crédito", "Cartão de Crédito"),
        ("Cartão de Crédito + Troco", "Cartão de Crédito"),
        ("Dinheiro + Pix", "Dinheiro + Pix"),
        ("DIVERSOS", None),
        ("", None),
        (None, None),
        ("Troco", None),
    ],
)
def test_forma_legivel(bruto, esperado):
    assert relatorios._forma_legivel(bruto) == esperado


# ── modo demo ────────────────────────────────────────────────────────────────
def test_leituras_de_gastos_funcionam_em_modo_demo(tmp_path, monkeypatch):
    with TestClient(_app(tmp_path, monkeypatch, app_mode="demo")) as c:
        _semear(c.app.state.con)
        assert c.get("/api/gastos?mes=2026-08").status_code == 200
        assert c.get("/api/gastos?mes=2026-08&limite=0").status_code == 200
        r = c.get("/api/gastos/cupons?mes=2026-08")
        assert r.status_code == 200
        assert len(r.json()["cupons"]) == 3
    config.cache_clear()
