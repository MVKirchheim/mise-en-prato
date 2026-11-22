"""Nenhum dado interpolado dentro de handler inline (`onclick="..."` etc.).

Escapar com entidades HTML não protege um handler inline: o parser decodifica
`&#39;`/`&quot;` antes de o JS rodar, e um nome vindo da nota fiscal vira
código. Dentro de `on*="..."` só pode entrar `${Number(...)}` ou uma das
variáveis numéricas da lista abaixo; texto vai em `data-*` escapado.
"""

import re
from pathlib import Path

WEB = Path(__file__).parent.parent / "mise_en_prato" / "web"

# Variáveis numéricas já usadas inline na aba Lista (ids e quantidades).
PERMITIDAS = {"i.produto_id", "produto_id", "passo", "p"}

INICIO_HANDLER = re.compile(r"\bon[a-z]+\s*=\s*([\"'])", re.IGNORECASE)


def _interpolacoes_em_handlers(fonte: str):
    """Devolve (linha, expressão) de cada `${...}` dentro de um handler inline."""
    for m in INICIO_HANDLER.finditer(fonte):
        aspa, pos = m.group(1), m.end()
        while pos < len(fonte) and fonte[pos] != aspa:
            if fonte.startswith("${", pos):
                inicio, nivel, pos = pos + 2, 1, pos + 2
                while pos < len(fonte) and nivel:
                    nivel += {"{": 1, "}": -1}.get(fonte[pos], 0)
                    pos += 1
                yield fonte.count("\n", 0, inicio) + 1, fonte[inicio : pos - 1].strip()
            else:
                pos += 1


def _permitida(expr: str) -> bool:
    return expr in PERMITIDAS or re.fullmatch(r"Number\([^()]*\)", expr) is not None


def test_handlers_inline_sem_dado_interpolado():
    achados = [
        f"{arq.name}:{linha}: ${{{expr}}}"
        for arq in sorted(WEB.glob("*.js"))
        for linha, expr in _interpolacoes_em_handlers(arq.read_text(encoding="utf-8"))
        if not _permitida(expr)
    ]
    assert not achados, "dado interpolado em handler inline:\n" + "\n".join(achados)


def test_detector_pega_o_caso_perigoso():
    perigoso = (
        "`<b onclick=\"ver('${c.chave}')\">` + `<i onclick='f(${Number(x)})'>`"
        " + `<u onClick = \"g('${c.loja}')\">`"
    )
    assert [e for _, e in _interpolacoes_em_handlers(perigoso)] == [
        "c.chave",
        "Number(x)",
        "c.loja",
    ]
    assert not _permitida("c.chave")
    assert _permitida("Number(x)")
