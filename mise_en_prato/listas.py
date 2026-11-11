#!/usr/bin/env python3
"""Lista de compras, e a pergunta que ela existe para responder:

    "levando ESTA lista, qual mercado sai mais barato?"

A resposta nunca é só um número. Vem sempre com a **cobertura** — quantos itens
da lista aquela loja tem preço conhecido — porque loja que só cobre metade da
lista fatalmente parece a mais barata, e deixá-la ganhar em silêncio seria
mentir com estatística verdadeira.

Os preços saem de duas fontes na mesma série: o que a casa pagou (cupom) e o
que a região praticou (menor_preco, consultado sob demanda). Preço da própria
casa é fato consumado; da região é informação de terceiro, já validada pelo
sanidade.py antes de chegar aqui.
"""

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from .banco import milesimos, reais, unidades
from .receitas import formatar_nota

# Janela larga DE PROPÓSITO. A API já devolve só o que ela considera vigente —
# e "vigente" para ela inclui preço de duas semanas atrás, medido em 19/08/2026:
# com data=7 vieram observações de 05/08. Filtrar de novo em 7 dias aqui era
# jogar fora dado que a fonte deu por bom, e fazia loja com preço válido sumir
# da comparação como se não vendesse o item. Quem julga se está velho é a tela,
# que mostra a data ao lado.
JANELA_PADRAO = 30
SEM_NOME = "loja não identificada"


# ── a lista ──────────────────────────────────────────────────────────────────
def _inserir_lista(con: sqlite3.Connection, nome: str, morador: str) -> int:
    return con.execute(
        "INSERT INTO lista (nome, morador, criada_em) VALUES (?,?,?)",
        (nome, morador, datetime.now().isoformat(sep=" ", timespec="seconds")),
    ).lastrowid


def criar(con: sqlite3.Connection, nome: str, morador: str) -> int:
    with con:
        return _inserir_lista(con, nome, morador)


def listar(con: sqlite3.Connection) -> list[dict]:
    return [
        {
            "id": r["id"],
            "nome": r["nome"],
            "morador": r["morador"],
            "criada_em": r["criada_em"],
            "itens": r["itens"],
        }
        for r in con.execute("""
            SELECT l.*, (SELECT COUNT(*) FROM lista_item li WHERE li.lista_id = l.id) AS itens
              FROM lista l ORDER BY l.criada_em DESC""")
    ]


def por_produto(
    con: sqlite3.Connection, lista_id: int, produto_id: int, quantidade: Decimal | None = None
) -> None:
    """Põe (ou atualiza) um item na lista. A quantidade é sobrescrita; a nota não."""
    with con:
        con.execute(
            """INSERT INTO lista_item (lista_id, produto_id, quantidade_milesimos)
               VALUES (?,?,?)
               ON CONFLICT(lista_id, produto_id)
               DO UPDATE SET quantidade_milesimos = excluded.quantidade_milesimos""",
            (lista_id, produto_id, milesimos(quantidade or Decimal("1"))),
        )


# ── o que vem das receitas ───────────────────────────────────────────────────
# Receita pede "500 g de farinha"; a lista diz quantos pacotes comprar. São
# grandezas diferentes, e por isso não dividem o mesmo campo: a quantidade
# continua sendo de compra (1 se o produto é novo na lista, intocada se já
# estava) e o pedido das receitas vai para a nota, em texto.
class ListaNaoEncontrada(LookupError):
    """A lista de compras pedida não existe."""


@dataclass
class Pedido:
    """O que uma ou mais receitas pedem de um ingrediente."""

    nome: str
    gtin: str | None
    partes: list[tuple[Decimal, str]]
    receitas: list[str]


def produto_para_ingrediente(con: sqlite3.Connection, nome: str, gtin: str | None = None) -> int:
    """O produto do catálogo que corresponde ao ingrediente; cria um se não houver.

    Procura pelo código de barras, depois pela descrição sem diferença de
    maiúscula. Não faz commit: roda dentro da transação de quem chama.
    """
    if gtin:
        row = con.execute("SELECT id FROM produto WHERE gtin = ? LIMIT 1", (gtin,)).fetchone()
        if row:
            return row["id"]
    row = con.execute(
        "SELECT id FROM produto WHERE UPPER(descricao_canonica) = UPPER(?) LIMIT 1",
        (nome.strip(),),
    ).fetchone()
    if row:
        return row["id"]
    cur = con.execute(
        "INSERT INTO produto (descricao_canonica, gtin, acompanhar) VALUES (?, ?, 0)",
        (nome.strip().upper(), gtin),
    )
    return cur.lastrowid


def mesclar_nota(existente: str | None, nova: str | None) -> str | None:
    """Junta a nota nova à que o item já tem, com "; ".

    Se o texto novo já é um dos trechos do antigo, nada muda — mandar a mesma
    receita duas vezes não repete a nota. A comparação é por trecho inteiro, e
    não por pedaço de texto: "500 g (Bolo)" não pode ser dado como presente só
    porque a nota tem "1500 g (Bolo)".
    """
    if not nova:
        return existente
    if not existente:
        return nova
    if nova in existente.split("; "):
        return existente
    return f"{existente}; {nova}"


def acrescentar_de_receita(
    con: sqlite3.Connection, lista_id: int, produto_id: int, nota: str | None
) -> bool:
    """Põe o produto na lista vindo de receita. Devolve True se ele era novo nela.

    Novo: entra com quantidade 1 e a nota. Já na lista: a quantidade fica como
    está (quem decidiu comprar 3 continua comprando 3) e só a nota é mesclada.
    Não faz commit: roda dentro da transação de quem chama.
    """
    linha = con.execute(
        "SELECT nota FROM lista_item WHERE lista_id = ? AND produto_id = ?",
        (lista_id, produto_id),
    ).fetchone()
    if linha is None:
        con.execute(
            "INSERT INTO lista_item (lista_id, produto_id, quantidade_milesimos, nota)"
            " VALUES (?,?,?,?)",
            (lista_id, produto_id, milesimos(Decimal("1")), nota),
        )
        return True
    nova = mesclar_nota(linha["nota"], nota)
    if nova != linha["nota"]:
        con.execute(
            "UPDATE lista_item SET nota = ? WHERE lista_id = ? AND produto_id = ?",
            (nova, lista_id, produto_id),
        )
    return False


def enviar_de_receitas(
    con: sqlite3.Connection,
    pedidos: list[Pedido],
    morador: str,
    lista_id: int | None = None,
    nome_nova_lista: str = "compras",
) -> dict:
    """Leva os pedidos das receitas para uma lista, numa transação só.

    Sem `lista_id`, cria a lista. Pedidos que caem no mesmo produto (dois
    ingredientes com o mesmo código de barras, ou "Farinha" e "farinha") viram
    uma nota só. Devolve {"lista_id", "adicionados", "ja_na_lista"}.
    """
    if lista_id is not None and (
        con.execute("SELECT 1 FROM lista WHERE id = ?", (lista_id,)).fetchone() is None
    ):
        raise ListaNaoEncontrada(f"Lista {lista_id} não encontrada.")

    with con:
        if lista_id is None:
            lista_id = _inserir_lista(con, nome_nova_lista, morador)
        por_produto_id: dict[int, tuple[list, list]] = {}
        for pedido in pedidos:
            produto_id = produto_para_ingrediente(con, pedido.nome, pedido.gtin)
            partes, nomes = por_produto_id.setdefault(produto_id, ([], []))
            partes.extend(pedido.partes)
            nomes.extend(pedido.receitas)
        adicionados = ja_na_lista = 0
        for produto_id, (partes, nomes) in por_produto_id.items():
            nota = formatar_nota(partes, sorted(set(nomes), key=str.casefold))
            if acrescentar_de_receita(con, lista_id, produto_id, nota):
                adicionados += 1
            else:
                ja_na_lista += 1
    return {"lista_id": lista_id, "adicionados": adicionados, "ja_na_lista": ja_na_lista}


def tirar(con: sqlite3.Connection, lista_id: int, produto_id: int) -> None:
    with con:
        con.execute(
            "DELETE FROM lista_item WHERE lista_id = ? AND produto_id = ?", (lista_id, produto_id)
        )


def itens(con: sqlite3.Connection, lista_id: int) -> list[dict]:
    return [
        {
            "produto_id": r["produto_id"],
            "descricao": r["apelido"] or r["descricao_canonica"],
            "gtin": r["gtin"],
            "quantidade": unidades(r["quantidade_milesimos"]),
            "nota": r["nota"],
            "is_granel": bool(r["is_granel"]),
            "ultimo_preco": reais(r["ultimo_centavos"])
            if r["ultimo_centavos"] is not None
            else None,
        }
        for r in con.execute(
            """
            SELECT li.produto_id, li.quantidade_milesimos, li.nota, p.descricao_canonica,
                   p.apelido, p.gtin, p.is_granel,
                   COALESCE(
                       (SELECT i2.unitario_centavos FROM item i2
                          JOIN cupom c2 ON c2.chave = i2.chave
                         WHERE i2.produto_id = p.id ORDER BY c2.emitido_em DESC LIMIT 1),
                       (SELECT pr.unitario_centavos FROM preco pr
                         WHERE pr.produto_id = p.id ORDER BY pr.observado_em DESC LIMIT 1)
                   ) AS ultimo_centavos
              FROM lista_item li JOIN produto p ON p.id = li.produto_id
             WHERE li.lista_id = ? ORDER BY p.descricao_canonica""",
            (lista_id,),
        )
    ]


def sugerir(con: sqlite3.Connection, limite: int = 20) -> list[dict]:
    """O que a casa mais compra — o ponto de partida de qualquer lista.

    Ordena por número de compras e não por gasto: arroz comprado toda semana
    interessa mais numa lista do que um churrasco caro comprado uma vez.
    """
    return [
        {
            "produto_id": r["id"],
            "descricao": r["apelido"] or r["descricao_canonica"],
            "compras": r["compras"],
            "ultimo": reais(r["ultimo"]),
            "is_granel": bool(r["is_granel"]),
        }
        for r in con.execute(
            """
            SELECT p.id, p.descricao_canonica, p.apelido, p.is_granel,
                   COUNT(DISTINCT i.chave) AS compras,
                   (SELECT i2.unitario_centavos FROM item i2
                      JOIN cupom c2 ON c2.chave = i2.chave
                     WHERE i2.produto_id = p.id ORDER BY c2.emitido_em DESC LIMIT 1) AS ultimo
              FROM produto p JOIN item i ON i.produto_id = p.id
             GROUP BY p.id ORDER BY compras DESC, p.descricao_canonica LIMIT ?""",
            (limite,),
        )
    ]


# ── a comparação ─────────────────────────────────────────────────────────────
def _precos_recentes(con: sqlite3.Connection, produto_ids: list[int], dias: int) -> list[dict]:
    """Preço mais recente de cada produto em cada loja, dentro da janela."""
    if not produto_ids:
        return []
    limite = (datetime.now() - timedelta(days=dias)).isoformat(sep=" ", timespec="seconds")
    marcadores = ",".join("?" * len(produto_ids))
    return [
        dict(r)
        for r in con.execute(
            f"""
        SELECT produto_id, loja_id, loja, unitario_centavos, observado_em, fonte, distancia_km
          FROM (
            SELECT pr.produto_id, pr.distancia_km,
                   COALESCE(pr.cnpj, pr.codigo_menor_preco, pr.loja, '?') AS loja_id,
                   COALESCE(e.razao, pr.loja, ?) AS loja,
                   pr.unitario_centavos, pr.observado_em, pr.fonte,
                   ROW_NUMBER() OVER (
                       PARTITION BY pr.produto_id,
                                    COALESCE(pr.cnpj, pr.codigo_menor_preco, pr.loja, '?')
                       ORDER BY pr.observado_em DESC) AS ordem
              FROM preco pr LEFT JOIN estabelecimento e ON e.cnpj = pr.cnpj
             WHERE pr.produto_id IN ({marcadores})
               AND COALESCE(pr.observado_em, '') >= ?)
         WHERE ordem = 1""",  # noqa: S608 — marcadores são "?", não dado
            [SEM_NOME, *produto_ids, limite],
        )
    ]


def comparar(con: sqlite3.Connection, lista_id: int, dias: int = JANELA_PADRAO) -> dict:
    """Quanto a lista custa em cada loja, e o que falta em cada uma."""
    da_lista = itens(con, lista_id)
    if not da_lista:
        return {"lista_id": lista_id, "itens": 0, "lojas": [], "sem_preco": []}

    quantidade = {i["produto_id"]: i["quantidade"] for i in da_lista}
    nome = {i["produto_id"]: i["descricao"] for i in da_lista}
    precos = _precos_recentes(con, list(quantidade), dias)

    lojas: dict[str, dict] = {}
    for p in precos:
        loja = lojas.setdefault(
            p["loja_id"],
            {
                "loja": p["loja"],
                "total": Decimal("0"),
                "itens": [],
                "fontes": set(),
                "distancias": [],
            },
        )
        if p["distancia_km"] is not None:
            loja["distancias"].append(p["distancia_km"])
        valor = reais(p["unitario_centavos"]) * quantidade[p["produto_id"]]
        loja["total"] += valor
        loja["fontes"].add(p["fonte"])
        loja["itens"].append(
            {
                "produto_id": p["produto_id"],
                "descricao": nome[p["produto_id"]],
                "unitario": reais(p["unitario_centavos"]),
                "subtotal": valor,
                "fonte": p["fonte"],
                "observado_em": p["observado_em"],
            }
        )

    resultado = []
    for dados in lojas.values():
        tem = {i["produto_id"] for i in dados["itens"]}
        resultado.append(
            {
                "loja": dados["loja"],
                # Rede grande tem várias filiais, e a API dá um código por filial.
                # Sem a distância, três linhas "COOPERATIVA AGROINDUSTRIAL LAR" com
                # totais diferentes são indistinguíveis na tela.
                "distancia_km": (
                    round(min(dados["distancias"]), 1) if dados["distancias"] else None
                ),
                "total": dados["total"].quantize(Decimal("0.01")),
                # cobertura vai SEMPRE junto do total: sem ela, a loja que só tem
                # metade da lista parece a mais barata e ganha por omissão.
                "cobertura": len(tem),
                "de": len(quantidade),
                "completa": len(tem) == len(quantidade),
                "falta": sorted(nome[i] for i in set(quantidade) - tem),
                "fontes": sorted(dados["fontes"]),
                # a observação mais antiga do lote: é o que permite desconfiar de um
                # total barato demais sem precisar abrir loja por loja
                "mais_antigo": min((i["observado_em"] or "") for i in dados["itens"])[:10],
                "itens": sorted(dados["itens"], key=lambda i: i["descricao"]),
            }
        )

    # completas primeiro, e entre elas a mais barata. Comparar total de listas
    # de tamanhos diferentes seria comparar coisas diferentes.
    resultado.sort(key=lambda r: (not r["completa"], r["total"]))
    com_preco = {p["produto_id"] for p in precos}
    return {
        "lista_id": lista_id,
        "itens": len(quantidade),
        "lojas": resultado,
        "sem_preco": sorted(nome[i] for i in set(quantidade) - com_preco),
        "janela_dias": dias,
    }
