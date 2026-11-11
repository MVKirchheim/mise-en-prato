#!/usr/bin/env python3
"""Cardápio semanal: o plano de almoço e jantar da casa, de segunda a domingo.

Cada entrada mora numa "casa" — uma data e uma refeição — e pode apontar para
uma receita do livro ou ser só texto livre ("pizza da esquina"). Marcar a
entrada como feita grava quando; com o tempo, o plano vira o diário do que a
casa de fato cozinhou.

O título é gravado na entrada, e não lido da receita a cada vez: renomear ou
apagar a receita não reescreve o passado. Apagada a receita, a entrada fica
com o título e sem o vínculo (ON DELETE SET NULL na migração 013).

A semana é sempre calculada a partir de uma data que o navegador manda na hora
local dele; aqui só se acha a segunda-feira.
"""

import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from . import receitas

REFEICOES = ("almoco", "jantar")

_FORMATO_DATA = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# Marca de "campo não enviado" na atualização parcial: None é um valor válido
# para porções (limpar), então não pode servir de ausência.
_NAO_INFORMADO: Any = object()


class EntradaNaoEncontrada(LookupError):
    """A entrada do cardápio pedida não existe."""


class ReceitaNaoEncontrada(LookupError):
    """A receita apontada pela entrada não existe."""


@dataclass
class Entrada:
    id: int
    data: str
    refeicao: str
    titulo: str
    porcoes: int | None
    ordem: int
    feito: bool
    feito_em: str | None
    receita_id: int | None
    receita_tem_foto: bool
    morador: str | None
    criada_em: str

    def __getitem__(self, item: str) -> Any:
        return getattr(self, item)


def _agora() -> str:
    return datetime.now().isoformat(sep=" ", timespec="seconds")


# ── validação ────────────────────────────────────────────────────────────────
def validar_data(valor: date | str) -> str:
    """Devolve a data em AAAA-MM-DD, ou ValueError se não for uma data real.

    O formato é conferido antes do fromisoformat porque, desde o Python 3.11,
    ele aceita também "20261111" e "2026-W45-3" — que não são o que a tela
    manda nem o que o banco guarda.
    """
    if isinstance(valor, datetime):
        return valor.date().isoformat()
    if isinstance(valor, date):
        return valor.isoformat()
    if not isinstance(valor, str) or not _FORMATO_DATA.match(valor.strip()):
        raise ValueError("Data inválida: use o formato AAAA-MM-DD.")
    try:
        return date.fromisoformat(valor.strip()).isoformat()
    except ValueError as erro:
        raise ValueError(f"Data inválida: {valor.strip()} não existe no calendário.") from erro


def validar_refeicao(valor: str) -> str:
    if valor not in REFEICOES:
        raise ValueError("Refeição inválida: use 'almoco' ou 'jantar'.")
    return valor


def validar_porcoes(valor: int | None) -> int | None:
    """Porções: inteiro maior que zero, ou None. Bool e float são recusados."""
    if valor is None:
        return None
    if isinstance(valor, bool) or not isinstance(valor, int):
        raise TypeError("Porções devem ser um número inteiro, sem float.")
    if valor <= 0:
        raise ValueError("O número de porções deve ser maior que zero.")
    return valor


def _validar_ordem(valor: int) -> int:
    if isinstance(valor, bool) or not isinstance(valor, int):
        raise TypeError("A ordem deve ser um número inteiro.")
    if valor < 0:
        raise ValueError("A ordem não pode ser negativa.")
    return valor


def _validar_titulo(valor: str | None) -> str:
    if not isinstance(valor, str) or not valor.strip():
        raise ValueError("O título da entrada não pode ser vazio.")
    return valor.strip()


# ── semana ───────────────────────────────────────────────────────────────────
def semana_de(dia: date) -> tuple[date, date]:
    """A segunda e o domingo da semana que contém `dia`."""
    if isinstance(dia, datetime):
        dia = dia.date()
    segunda = dia - timedelta(days=dia.weekday())
    return segunda, segunda + timedelta(days=6)


_SELECT_ENTRADA = """
    SELECT e.id, e.data, e.refeicao, e.receita_id, e.titulo, e.porcoes, e.ordem,
           e.feito_em, e.morador, e.criada_em,
           (r.foto_caminho IS NOT NULL AND r.foto_caminho != '') AS receita_tem_foto
      FROM cardapio_entrada e
      LEFT JOIN receitas r ON r.id = e.receita_id
"""


def _entrada(linha: sqlite3.Row) -> Entrada:
    return Entrada(
        id=linha["id"],
        data=linha["data"],
        refeicao=linha["refeicao"],
        titulo=linha["titulo"],
        porcoes=linha["porcoes"],
        ordem=linha["ordem"],
        feito=linha["feito_em"] is not None,
        feito_em=linha["feito_em"],
        receita_id=linha["receita_id"],
        receita_tem_foto=bool(linha["receita_tem_foto"]),
        morador=linha["morador"],
        criada_em=linha["criada_em"],
    )


def listar_semana(con: sqlite3.Connection, dia: date) -> dict:
    """Os sete dias da semana de `dia`, cada um com almoço e jantar em ordem.

    Devolve {"inicio", "fim", "dias": [{"data", "almoco": [...], "jantar": [...]}]}.
    Dia sem nada planejado vem com as duas listas vazias.
    """
    segunda, domingo = semana_de(dia)
    dias = {
        (segunda + timedelta(days=i)).isoformat(): {"almoco": [], "jantar": []} for i in range(7)
    }
    linhas = con.execute(
        _SELECT_ENTRADA
        + """
         WHERE e.data BETWEEN ? AND ?
         ORDER BY e.data, e.refeicao, e.ordem, e.id
        """,
        (segunda.isoformat(), domingo.isoformat()),
    ).fetchall()
    for linha in linhas:
        dias[linha["data"]][linha["refeicao"]].append(_entrada(linha))
    return {
        "inicio": segunda.isoformat(),
        "fim": domingo.isoformat(),
        "dias": [
            {"data": d, "almoco": casas["almoco"], "jantar": casas["jantar"]}
            for d, casas in dias.items()
        ],
    }


# ── entradas ─────────────────────────────────────────────────────────────────
def obter_entrada(con: sqlite3.Connection, entrada_id: int) -> Entrada | None:
    linha = con.execute(_SELECT_ENTRADA + " WHERE e.id = ?", (entrada_id,)).fetchone()
    return None if linha is None else _entrada(linha)


def _proxima_ordem(con: sqlite3.Connection, data: str, refeicao: str) -> int:
    """A posição logo depois da última entrada da casa (0 se a casa está vazia)."""
    linha = con.execute(
        "SELECT MAX(ordem) AS m FROM cardapio_entrada WHERE data = ? AND refeicao = ?",
        (data, refeicao),
    ).fetchone()
    return 0 if linha["m"] is None else linha["m"] + 1


def criar_entrada(
    con: sqlite3.Connection,
    data: date | str,
    refeicao: str,
    receita_id: int | None = None,
    titulo: str | None = None,
    porcoes: int | None = None,
    feito: bool = False,
    morador: str | None = None,
) -> Entrada:
    """Põe uma entrada no fim da casa (data + refeição) e a devolve.

    Com `receita_id`, a receita tem de existir; título e porções vêm dela quando
    não informados. Sem receita, o título é obrigatório e as porções ficam
    vazias se não vierem. `feito=True` já cria marcada — dá para registrar o
    que foi comido sem ter planejado antes.
    """
    data_str = validar_data(data)
    validar_refeicao(refeicao)
    porcoes = validar_porcoes(porcoes)
    if not isinstance(feito, bool):
        raise TypeError("O campo feito deve ser verdadeiro ou falso.")

    titulo_limpo = titulo.strip() if isinstance(titulo, str) else None
    if receita_id is None:
        if titulo is None:
            raise ValueError("Informe uma receita ou um título para a entrada do cardápio.")
        titulo_final = _validar_titulo(titulo)
    else:
        if isinstance(receita_id, bool) or not isinstance(receita_id, int):
            raise TypeError("O id da receita deve ser um número inteiro.")
        receita = con.execute(
            "SELECT nome, porcoes FROM receitas WHERE id = ?", (receita_id,)
        ).fetchone()
        if receita is None:
            raise ReceitaNaoEncontrada(f"Receita {receita_id} não encontrada.")
        titulo_final = titulo_limpo or receita["nome"]
        if porcoes is None:
            porcoes = receita["porcoes"]

    with con:
        cursor = con.execute(
            """
            INSERT INTO cardapio_entrada (
                data, refeicao, receita_id, titulo, porcoes, ordem, feito_em, morador, criada_em
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                data_str,
                refeicao,
                receita_id,
                titulo_final,
                porcoes,
                _proxima_ordem(con, data_str, refeicao),
                _agora() if feito else None,
                morador,
                _agora(),
            ),
        )
        entrada_id = cursor.lastrowid
    if entrada_id is None:
        raise RuntimeError("Falha ao obter ID da entrada criada.")
    criada = obter_entrada(con, entrada_id)
    if criada is None:
        raise RuntimeError("Erro ao obter entrada criada.")
    return criada


def atualizar_entrada(
    con: sqlite3.Connection,
    entrada_id: int,
    *,
    titulo: str = _NAO_INFORMADO,
    porcoes: int | None = _NAO_INFORMADO,
    data: date | str = _NAO_INFORMADO,
    refeicao: str = _NAO_INFORMADO,
    ordem: int = _NAO_INFORMADO,
    feito: bool = _NAO_INFORMADO,
) -> Entrada:
    """Altera só os campos informados e devolve a entrada como ficou.

    `feito=True` grava o momento (ou mantém o que já havia); `False` limpa.
    Mudar de dia ou refeição sem `ordem` leva a entrada para o fim da casa de
    destino. `porcoes=None` limpa as porções.
    """
    atual = obter_entrada(con, entrada_id)
    if atual is None:
        raise EntradaNaoEncontrada(f"Entrada {entrada_id} não encontrada no cardápio.")

    novo = {
        "titulo": atual.titulo,
        "porcoes": atual.porcoes,
        "data": atual.data,
        "refeicao": atual.refeicao,
        "ordem": atual.ordem,
        "feito_em": atual.feito_em,
    }
    if titulo is not _NAO_INFORMADO:
        novo["titulo"] = _validar_titulo(titulo)
    if porcoes is not _NAO_INFORMADO:
        novo["porcoes"] = validar_porcoes(porcoes)
    if data is not _NAO_INFORMADO:
        novo["data"] = validar_data(data)
    if refeicao is not _NAO_INFORMADO:
        novo["refeicao"] = validar_refeicao(refeicao)
    if feito is not _NAO_INFORMADO:
        if not isinstance(feito, bool):
            raise TypeError("O campo feito deve ser verdadeiro ou falso.")
        novo["feito_em"] = (atual.feito_em or _agora()) if feito else None

    mudou_de_casa = (novo["data"], novo["refeicao"]) != (atual.data, atual.refeicao)
    if ordem is not _NAO_INFORMADO:
        novo["ordem"] = _validar_ordem(ordem)
    elif mudou_de_casa:
        novo["ordem"] = _proxima_ordem(con, novo["data"], novo["refeicao"])

    with con:
        con.execute(
            """
            UPDATE cardapio_entrada
               SET titulo = ?, porcoes = ?, data = ?, refeicao = ?, ordem = ?, feito_em = ?
             WHERE id = ?
            """,
            (
                novo["titulo"],
                novo["porcoes"],
                novo["data"],
                novo["refeicao"],
                novo["ordem"],
                novo["feito_em"],
                entrada_id,
            ),
        )
    resultado = obter_entrada(con, entrada_id)
    if resultado is None:
        raise RuntimeError("Erro ao obter entrada atualizada.")
    return resultado


def excluir_entrada(con: sqlite3.Connection, entrada_id: int) -> None:
    with con:
        cur = con.execute("DELETE FROM cardapio_entrada WHERE id = ?", (entrada_id,))
    if cur.rowcount == 0:
        raise EntradaNaoEncontrada(f"Entrada {entrada_id} não encontrada no cardápio.")


# ── lista de compras da semana ───────────────────────────────────────────────
_MESES_CURTOS = ("jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez")


def _nome_normalizado(nome: str) -> str:
    """Nome sem espaços sobrando e sem caixa: "  Farinha  de Trigo" = "farinha de trigo"."""
    return " ".join(nome.split()).casefold()


def nome_padrao_lista(inicio: date, fim: date) -> str:
    """Nome da lista nova: "Cardápio 9 – 15 nov", ou "Cardápio 30 nov – 6 dez".

    O mesmo formato curto do título da semana na tela.
    """
    if inicio.month == fim.month:
        return f"Cardápio {inicio.day} – {fim.day} {_MESES_CURTOS[fim.month - 1]}"
    return (
        f"Cardápio {inicio.day} {_MESES_CURTOS[inicio.month - 1]}"
        f" – {fim.day} {_MESES_CURTOS[fim.month - 1]}"
    )


def lista_da_semana(con: sqlite3.Connection, dia: date) -> dict:
    """O que comprar para as receitas planejadas na semana de `dia`.

    Entram só entradas com receita e ainda não feitas: texto livre não tem
    ingrediente, e o que já foi cozinhado não precisa mais ser comprado. Cada
    receita é escalada para as porções da entrada. Os ingredientes se agrupam
    pelo código de barras quando têm, senão pelo nome normalizado; dentro do
    grupo, quantidades na mesma unidade são somadas (sem converter unidades).

    Devolve {"inicio", "fim", "entradas", "ignoradas", "grupos": [...]}, onde
    `ignoradas` conta entradas cuja receita não existe mais e cada grupo é
    {"chave", "nome", "gtin", "partes": [{"quantidade", "unidade"}],
    "receitas", "nota"}.
    """
    segunda, domingo = semana_de(dia)
    linhas = con.execute(
        """
        SELECT receita_id, porcoes
          FROM cardapio_entrada
         WHERE data BETWEEN ? AND ?
           AND receita_id IS NOT NULL
           AND feito_em IS NULL
         ORDER BY data, refeicao, ordem, id
        """,
        (segunda.isoformat(), domingo.isoformat()),
    ).fetchall()

    grupos: dict[str, dict] = {}
    entradas = ignoradas = 0
    livro: dict[int, receitas.Receita | None] = {}
    for linha in linhas:
        receita_id = linha["receita_id"]
        if receita_id not in livro:
            livro[receita_id] = receitas.obter_receita(con, receita_id)
        receita = livro[receita_id]
        if receita is None:
            ignoradas += 1
            continue
        entradas += 1
        porcoes = linha["porcoes"]
        if porcoes and porcoes != receita.porcoes:
            receita = receitas.escalar_receita(receita, porcoes)
        for ing in receita.ingredientes:
            gtin = (ing.gtin_associado or "").strip() or None
            chave = f"gtin:{gtin}" if gtin else f"nome:{_nome_normalizado(ing.nome_ingrediente)}"
            grupo = grupos.setdefault(
                chave,
                {
                    "chave": chave,
                    "nome": ing.nome_ingrediente,
                    "gtin": gtin,
                    "partes": [],
                    "receitas": set(),
                },
            )
            grupo["partes"].append((ing.quantidade, ing.unidade))
            grupo["receitas"].add(receita.nome)

    resultado = []
    for grupo in grupos.values():
        partes = receitas.somar_por_unidade(grupo["partes"])
        nomes = sorted(grupo["receitas"], key=str.casefold)
        resultado.append(
            {
                "chave": grupo["chave"],
                "nome": grupo["nome"],
                "gtin": grupo["gtin"],
                "partes": [{"quantidade": q, "unidade": u} for q, u in partes],
                "receitas": nomes,
                "nota": receitas.formatar_nota(partes, nomes),
            }
        )
    resultado.sort(key=lambda g: (g["nome"].casefold(), g["chave"]))
    return {
        "inicio": segunda.isoformat(),
        "fim": domingo.isoformat(),
        "entradas": entradas,
        "ignoradas": ignoradas,
        "grupos": resultado,
    }
