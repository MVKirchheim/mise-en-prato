"""Tela "Ofertas": o que vale hoje em cada loja, a saúde da entrada e a imagem."""

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mise_en_prato import encartes
from mise_en_prato.banco import conectar
from mise_en_prato.config import config

FUSO = encartes.FUSO
FIXTURES = Path(__file__).parent / "fixtures" / "encartes"
LAR_PNG = (FIXTURES / "lar_2026-11-21_super_promocao.png").read_bytes()

AGORA = datetime(2026, 11, 23, 10, 0, tzinfo=FUSO)
HOJE = "2026-11-23"


@pytest.fixture
def con(tmp_path):
    con = conectar(tmp_path / "mise_en_prato.db")
    yield con
    con.close()


@pytest.fixture
def pasta(tmp_path):
    return tmp_path / "encartes"


def _fonte(con, nome: str, n: int) -> int:
    return encartes.adicionar_fonte(con, f"12036300000000000{n}@g.us", nome)


def _encarte(
    con,
    pasta: Path,
    fonte_id: int,
    *,
    recebido: datetime = AGORA - timedelta(hours=2),
    status: str = "lido",
    com_arquivo: bool = True,
    observacoes: str | None = None,
    avisos: list[str] | None = None,
) -> int:
    """Linha de encarte como o webhook e o leitor deixariam, com a imagem em disco."""
    n = con.execute("SELECT COUNT(*) FROM encarte").fetchone()[0] + 1
    arquivo = None
    if com_arquivo:
        alvo = pasta / recebido.date().isoformat() / f"arte-{n}.png"
        alvo.parent.mkdir(parents=True, exist_ok=True)
        alvo.write_bytes(LAR_PNG)
        arquivo = alvo.relative_to(pasta.parent).as_posix()
    encarte_id = encartes._inserir_encarte(
        con,
        fonte_id=fonte_id,
        mensagem_id=f"msg-{n}",
        recebido_em=encartes._iso(recebido),
        sha256=f"hash-{n}",
        arquivo=arquivo,
        mime="image/png" if com_arquivo else None,
        status=status,
        observacoes=observacoes,
        avisos=json.dumps(avisos, ensure_ascii=False) if avisos else None,
    )
    con.commit()
    return encarte_id


def _oferta(
    con,
    encarte_id: int,
    produto: str,
    *,
    inicio: str = HOJE,
    fim: str = HOJE,
    normal: int = 999,
    clube: int | None = None,
    unidade: str | None = None,
    avisos: list[str] | None = None,
) -> int:
    fonte_id = con.execute("SELECT fonte_id FROM encarte WHERE id = ?", (encarte_id,)).fetchone()[0]
    oferta_id = con.execute(
        """INSERT INTO oferta (encarte_id, fonte_id, produto, embalagem, unidade,
                               preco_normal_centavos, preco_clube_centavos, condicao,
                               validade_inicio, validade_fim, avisos, chave_dedupe)
           VALUES (?, ?, ?, '1 kg', ?, ?, ?, NULL, ?, ?, ?, ?)""",
        (
            encarte_id,
            fonte_id,
            produto,
            unidade,
            normal,
            clube,
            inicio,
            fim,
            json.dumps(avisos, ensure_ascii=False) if avisos else None,
            f"{produto}|{normal}|{clube}|{inicio}|{fim}",
        ),
    ).lastrowid
    con.commit()
    return oferta_id


def _vigentes(con, pasta, agora=AGORA) -> dict:
    return encartes.ofertas_vigentes(con, agora, pasta)


def _loja(resultado: dict, nome: str) -> dict:
    return next(loja for loja in resultado["lojas"] if loja["nome"] == nome)


def _produtos(resultado: dict, nome: str) -> list[str]:
    return [o["produto"] for o in _loja(resultado, nome)["ofertas"]]


# ── ofertas_vigentes ─────────────────────────────────────────────────────────
def test_bordas_do_intervalo(con, pasta):
    lar = _fonte(con, "LAR", 1)
    e = _encarte(con, pasta, lar)
    _oferta(con, e, "começa hoje", inicio=HOJE, fim="2026-11-25")
    _oferta(con, e, "acaba hoje", inicio="2026-11-20", fim=HOJE)
    _oferta(con, e, "acabou ontem", inicio="2026-11-20", fim="2026-11-22")
    _oferta(con, e, "começa amanhã", inicio="2026-11-24", fim="2026-11-26")

    r = _vigentes(con, pasta)

    assert r["hoje"] == HOJE
    assert _produtos(r, "LAR") == ["acaba hoje", "começa hoje"]


def test_oferta_de_varios_dias_vale_no_meio(con, pasta):
    lar = _fonte(con, "LAR", 1)
    e = _encarte(con, pasta, lar)
    oferta = _oferta(con, e, "Arroz", inicio="2026-11-20", fim="2026-11-29")

    [o] = _loja(_vigentes(con, pasta), "LAR")["ofertas"]

    assert o["id"] == oferta
    assert (o["validade_inicio"], o["validade_fim"]) == ("2026-11-20", "2026-11-29")


def test_hoje_e_o_dia_de_sao_paulo_e_nao_o_do_utc(con, pasta):
    lar = _fonte(con, "LAR", 1)
    e = _encarte(con, pasta, lar, recebido=datetime(2026, 11, 23, 8, 0, tzinfo=FUSO))
    _oferta(con, e, "só dia 23")
    # 23:30 em São Paulo = 02:30 UTC do dia 24: ainda é dia 23 lá.
    noite = datetime(2026, 11, 24, 2, 30, tzinfo=UTC)

    r = _vigentes(con, pasta, agora=noite)

    assert r["hoje"] == HOJE
    assert _produtos(r, "LAR") == ["só dia 23"]
    # Meia-noite e meia em São Paulo: já é dia 24, e a oferta acabou.
    r = _vigentes(con, pasta, agora=datetime(2026, 11, 24, 3, 30, tzinfo=UTC))
    assert r["hoje"] == "2026-11-24"
    assert _produtos(r, "LAR") == []


def test_loja_inativa_some_e_loja_sem_oferta_aparece_vazia(con, pasta):
    lar = _fonte(con, "LAR", 1)
    _fonte(con, "Max", 2)
    velha = _fonte(con, "Antiga", 3)
    _oferta(con, _encarte(con, pasta, lar), "Arroz")
    _oferta(con, _encarte(con, pasta, velha), "Feijão")
    encartes.desativar_fonte(con, velha)

    r = _vigentes(con, pasta)

    assert [loja["nome"] for loja in r["lojas"]] == ["LAR", "Max"]
    max_ = _loja(r, "Max")
    assert max_["ofertas"] == [] and max_["encartes"] == []
    assert max_["id"] == 2


def test_ordem_das_lojas_e_das_ofertas(con, pasta):
    zebra = _fonte(con, "zebra", 1)
    _fonte(con, "Atacadão", 2)
    _fonte(con, "max", 3)
    e = _encarte(con, pasta, zebra)
    for produto in ("banana", "Água mineral", "Arroz", "abacate"):
        _oferta(con, e, produto)

    r = _vigentes(con, pasta)

    assert [loja["nome"] for loja in r["lojas"]] == ["Atacadão", "max", "zebra"]
    assert _produtos(r, "zebra") == ["abacate", "Água mineral", "Arroz", "banana"]


def test_campos_da_oferta_e_avisos_como_lista(con, pasta):
    lar = _fonte(con, "LAR", 1)
    e = _encarte(con, pasta, lar, observacoes="Válido no app", avisos=["validade não encontrada"])
    _oferta(con, e, "Picanha", normal=6990, clube=5990, unidade="kg", avisos=["preço alto"])
    _oferta(con, e, "Sabão", normal=1290)

    loja = _loja(_vigentes(con, pasta), "LAR")
    picanha, sabao = loja["ofertas"]

    assert picanha["preco_normal_centavos"] == 6990
    assert picanha["preco_clube_centavos"] == 5990
    assert picanha["unidade"] == "kg"
    assert picanha["embalagem"] == "1 kg"
    assert picanha["encarte_id"] == e
    assert picanha["avisos"] == ["preço alto"]
    assert sabao["avisos"] == []
    assert sabao["preco_clube_centavos"] is None
    assert set(picanha) == {
        "id",
        "encarte_id",
        "produto",
        "embalagem",
        "unidade",
        "preco_normal_centavos",
        "preco_clube_centavos",
        "condicao",
        "validade_inicio",
        "validade_fim",
        "avisos",
    }
    [encarte] = loja["encartes"]
    assert encarte["id"] == e
    assert encarte["observacoes"] == "Válido no app"
    assert encarte["avisos"] == ["validade não encontrada"]
    assert encarte["tem_imagem"] is True


def test_so_encartes_com_oferta_vigente_aparecem(con, pasta):
    lar = _fonte(con, "LAR", 1)
    vigente = _encarte(con, pasta, lar)
    vencido = _encarte(con, pasta, lar, recebido=AGORA - timedelta(days=5))
    _encarte(con, pasta, lar, status="sem_ofertas")
    _encarte(con, pasta, lar, status="pendente")
    _oferta(con, vigente, "Arroz")
    _oferta(con, vencido, "Feijão", inicio="2026-11-18", fim="2026-11-19")

    assert [e["id"] for e in _loja(_vigentes(con, pasta), "LAR")["encartes"]] == [vigente]


def test_tem_imagem_falso_sem_arquivo_ou_com_arquivo_sumido(con, pasta):
    lar = _fonte(con, "LAR", 1)
    podado = _encarte(con, pasta, lar, com_arquivo=False, recebido=AGORA - timedelta(hours=3))
    sumido = _encarte(con, pasta, lar)
    (
        pasta.parent
        / con.execute("SELECT arquivo FROM encarte WHERE id = ?", (sumido,)).fetchone()[0]
    ).unlink()
    _oferta(con, podado, "Arroz")
    _oferta(con, sumido, "Feijão")

    encartes_ = _loja(_vigentes(con, pasta), "LAR")["encartes"]

    assert [(e["id"], e["tem_imagem"]) for e in encartes_] == [(podado, False), (sumido, False)]


def test_avisos_com_json_torto_viram_lista_vazia(con, pasta):
    lar = _fonte(con, "LAR", 1)
    e = _encarte(con, pasta, lar)
    oferta = _oferta(con, e, "Arroz")
    con.execute("UPDATE oferta SET avisos = 'nao e json' WHERE id = ?", (oferta,))
    con.execute("UPDATE encarte SET avisos = '{\"a\": 1}' WHERE id = ?", (e,))
    con.commit()

    loja = _loja(_vigentes(con, pasta), "LAR")

    assert loja["ofertas"][0]["avisos"] == []
    assert loja["encartes"][0]["avisos"] == []


# ── saúde ────────────────────────────────────────────────────────────────────
def test_saude_sem_encarte_nenhum(con, pasta):
    _fonte(con, "LAR", 1)

    assert _vigentes(con, pasta)["saude"] == {
        "ultimo_recebido_em": None,
        "pendentes": 0,
        "falhas_recentes": 0,
        "sem_ofertas_recentes": 0,
        "alerta": False,
    }


@pytest.mark.parametrize(
    ("horas", "alerta"),
    [(47, False), (48, False), (49, True)],
)
def test_saude_acende_depois_de_48_horas(con, pasta, horas, alerta):
    lar = _fonte(con, "LAR", 1)
    recebido = AGORA - timedelta(hours=horas)
    _encarte(con, pasta, lar, recebido=recebido)

    saude = _vigentes(con, pasta)["saude"]

    assert saude["ultimo_recebido_em"] == encartes._iso(recebido)
    assert saude["alerta"] is alerta


def test_saude_ultimo_e_o_mais_recente_das_fontes_ativas(con, pasta):
    lar = _fonte(con, "LAR", 1)
    velha = _fonte(con, "Antiga", 2)
    _encarte(con, pasta, lar, recebido=AGORA - timedelta(days=3))
    _encarte(con, pasta, lar, recebido=AGORA - timedelta(hours=10))
    _encarte(con, pasta, velha, recebido=AGORA - timedelta(hours=1))
    encartes.desativar_fonte(con, velha)

    saude = _vigentes(con, pasta)["saude"]

    assert saude["ultimo_recebido_em"] == encartes._iso(AGORA - timedelta(hours=10))
    assert saude["alerta"] is False


def test_saude_conta_falhas_recentes_e_nao_as_antigas(con, pasta):
    lar = _fonte(con, "LAR", 1)
    _encarte(con, pasta, lar, recebido=AGORA - timedelta(hours=1))
    _encarte(con, pasta, lar, status="falhou", recebido=AGORA - timedelta(days=8))

    saude = _vigentes(con, pasta)["saude"]
    assert saude["falhas_recentes"] == 0
    assert saude["alerta"] is False

    _encarte(con, pasta, lar, status="falhou", recebido=AGORA - timedelta(days=6))
    saude = _vigentes(con, pasta)["saude"]
    assert saude["falhas_recentes"] == 1
    assert saude["alerta"] is True


def test_saude_pendentes_e_sem_ofertas(con, pasta):
    lar = _fonte(con, "LAR", 1)
    _encarte(con, pasta, lar, status="pendente", recebido=AGORA - timedelta(hours=1))
    _encarte(con, pasta, lar, status="pendente", recebido=AGORA - timedelta(days=10))
    _encarte(con, pasta, lar, status="sem_ofertas", recebido=AGORA - timedelta(days=2))
    _encarte(con, pasta, lar, status="sem_ofertas", recebido=AGORA - timedelta(days=9))

    saude = _vigentes(con, pasta)["saude"]

    assert saude["pendentes"] == 2
    assert saude["sem_ofertas_recentes"] == 1
    assert saude["falhas_recentes"] == 0
    assert saude["alerta"] is False


# ── imagem_do_encarte ────────────────────────────────────────────────────────
def test_imagem_do_encarte(con, pasta):
    lar = _fonte(con, "LAR", 1)
    e = _encarte(con, pasta, lar)

    caminho, mime = encartes.imagem_do_encarte(con, e, pasta)

    assert caminho.read_bytes() == LAR_PNG
    assert mime == "image/png"


def test_imagem_fora_da_pasta_e_recusada(con, pasta, tmp_path):
    lar = _fonte(con, "LAR", 1)
    e = _encarte(con, pasta, lar)
    _oferta(con, e, "Arroz")
    (tmp_path / "segredo.png").write_bytes(LAR_PNG)
    for caminho in ("encartes/../segredo.png", "segredo.png", str(tmp_path / "segredo.png")):
        con.execute("UPDATE encarte SET arquivo = ? WHERE id = ?", (caminho, e))
        con.commit()
        with pytest.raises(LookupError):
            encartes.imagem_do_encarte(con, e, pasta)
        [encarte] = _loja(_vigentes(con, pasta), "LAR")["encartes"]
        assert encarte["tem_imagem"] is False


# ── rotas ────────────────────────────────────────────────────────────────────
def _app(tmp_path, monkeypatch, *, app_mode: str = "dev", exigir_tailnet: bool = False):
    monkeypatch.setenv("DADOS", str(tmp_path))
    monkeypatch.setenv("EXIGIR_TAILNET", "true" if exigir_tailnet else "false")
    monkeypatch.setenv("APP_MODE", app_mode)
    config.cache_clear()
    from mise_en_prato.main import app

    return app


def _popular(con: sqlite3.Connection, pasta: Path) -> dict[str, int]:
    """Uma loja com oferta de hoje e os três encartes sem imagem para servir."""
    hoje = datetime.now(FUSO)
    lar = _fonte(con, "LAR", 1)
    com_imagem = _encarte(con, pasta, lar, recebido=hoje)
    podado = _encarte(con, pasta, lar, com_arquivo=False, recebido=hoje)
    sumido = _encarte(con, pasta, lar, recebido=hoje - timedelta(minutes=1))
    arquivo = con.execute("SELECT arquivo FROM encarte WHERE id = ?", (sumido,)).fetchone()[0]
    (pasta.parent / arquivo).unlink()
    dia = hoje.date().isoformat()
    _oferta(con, com_imagem, "Arroz", inicio=dia, fim=dia, clube=899)
    return {"com_imagem": com_imagem, "podado": podado, "sumido": sumido}


def _contagens(con) -> dict[str, int]:
    tabelas = [
        r[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    ]
    return {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tabelas}


def _arquivos(pasta: Path) -> set[Path]:
    """Os arquivos de DADOS, fora o próprio banco (o WAL mexe nele sozinho)."""
    return {
        p for p in pasta.rglob("*") if p.is_file() and not p.name.startswith("mise_en_prato.db")
    }


@pytest.mark.parametrize("modo", ["dev", "demo"])
def test_rotas_servem_ofertas_e_imagem(tmp_path, monkeypatch, modo):
    with TestClient(_app(tmp_path, monkeypatch, app_mode=modo)) as c:
        con = c.app.state.con
        ids = _popular(con, tmp_path / "encartes")
        antes = _contagens(con)
        arquivos_antes = _arquivos(tmp_path)

        r = c.get("/api/encartes/ofertas")
        assert r.status_code == 200, r.text
        dados = r.json()
        assert dados["hoje"] == datetime.now(FUSO).date().isoformat()
        [loja] = dados["lojas"]
        assert [o["produto"] for o in loja["ofertas"]] == ["Arroz"]
        assert loja["ofertas"][0]["preco_clube_centavos"] == 899
        assert set(dados["saude"]) >= {"ultimo_recebido_em", "pendentes", "alerta"}

        r = c.get(f"/api/encartes/{ids['com_imagem']}/imagem")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/png"
        assert r.content == LAR_PNG
        assert "private" in r.headers["cache-control"]

        for caso in ("podado", "sumido"):
            assert c.get(f"/api/encartes/{ids[caso]}/imagem").status_code == 404
        assert c.get("/api/encartes/9999/imagem").status_code == 404

        assert _contagens(con) == antes
        assert _arquivos(tmp_path) == arquivos_antes


def test_imagem_com_caminho_fora_da_pasta_e_404(tmp_path, monkeypatch):
    with TestClient(_app(tmp_path, monkeypatch)) as c:
        con = c.app.state.con
        ids = _popular(con, tmp_path / "encartes")
        (tmp_path / "segredo.png").write_bytes(LAR_PNG)
        con.execute(
            "UPDATE encarte SET arquivo = 'encartes/../segredo.png' WHERE id = ?",
            (ids["com_imagem"],),
        )
        con.commit()

        assert c.get(f"/api/encartes/{ids['com_imagem']}/imagem").status_code == 404


def test_rotas_exigem_morador(tmp_path, monkeypatch):
    with TestClient(_app(tmp_path, monkeypatch, exigir_tailnet=True)) as c:
        ids = _popular(c.app.state.con, tmp_path / "encartes")
        assert c.get("/api/encartes/ofertas").status_code == 403
        assert c.get(f"/api/encartes/{ids['com_imagem']}/imagem").status_code == 403
