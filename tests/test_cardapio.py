"""Testes do módulo de cardápio semanal e da migração 013.

Cobre:
- Cálculo da semana (segunda a domingo), inclusive virada de mês e de ano.
- Ordem das entradas dentro de uma casa (data + refeição).
- Entrada com receita (título e porções herdados) e com texto livre.
- Marcar e desmarcar como feito, inclusive criar já feita.
- Mover de casa leva para o fim do destino.
- Apagar ou renomear a receita não reescreve a entrada.
- Erros de validação e de "não encontrado".
"""

from datetime import date

import pytest

from mise_en_prato.banco import conectar
from mise_en_prato.cardapio import (
    EntradaNaoEncontrada,
    ReceitaNaoEncontrada,
    atualizar_entrada,
    criar_entrada,
    excluir_entrada,
    listar_semana,
    obter_entrada,
    semana_de,
)
from mise_en_prato.receitas import atualizar_receita, criar_receita, excluir_receita


@pytest.fixture
def con(tmp_path):
    conexao = conectar(tmp_path / "teste_cardapio.db")
    yield conexao
    conexao.close()


def _titulos(semana: dict, data: str, refeicao: str) -> list[str]:
    dia = next(d for d in semana["dias"] if d["data"] == data)
    return [e.titulo for e in dia[refeicao]]


# ── semana ───────────────────────────────────────────────────────────────────
@pytest.mark.parametrize(
    ("dia", "segunda", "domingo"),
    [
        (date(2026, 11, 11), date(2026, 11, 9), date(2026, 11, 15)),  # quarta comum
        (date(2026, 11, 9), date(2026, 11, 9), date(2026, 11, 15)),  # a própria segunda
        (date(2026, 11, 15), date(2026, 11, 9), date(2026, 11, 15)),  # o próprio domingo
        (date(2026, 12, 2), date(2026, 11, 30), date(2026, 12, 6)),  # virada de mês
        (date(2026, 12, 31), date(2026, 12, 28), date(2027, 1, 3)),  # virada de ano
        (date(2027, 1, 2), date(2026, 12, 28), date(2027, 1, 3)),  # virada de ano, lado de lá
    ],
)
def test_semana_de(dia, segunda, domingo):
    assert semana_de(dia) == (segunda, domingo)


def test_listar_semana_vazia_traz_sete_dias_em_ordem(con):
    semana = listar_semana(con, date(2026, 12, 31))
    assert semana["inicio"] == "2026-12-28"
    assert semana["fim"] == "2027-01-03"
    assert [d["data"] for d in semana["dias"]] == [
        "2026-12-28",
        "2026-12-29",
        "2026-12-30",
        "2026-12-31",
        "2027-01-01",
        "2027-01-02",
        "2027-01-03",
    ]
    assert all(d["almoco"] == [] and d["jantar"] == [] for d in semana["dias"])


def test_listar_semana_ignora_outras_semanas(con):
    criar_entrada(con, "2026-11-08", "almoco", titulo="domingo anterior")
    criar_entrada(con, "2026-11-09", "almoco", titulo="segunda")
    criar_entrada(con, "2026-11-15", "jantar", titulo="domingo")
    criar_entrada(con, "2026-11-16", "almoco", titulo="segunda seguinte")
    semana = listar_semana(con, date(2026, 11, 12))
    todas = [e.titulo for d in semana["dias"] for r in ("almoco", "jantar") for e in d[r]]
    assert sorted(todas) == ["domingo", "segunda"]


# ── ordem e várias entradas por casa ─────────────────────────────────────────
def test_varias_entradas_na_mesma_casa_vao_para_o_fim(con):
    a = criar_entrada(con, "2026-11-11", "almoco", titulo="Arroz")
    b = criar_entrada(con, "2026-11-11", "almoco", titulo="Feijão")
    c = criar_entrada(con, "2026-11-11", "almoco", titulo="Salada")
    criar_entrada(con, "2026-11-11", "jantar", titulo="Sopa")
    assert (a.ordem, b.ordem, c.ordem) == (0, 1, 2)

    semana = listar_semana(con, date(2026, 11, 11))
    assert _titulos(semana, "2026-11-11", "almoco") == ["Arroz", "Feijão", "Salada"]
    assert _titulos(semana, "2026-11-11", "jantar") == ["Sopa"]


def test_ordem_explicita_reordena_e_empate_desfaz_pelo_id(con):
    a = criar_entrada(con, "2026-11-11", "jantar", titulo="A")
    b = criar_entrada(con, "2026-11-11", "jantar", titulo="B")
    c = criar_entrada(con, "2026-11-11", "jantar", titulo="C")
    atualizar_entrada(con, c.id, ordem=0)  # empata com A; A tem id menor
    atualizar_entrada(con, b.id, ordem=5)
    semana = listar_semana(con, date(2026, 11, 11))
    assert _titulos(semana, "2026-11-11", "jantar") == ["A", "C", "B"]
    assert a.id < c.id


# ── criação ──────────────────────────────────────────────────────────────────
def test_criar_com_receita_herda_titulo_e_porcoes(con):
    rec = criar_receita(con, nome="Feijoada", porcoes=6)
    e = criar_entrada(con, "2026-11-14", "almoco", receita_id=rec, morador="ana@casa")
    assert e.titulo == "Feijoada"
    assert e.porcoes == 6
    assert e.receita_id == rec
    assert e.receita_tem_foto is False
    assert e.feito is False and e.feito_em is None
    assert e.morador == "ana@casa"
    assert e.criada_em


def test_criar_com_receita_respeita_titulo_e_porcoes_informados(con):
    rec = criar_receita(con, nome="Feijoada", porcoes=6)
    e = criar_entrada(
        con, "2026-11-14", "almoco", receita_id=rec, titulo="  Feijoada light  ", porcoes=2
    )
    assert e.titulo == "Feijoada light"
    assert e.porcoes == 2


def test_criar_com_receita_indica_foto(con):
    rec = criar_receita(con, nome="Carbonara", foto_caminho="receitas/fotos/1_1.webp")
    e = criar_entrada(con, "2026-11-14", "jantar", receita_id=rec)
    assert e.receita_tem_foto is True
    dia = listar_semana(con, date(2026, 11, 14))["dias"][5]
    assert dia["jantar"][0].receita_tem_foto is True


def test_criar_texto_livre(con):
    e = criar_entrada(con, "2026-11-13", "jantar", titulo="  pizza da esquina  ")
    assert e.titulo == "pizza da esquina"
    assert e.porcoes is None
    assert e.receita_id is None
    assert e.receita_tem_foto is False


def test_criar_texto_livre_com_porcoes(con):
    e = criar_entrada(con, "2026-11-13", "jantar", titulo="sobras", porcoes=3)
    assert e.porcoes == 3


def test_criar_ja_feito_sem_plano_previo(con):
    e = criar_entrada(con, "2026-10-01", "almoco", titulo="Omelete", feito=True)
    assert e.feito is True
    assert e.feito_em


def test_criar_aceita_date(con):
    e = criar_entrada(con, date(2026, 11, 11), "almoco", titulo="X")
    assert e.data == "2026-11-11"


# ── feito ────────────────────────────────────────────────────────────────────
def test_marcar_e_desmarcar_feito(con):
    e = criar_entrada(con, "2026-11-11", "almoco", titulo="Arroz")
    feito = atualizar_entrada(con, e.id, feito=True)
    assert feito.feito is True
    assert feito.feito_em

    de_novo = atualizar_entrada(con, e.id, feito=True)
    assert de_novo.feito_em == feito.feito_em  # já feito: mantém o momento original

    desfeito = atualizar_entrada(con, e.id, feito=False)
    assert desfeito.feito is False
    assert desfeito.feito_em is None


def test_atualizacao_parcial_nao_mexe_no_resto(con):
    rec = criar_receita(con, nome="Lasanha", porcoes=8)
    e = criar_entrada(con, "2026-11-11", "jantar", receita_id=rec, porcoes=4)
    depois = atualizar_entrada(con, e.id, feito=True)
    assert (depois.titulo, depois.porcoes, depois.data, depois.refeicao, depois.ordem) == (
        "Lasanha",
        4,
        "2026-11-11",
        "jantar",
        0,
    )
    assert depois.receita_id == rec


def test_atualizar_titulo_e_limpar_porcoes(con):
    e = criar_entrada(con, "2026-11-11", "jantar", titulo="sobras", porcoes=2)
    depois = atualizar_entrada(con, e.id, titulo="  restos de ontem ", porcoes=None)
    assert depois.titulo == "restos de ontem"
    assert depois.porcoes is None


# ── mover ────────────────────────────────────────────────────────────────────
def test_mover_para_outra_casa_vai_para_o_fim(con):
    criar_entrada(con, "2026-11-12", "jantar", titulo="Já estava 1")
    criar_entrada(con, "2026-11-12", "jantar", titulo="Já estava 2")
    e = criar_entrada(con, "2026-11-11", "almoco", titulo="Viajante")

    movida = atualizar_entrada(con, e.id, data="2026-11-12", refeicao="jantar")
    assert movida.ordem == 2
    semana = listar_semana(con, date(2026, 11, 11))
    assert _titulos(semana, "2026-11-11", "almoco") == []
    assert _titulos(semana, "2026-11-12", "jantar") == ["Já estava 1", "Já estava 2", "Viajante"]


def test_mover_so_a_refeicao_vai_para_o_fim(con):
    criar_entrada(con, "2026-11-11", "jantar", titulo="Sopa")
    e = criar_entrada(con, "2026-11-11", "almoco", titulo="Arroz")
    movida = atualizar_entrada(con, e.id, refeicao="jantar")
    assert movida.ordem == 1


def test_mover_com_ordem_explicita_respeita_a_ordem(con):
    criar_entrada(con, "2026-11-12", "jantar", titulo="Primeira")
    e = criar_entrada(con, "2026-11-11", "almoco", titulo="Furona")
    movida = atualizar_entrada(con, e.id, data="2026-11-12", refeicao="jantar", ordem=0)
    assert movida.ordem == 0


def test_reenviar_a_mesma_casa_nao_muda_a_ordem(con):
    e = criar_entrada(con, "2026-11-11", "almoco", titulo="A")
    criar_entrada(con, "2026-11-11", "almoco", titulo="B")
    mesma = atualizar_entrada(con, e.id, data="2026-11-11", refeicao="almoco")
    assert mesma.ordem == 0


# ── vínculo com a receita ────────────────────────────────────────────────────
def test_apagar_receita_preserva_entrada_com_titulo(con):
    rec = criar_receita(con, nome="Strogonoff", porcoes=4)
    e = criar_entrada(con, "2026-11-11", "jantar", receita_id=rec)
    assert excluir_receita(con, rec)

    depois = obter_entrada(con, e.id)
    assert depois is not None
    assert depois.titulo == "Strogonoff"
    assert depois.receita_id is None
    assert depois.porcoes == 4
    assert depois.receita_tem_foto is False


def test_renomear_receita_nao_altera_entrada(con):
    rec = criar_receita(con, nome="Bolo", porcoes=8)
    e = criar_entrada(con, "2026-11-11", "jantar", receita_id=rec)
    atualizar_receita(con, rec, nome="Bolo de Cenoura", porcoes=10)

    depois = obter_entrada(con, e.id)
    assert depois.titulo == "Bolo"
    assert depois.porcoes == 8
    assert depois.receita_id == rec


# ── exclusão ─────────────────────────────────────────────────────────────────
def test_excluir_entrada(con):
    e = criar_entrada(con, "2026-11-11", "almoco", titulo="Arroz")
    excluir_entrada(con, e.id)
    assert obter_entrada(con, e.id) is None


def test_excluir_entrada_inexistente(con):
    with pytest.raises(EntradaNaoEncontrada):
        excluir_entrada(con, 999)


# ── validação ────────────────────────────────────────────────────────────────
def test_criar_sem_receita_e_sem_titulo(con):
    with pytest.raises(ValueError, match="receita ou um título"):
        criar_entrada(con, "2026-11-11", "almoco")


@pytest.mark.parametrize("titulo", ["", "   "])
def test_criar_texto_livre_vazio(con, titulo):
    with pytest.raises(ValueError, match="vazio"):
        criar_entrada(con, "2026-11-11", "almoco", titulo=titulo)


def test_criar_com_receita_inexistente(con):
    with pytest.raises(ReceitaNaoEncontrada):
        criar_entrada(con, "2026-11-11", "almoco", receita_id=999)


@pytest.mark.parametrize("refeicao", ["cafe", "Almoco", "", "lanche"])
def test_refeicao_invalida(con, refeicao):
    with pytest.raises(ValueError, match="Refeição"):
        criar_entrada(con, "2026-11-11", refeicao, titulo="X")


@pytest.mark.parametrize(
    "data", ["2026-02-30", "2026-13-01", "11/11/2026", "20261111", "2026-W45-3", "", "amanhã"]
)
def test_data_invalida(con, data):
    with pytest.raises(ValueError, match="Data inválida"):
        criar_entrada(con, data, "almoco", titulo="X")


@pytest.mark.parametrize("porcoes", [0, -2])
def test_porcoes_nao_positivas(con, porcoes):
    with pytest.raises(ValueError, match="maior que zero"):
        criar_entrada(con, "2026-11-11", "almoco", titulo="X", porcoes=porcoes)


@pytest.mark.parametrize("porcoes", [2.0, 2.5, True, "3"])
def test_porcoes_de_tipo_errado(con, porcoes):
    with pytest.raises(TypeError, match="inteiro"):
        criar_entrada(con, "2026-11-11", "almoco", titulo="X", porcoes=porcoes)


def test_atualizar_valida_campos(con):
    e = criar_entrada(con, "2026-11-11", "almoco", titulo="X")
    with pytest.raises(ValueError, match="Refeição"):
        atualizar_entrada(con, e.id, refeicao="ceia")
    with pytest.raises(ValueError, match="Data inválida"):
        atualizar_entrada(con, e.id, data="2026-02-29")  # 2026 não é bissexto
    with pytest.raises(TypeError):
        atualizar_entrada(con, e.id, porcoes=1.5)
    with pytest.raises(ValueError, match="maior que zero"):
        atualizar_entrada(con, e.id, porcoes=0)
    with pytest.raises(ValueError, match="vazio"):
        atualizar_entrada(con, e.id, titulo="  ")
    with pytest.raises(ValueError, match="vazio"):
        atualizar_entrada(con, e.id, titulo=None)
    with pytest.raises(TypeError):
        atualizar_entrada(con, e.id, feito="sim")
    with pytest.raises(ValueError, match="negativa"):
        atualizar_entrada(con, e.id, ordem=-1)
    # nada do que falhou foi gravado
    intacta = obter_entrada(con, e.id)
    assert (intacta.titulo, intacta.data, intacta.refeicao, intacta.porcoes) == (
        "X",
        "2026-11-11",
        "almoco",
        None,
    )


def test_atualizar_entrada_inexistente(con):
    with pytest.raises(EntradaNaoEncontrada):
        atualizar_entrada(con, 999, feito=True)


def test_banco_recusa_porcoes_e_refeicao_invalidas_direto(con):
    """A CHECK da migração segura o que escapar da camada python."""
    import sqlite3

    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO cardapio_entrada (data, refeicao, titulo, criada_em) "
            "VALUES ('2026-11-11', 'ceia', 'X', '2026-11-11 10:00:00')"
        )
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO cardapio_entrada (data, refeicao, titulo, porcoes, criada_em) "
            "VALUES ('2026-11-11', 'almoco', 'X', 0, '2026-11-11 10:00:00')"
        )
