"""Lista de compras da semana e a regra da nota (fatia 3, migração 014).

Cobre:
- Agrupamento da semana: soma na mesma unidade, unidades diferentes com " + ",
  receita citada uma vez, escala pelas porções da entrada sem float.
- Entradas feitas, de texto livre e de receita apagada ficam de fora.
- A regra da lista: produto novo entra com 1, o que já estava não muda de
  quantidade, a nota é mesclada com "; " e não se repete.
- Rotas GET/POST /api/cardapio/lista e o envio por receita com `fator`.
- O caminho manual (`por_produto`) continua sobrescrevendo a quantidade sem
  apagar a nota.
"""

from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from mise_en_prato.banco import conectar
from mise_en_prato.cardapio import (
    atualizar_entrada,
    criar_entrada,
    lista_da_semana,
    nome_padrao_lista,
)
from mise_en_prato.config import config
from mise_en_prato.listas import (
    Pedido,
    criar,
    enviar_de_receitas,
    itens,
    mesclar_nota,
    por_produto,
)
from mise_en_prato.receitas import criar_receita, excluir_receita, formatar_nota

QUARTA = date(2026, 11, 11)  # semana de 9 a 15 de novembro


@pytest.fixture
def con(tmp_path):
    conexao = conectar(tmp_path / "teste_lista_semana.db")
    yield conexao
    conexao.close()


def _receita(con, nome, porcoes, ingredientes):
    """Ingredientes como (nome, quantidade, unidade) ou (nome, quantidade, unidade, gtin)."""
    return criar_receita(
        con,
        nome=nome,
        porcoes=porcoes,
        ingredientes=[
            {
                "nome_ingrediente": ing[0],
                "quantidade": Decimal(ing[1]),
                "unidade": ing[2],
                "gtin_associado": ing[3] if len(ing) > 3 else None,
            }
            for ing in ingredientes
        ],
    )


def _grupo(previa, nome):
    return next(g for g in previa["grupos"] if g["nome"].casefold() == nome.casefold())


def _item(con, lista_id, produto_id):
    return next(i for i in itens(con, lista_id) if i["produto_id"] == produto_id)


def _produto(con, descricao):
    con.execute("INSERT INTO produto (descricao_canonica, acompanhar) VALUES (?, 0)", (descricao,))
    con.commit()
    return con.execute(
        "SELECT id FROM produto WHERE descricao_canonica = ?", (descricao,)
    ).fetchone()["id"]


# ── formatação da nota ───────────────────────────────────────────────────────
def test_formatar_nota_soma_unidade_igual_e_concatena_as_diferentes():
    partes = [
        (Decimal("300"), "g"),
        (Decimal("2"), "xícaras"),
        (Decimal("200"), " G "),
        (Decimal("1"), "kg"),
    ]
    assert formatar_nota(partes, ["Lasanha", "Bolo", "Lasanha"]) == (
        "500 g + 2 xícaras + 1 kg (Lasanha, Bolo)"
    )


def test_formatar_nota_usa_fracao_culinaria():
    assert formatar_nota([(Decimal("1.5"), "xícara")], ["Bolo"]) == "1 ½ xícara (Bolo)"


def test_mesclar_nota():
    assert mesclar_nota(None, "500 g (Bolo)") == "500 g (Bolo)"
    assert mesclar_nota("500 g (Bolo)", None) == "500 g (Bolo)"
    assert mesclar_nota("500 g (Bolo)", "500 g (Bolo)") == "500 g (Bolo)"
    assert mesclar_nota("1 kg (Pão); 500 g (Bolo)", "500 g (Bolo)") == "1 kg (Pão); 500 g (Bolo)"
    assert mesclar_nota("500 g (Bolo)", "1 kg (Pão)") == "500 g (Bolo); 1 kg (Pão)"
    # trecho inteiro, não pedaço de texto: "500 g" não está em "1500 g"
    assert mesclar_nota("1500 g (Bolo)", "500 g (Bolo)") == "1500 g (Bolo); 500 g (Bolo)"


def test_nome_padrao_lista():
    assert nome_padrao_lista(date(2026, 11, 9), date(2026, 11, 15)) == "Cardápio 9 – 15 nov"
    assert nome_padrao_lista(date(2026, 11, 30), date(2026, 12, 6)) == "Cardápio 30 nov – 6 dez"


# ── prévia da semana ─────────────────────────────────────────────────────────
def test_soma_por_unidade_entre_receitas_e_receita_citada_uma_vez(con):
    lasanha = _receita(
        con,
        "Lasanha",
        4,
        [
            ("Farinha de trigo", "300", "g"),
            ("Leite", "1", "l"),
            ("farinha  de TRIGO", "1", "xícara"),
        ],
    )
    bolo = _receita(con, "Bolo", 4, [(" Farinha de Trigo ", "200", "g"), ("Ovo", "3", "un")])
    criar_entrada(con, QUARTA, "almoco", receita_id=lasanha)
    criar_entrada(con, date(2026, 11, 12), "jantar", receita_id=bolo)
    criar_entrada(con, date(2026, 11, 13), "jantar", receita_id=bolo)  # o bolo, de novo

    previa = lista_da_semana(con, QUARTA)

    assert previa["inicio"] == "2026-11-09"
    assert previa["fim"] == "2026-11-15"
    assert previa["entradas"] == 3
    assert previa["ignoradas"] == 0
    farinha = _grupo(previa, "Farinha de trigo")
    assert farinha["chave"] == "nome:farinha de trigo"
    assert farinha["partes"] == [
        {"quantidade": Decimal("700"), "unidade": "g"},
        {"quantidade": Decimal("1"), "unidade": "xícara"},
    ]
    assert farinha["receitas"] == ["Bolo", "Lasanha"]
    assert farinha["nota"] == "700 g + 1 xícara (Bolo, Lasanha)"
    assert _grupo(previa, "Ovo")["nota"] == "6 un (Bolo)"
    assert len(previa["grupos"]) == 3


def test_agrupa_pelo_codigo_de_barras_quando_ha(con):
    a = _receita(con, "A", 1, [("Molho de tomate", "1", "lata", "789123")])
    b = _receita(con, "B", 1, [("Extrato Pomarola", "2", "lata", "789123")])
    criar_entrada(con, QUARTA, "almoco", receita_id=a)
    criar_entrada(con, QUARTA, "jantar", receita_id=b)

    previa = lista_da_semana(con, QUARTA)

    assert len(previa["grupos"]) == 1
    grupo = previa["grupos"][0]
    assert grupo["chave"] == "gtin:789123"
    assert grupo["nome"] == "Molho de tomate"  # a primeira grafia vista
    assert grupo["gtin"] == "789123"
    assert grupo["nota"] == "3 lata (A, B)"


def test_escala_pelas_porcoes_da_entrada_sem_float(con):
    rec = _receita(con, "Risoto", 4, [("Arroz", "300", "g"), ("Manteiga", "1", "colher")])
    criar_entrada(con, QUARTA, "almoco", receita_id=rec, porcoes=6)

    previa = lista_da_semana(con, QUARTA)

    arroz = _grupo(previa, "Arroz")["partes"][0]["quantidade"]
    manteiga = _grupo(previa, "Manteiga")["partes"][0]["quantidade"]
    assert arroz == Decimal("450") and isinstance(arroz, Decimal)
    assert manteiga == Decimal("1.5") and isinstance(manteiga, Decimal)
    assert _grupo(previa, "Manteiga")["nota"] == "1 ½ colher (Risoto)"


def test_porcoes_iguais_ou_vazias_nao_escalam(con):
    rec = _receita(con, "Sopa", 2, [("Batata", "3", "un")])
    criar_entrada(con, QUARTA, "almoco", receita_id=rec)  # herda 2 porções
    entrada = criar_entrada(con, QUARTA, "jantar", receita_id=rec)
    atualizar_entrada(con, entrada.id, porcoes=None)

    assert _grupo(lista_da_semana(con, QUARTA), "Batata")["nota"] == "6 un (Sopa)"


def test_feitas_texto_livre_receita_apagada_e_outra_semana_ficam_de_fora(con):
    feita = _receita(con, "Feita", 1, [("Alho", "1", "dente")])
    apagada = _receita(con, "Apagada", 1, [("Cebola", "1", "un")])
    fica = _receita(con, "Fica", 1, [("Tomate", "2", "un")])
    criar_entrada(con, QUARTA, "almoco", receita_id=feita, feito=True)
    criar_entrada(con, QUARTA, "almoco", titulo="Pizza da esquina")
    criar_entrada(con, QUARTA, "jantar", receita_id=apagada)
    criar_entrada(con, QUARTA, "jantar", receita_id=fica)
    criar_entrada(con, date(2026, 11, 16), "almoco", receita_id=feita)  # semana seguinte
    excluir_receita(con, apagada)

    previa = lista_da_semana(con, QUARTA)

    assert [g["nome"] for g in previa["grupos"]] == ["Tomate"]
    assert previa["entradas"] == 1


def test_semana_vazia(con):
    previa = lista_da_semana(con, QUARTA)
    assert previa["grupos"] == []
    assert previa["entradas"] == 0


# ── a regra da lista ─────────────────────────────────────────────────────────
def test_quantidade_existente_preservada_novo_entra_com_um_e_nota_mesclada(con):
    lista = criar(con, "Compras", "Miguel")
    farinha = _produto(con, "FARINHA DE TRIGO")
    por_produto(con, lista, farinha, Decimal("3"))

    pedidos = [
        Pedido("Farinha de trigo", None, [(Decimal("500"), "g")], ["Bolo"]),
        Pedido("Ovo", None, [(Decimal("3"), "un")], ["Bolo"]),
    ]
    resultado = enviar_de_receitas(con, pedidos, "Miguel", lista_id=lista)

    assert resultado == {"lista_id": lista, "adicionados": 1, "ja_na_lista": 1}
    item_farinha = _item(con, lista, farinha)
    assert item_farinha["quantidade"] == Decimal("3.000")
    assert item_farinha["nota"] == "500 g (Bolo)"
    ovo = next(i for i in itens(con, lista) if i["descricao"] == "OVO")
    assert ovo["quantidade"] == Decimal("1.000")
    assert ovo["nota"] == "3 un (Bolo)"

    # outra receita: a nota cresce com "; "
    enviar_de_receitas(
        con, [Pedido("farinha de trigo", None, [(Decimal("1"), "kg")], ["Pão"])], "Miguel", lista
    )
    assert _item(con, lista, farinha)["nota"] == "500 g (Bolo); 1 kg (Pão)"

    # a mesma coisa de novo: nada se repete, nada muda de quantidade
    resultado = enviar_de_receitas(con, pedidos, "Miguel", lista_id=lista)
    assert resultado["adicionados"] == 0 and resultado["ja_na_lista"] == 2
    assert _item(con, lista, farinha)["nota"] == "500 g (Bolo); 1 kg (Pão)"
    assert _item(con, lista, farinha)["quantidade"] == Decimal("3.000")


def test_por_produto_manual_sobrescreve_quantidade_e_mantem_nota(con):
    lista = criar(con, "Compras", "Miguel")
    enviar_de_receitas(
        con, [Pedido("Leite", None, [(Decimal("1"), "l")], ["Bolo"])], "Miguel", lista
    )
    leite = itens(con, lista)[0]["produto_id"]

    por_produto(con, lista, leite, Decimal("4"))

    item = _item(con, lista, leite)
    assert item["quantidade"] == Decimal("4.000")
    assert item["nota"] == "1 l (Bolo)"


def test_item_manual_sem_nota(con):
    lista = criar(con, "Compras", "Miguel")
    por_produto(con, lista, _produto(con, "SABAO"))
    assert itens(con, lista)[0]["nota"] is None


# ── rotas ────────────────────────────────────────────────────────────────────
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


def _receita_api(cliente, nome, porcoes, ingredientes) -> int:
    r = cliente.post(
        "/api/receitas",
        json={
            "nome": nome,
            "porcoes": porcoes,
            "ingredientes": [
                {"nome_ingrediente": n, "quantidade": q, "unidade": u} for n, q, u in ingredientes
            ],
        },
    )
    assert r.status_code == 200
    return r.json()["id"]


def _planejar(cliente, receita_id, data="2026-11-11", refeicao="almoco", **extra):
    r = cliente.post(
        "/api/cardapio",
        json={"data": data, "refeicao": refeicao, "receita_id": receita_id, **extra},
    )
    assert r.status_code == 200
    return r.json()


def _itens_lista(cliente, lista_id):
    r = cliente.get(f"/api/lista/{lista_id}")
    assert r.status_code == 200
    return {i["descricao"]: i for i in r.json()["itens"]}


def test_get_previa_da_semana(cliente):
    rec = _receita_api(cliente, "Lasanha", 4, [("Farinha", "300", "g"), ("Leite", "1", "l")])
    _planejar(cliente, rec, porcoes=6)

    r = cliente.get("/api/cardapio/lista?semana=2026-11-13")

    assert r.status_code == 200
    previa = r.json()
    assert previa["inicio"] == "2026-11-09"
    assert previa["entradas"] == 1
    farinha = next(g for g in previa["grupos"] if g["nome"] == "Farinha")
    assert farinha["partes"] == [{"quantidade": "450", "unidade": "g"}]  # Decimal vira texto
    assert farinha["nota"] == "450 g (Lasanha)"
    leite = next(g for g in previa["grupos"] if g["nome"] == "Leite")
    assert leite["nota"] == "1 ½ l (Lasanha)"


def test_get_previa_valida_a_data(cliente):
    assert cliente.get("/api/cardapio/lista?semana=2026-02-30").status_code == 422
    assert cliente.get("/api/cardapio/lista?semana=ontem").status_code == 422


def test_rotas_por_id_continuam_funcionando(cliente):
    rec = _receita_api(cliente, "Sopa", 2, [("Batata", "3", "un")])
    entrada = _planejar(cliente, rec)
    assert cliente.put(f"/api/cardapio/{entrada['id']}", json={"porcoes": 3}).status_code == 200
    assert cliente.put("/api/cardapio/lista", json={"porcoes": 3}).status_code == 422
    assert cliente.delete(f"/api/cardapio/{entrada['id']}").status_code == 200


def test_post_aplica_so_as_chaves_escolhidas_em_lista_nova(cliente):
    rec = _receita_api(cliente, "Bolo", 4, [("Farinha", "500", "g"), ("Ovo", "3", "un")])
    _planejar(cliente, rec)

    r = cliente.post(
        "/api/cardapio/lista",
        json={"semana": "2026-11-11", "chaves": ["nome:farinha", "nome:inexistente"]},
    )

    assert r.status_code == 200
    corpo = r.json()
    assert corpo["ok"] is True
    assert corpo["adicionados"] == 1
    assert corpo["ja_na_lista"] == 0
    lista = next(
        lst for lst in cliente.get("/api/listas").json()["listas"] if lst["id"] == corpo["lista_id"]
    )
    assert lista["nome"] == "Cardápio 9 – 15 nov"
    itens_lista = _itens_lista(cliente, corpo["lista_id"])
    assert list(itens_lista) == ["FARINHA"]
    assert itens_lista["FARINHA"]["quantidade"] == "1.000"
    assert itens_lista["FARINHA"]["nota"] == "500 g (Bolo)"


def test_post_em_lista_existente_preserva_quantidade_e_nao_duplica_nota(cliente):
    rec = _receita_api(cliente, "Bolo", 4, [("Farinha", "500", "g")])
    _planejar(cliente, rec)
    lista_id = cliente.post("/api/listas", json={"nome": "Mercado"}).json()["id"]
    corpo = {"semana": "2026-11-11", "chaves": ["nome:farinha"], "lista_id": lista_id}

    assert cliente.post("/api/cardapio/lista", json=corpo).json()["adicionados"] == 1
    produto_id = _itens_lista(cliente, lista_id)["FARINHA"]["produto_id"]
    cliente.post(f"/api/lista/{lista_id}/item", json={"produto_id": produto_id, "quantidade": "2"})

    r = cliente.post("/api/cardapio/lista", json=corpo)

    assert r.status_code == 200
    assert r.json()["adicionados"] == 0
    assert r.json()["ja_na_lista"] == 1
    farinha = _itens_lista(cliente, lista_id)["FARINHA"]
    assert farinha["quantidade"] == "2.000"
    assert farinha["nota"] == "500 g (Bolo)"


def test_post_erros(cliente):
    rec = _receita_api(cliente, "Bolo", 4, [("Farinha", "500", "g")])
    _planejar(cliente, rec)

    r = cliente.post(
        "/api/cardapio/lista",
        json={"semana": "2026-11-11", "chaves": ["nome:farinha"], "lista_id": 999},
    )
    assert r.status_code == 404

    r = cliente.post("/api/cardapio/lista", json={"semana": "2026-11-11", "chaves": []})
    assert r.status_code == 422
    assert "nenhum ingrediente" in r.json()["detail"].lower()

    r = cliente.post("/api/cardapio/lista", json={"semana": "2026-11-11", "chaves": ["nome:x"]})
    assert r.status_code == 422

    r = cliente.post(
        "/api/cardapio/lista", json={"semana": "11/11/2026", "chaves": ["nome:farinha"]}
    )
    assert r.status_code == 422

    # nada foi criado pelas tentativas que falharam
    assert cliente.get("/api/listas").json()["listas"] == []


def test_demo_le_a_previa_mas_nao_envia(tmp_path, monkeypatch):
    with TestClient(_app(tmp_path, monkeypatch, app_mode="demo")) as c:
        r = c.get("/api/cardapio/lista?semana=2026-11-11")
        assert r.status_code == 200
        assert r.json()["grupos"] == []

        r = c.post("/api/cardapio/lista", json={"semana": "2026-11-11", "chaves": ["nome:x"]})
        assert r.status_code == 403
        assert "demonstração" in r.json()["detail"].lower()
    config.cache_clear()


# ── envio por receita ────────────────────────────────────────────────────────
def _ingredientes(cliente, rec_id):
    return {
        i["nome_ingrediente"]: i["id"]
        for i in cliente.get(f"/api/receitas/{rec_id}").json()["ingredientes"]
    }


def test_enviar_receita_nao_transforma_gramas_em_quantidade(cliente):
    rec = _receita_api(cliente, "Pão", 1, [("Farinha", "500", "g")])
    ids = _ingredientes(cliente, rec)

    r = cliente.post(
        f"/api/receitas/{rec}/enviar-para-lista", json={"ingrediente_ids": [ids["Farinha"]]}
    )

    assert r.status_code == 200
    farinha = _itens_lista(cliente, r.json()["lista_id"])["FARINHA"]
    assert farinha["quantidade"] == "1.000"
    assert farinha["nota"] == "500 g (Pão)"


def test_enviar_receita_aplica_fator(cliente):
    rec = _receita_api(cliente, "Pão", 1, [("Farinha", "500", "g"), ("Fermento", "1", "colher")])
    ids = _ingredientes(cliente, rec)

    r = cliente.post(
        f"/api/receitas/{rec}/enviar-para-lista",
        json={"ingrediente_ids": list(ids.values()), "fator": "0.5"},
    )
    assert r.status_code == 200
    lista_id = r.json()["lista_id"]
    itens_lista = _itens_lista(cliente, lista_id)
    assert itens_lista["FARINHA"]["nota"] == "250 g (Pão)"
    assert itens_lista["FERMENTO"]["nota"] == "½ colher (Pão)"

    r = cliente.post(
        f"/api/receitas/{rec}/enviar-para-lista",
        json={"ingrediente_ids": [ids["Farinha"]], "fator": 3, "lista_id": lista_id},
    )
    assert r.status_code == 200
    assert r.json()["ja_na_lista"] == 1
    farinha = _itens_lista(cliente, lista_id)["FARINHA"]
    assert farinha["nota"] == "250 g (Pão); 1500 g (Pão)"
    assert farinha["quantidade"] == "1.000"


@pytest.mark.parametrize("fator", [0.5, "0", "-1", "21", "abc", "NaN", "Infinity", True, ""])
def test_enviar_receita_recusa_fator_invalido(cliente, fator):
    rec = _receita_api(cliente, "Pão", 1, [("Farinha", "500", "g")])
    ids = _ingredientes(cliente, rec)

    r = cliente.post(
        f"/api/receitas/{rec}/enviar-para-lista",
        json={"ingrediente_ids": [ids["Farinha"]], "fator": fator},
    )

    assert r.status_code == 422
    assert cliente.get("/api/listas").json()["listas"] == []


def test_enviar_receita_junta_ingredientes_do_mesmo_produto(cliente):
    rec = _receita_api(
        cliente,
        "Bolo",
        1,
        [("Farinha", "300", "g"), ("farinha", "200", "g"), ("FARINHA", "1", "xícara")],
    )
    ids = [i["id"] for i in cliente.get(f"/api/receitas/{rec}").json()["ingredientes"]]

    r = cliente.post(f"/api/receitas/{rec}/enviar-para-lista", json={"ingrediente_ids": ids})

    assert r.status_code == 200
    assert r.json()["adicionados"] == 1
    assert r.json()["ja_na_lista"] == 0
    itens_lista = _itens_lista(cliente, r.json()["lista_id"])
    assert list(itens_lista) == ["FARINHA"]
    assert itens_lista["FARINHA"]["nota"] == "500 g + 1 xícara (Bolo)"


def test_enviar_receita_para_lista_inexistente(cliente):
    rec = _receita_api(cliente, "Pão", 1, [("Farinha", "500", "g")])
    ids = _ingredientes(cliente, rec)
    r = cliente.post(
        f"/api/receitas/{rec}/enviar-para-lista",
        json={"ingrediente_ids": [ids["Farinha"]], "lista_id": 999},
    )
    assert r.status_code == 404


@pytest.mark.parametrize("ids_enviados", [[], [999999]])
def test_enviar_receita_sem_ingrediente_valido_nao_cria_lista(cliente, ids_enviados):
    rec = _receita_api(cliente, "Pão", 1, [("Farinha", "500", "g")])
    r = cliente.post(
        f"/api/receitas/{rec}/enviar-para-lista", json={"ingrediente_ids": ids_enviados}
    )
    assert r.status_code == 422
    assert cliente.get("/api/listas").json()["listas"] == []
