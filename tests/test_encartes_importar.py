"""CLI de encartes: `importar` põe uma imagem do disco na fila, `reler` a devolve."""

import hashlib
from datetime import datetime
from pathlib import Path

import pytest

from mise_en_prato import encartes
from mise_en_prato.banco import conectar
from mise_en_prato.config import ModoDemo, config
from tests.test_encartes import GRUPO_ESTRANHO, GRUPO_LAR, LAR, MAX, _arquivos, _contagens
from tests.test_leitor_encartes import LeitorFalso, _leitura, _oferta

FUSO = encartes.FUSO
SHA_LAR = hashlib.sha256(LAR).hexdigest()
AGORA = datetime(2026, 11, 21, 13, 0, tzinfo=FUSO)


@pytest.fixture
def dados(tmp_path, monkeypatch):
    """A pasta de dados da CLI (DADOS), em modo dev."""
    monkeypatch.setenv("DADOS", str(tmp_path))
    monkeypatch.setenv("APP_MODE", "dev")
    config.cache_clear()
    return tmp_path


@pytest.fixture
def con(dados):
    con = conectar(dados / "mise_en_prato.db")
    yield con
    con.close()


@pytest.fixture
def pasta(dados):
    return dados / "encartes"


def _foto(dados: Path, conteudo: bytes = LAR, nome: str = "foto.png") -> Path:
    alvo = dados / "importar" / nome
    alvo.parent.mkdir(parents=True, exist_ok=True)
    alvo.write_bytes(conteudo)
    return alvo


def _lar(con) -> int:
    return encartes.adicionar_fonte(con, GRUPO_LAR, "LAR Centro")


def _encarte(con, encarte_id: int) -> dict:
    return dict(con.execute("SELECT * FROM encarte WHERE id = ?", (encarte_id,)).fetchone())


def _ofertas_do(con, encarte_id: int) -> list[dict]:
    return [
        dict(r)
        for r in con.execute("SELECT * FROM oferta WHERE encarte_id = ? ORDER BY id", (encarte_id,))
    ]


def _demo(monkeypatch):
    monkeypatch.setenv("APP_MODE", "demo")
    config.cache_clear()


# ── importar ─────────────────────────────────────────────────────────────────
def test_importar_caminho_feliz(dados, con, capsys):
    fonte_id = _lar(con)
    foto = _foto(dados)
    argv = ["importar", str(fonte_id), str(foto), "--legenda", " Super Promoção "]
    assert encartes.main([*argv, "--recebido-em", "2026-11-21"]) == 0

    [linha] = [dict(r) for r in con.execute("SELECT * FROM encarte")]
    assert linha["fonte_id"] == fonte_id
    assert linha["mensagem_id"] == f"manual:{SHA_LAR}"
    assert linha["status"] == "pendente"
    assert linha["tentativas"] == 0
    assert linha["legenda"] == "Super Promoção"
    assert linha["sha256"] == SHA_LAR
    assert linha["mime"] == "image/png"
    # Só a data: vale o meio-dia local daquele dia.
    assert linha["recebido_em"] == "2026-11-21T12:00:00-03:00"
    assert linha["arquivo"] == f"encartes/2026-11-21/{SHA_LAR}.png"
    assert _arquivos(dados / "encartes") == [dados / linha["arquivo"]]
    assert (dados / linha["arquivo"]).read_bytes() == LAR

    saida = capsys.readouterr().out
    assert f"Encarte {linha['id']} pendente (LAR Centro)" in saida
    assert "Super" not in saida  # nada além de id, fonte e status


def test_importar_com_hora_e_por_jid(dados, con):
    _lar(con)
    foto = _foto(dados)
    assert (
        encartes.main(["importar", GRUPO_LAR, str(foto), "--recebido-em", "2026-11-20T08:15"]) == 0
    )
    linha = con.execute("SELECT recebido_em, arquivo, legenda FROM encarte").fetchone()
    assert linha["recebido_em"] == "2026-11-20T08:15:00-03:00"
    assert linha["arquivo"].startswith("encartes/2026-11-20/")
    assert linha["legenda"] is None


def test_importar_sem_data_vale_agora(dados, con, pasta):
    fonte_id = _lar(con)
    antes = datetime.now(FUSO).replace(microsecond=0)
    codigo, encarte_id, _ = encartes.importar_imagem(
        con, fonte_id, _foto(dados), pasta, datetime.now(FUSO)
    )
    assert codigo == encartes.GUARDADO
    recebido = datetime.fromisoformat(_encarte(con, encarte_id)["recebido_em"])
    assert antes <= recebido <= datetime.now(FUSO)


@pytest.mark.parametrize("valor", ["21/11/2026", "2026-11-21 08:15", "2026-13-01", "ontem"])
def test_importar_data_invalida_nao_grava(dados, con, valor):
    fonte_id = _lar(con)
    with pytest.raises(SystemExit) as saida:
        encartes.main(["importar", str(fonte_id), str(_foto(dados)), "--recebido-em", valor])
    assert saida.value.code != 0
    assert _contagens(con)["encarte"] == 0


def test_importar_repetido(dados, con, capsys):
    fonte_id = _lar(con)
    foto = _foto(dados)
    assert encartes.main(["importar", str(fonte_id), str(foto)]) == 0
    [encarte_id] = [r[0] for r in con.execute("SELECT id FROM encarte")]
    capsys.readouterr()
    # A mesma imagem com outro nome e outro dia continua sendo a mesma.
    outra = _foto(dados, nome="copia.png")
    argv = ["importar", str(fonte_id), str(outra), "--recebido-em", "2026-11-23"]
    assert encartes.main(argv) == 0
    assert f"Já importado: encarte {encarte_id}" in capsys.readouterr().out
    assert _contagens(con)["encarte"] == 1
    assert len(_arquivos(dados / "encartes")) == 1


def test_importar_imagem_invalida(dados, con, capsys):
    fonte_id = _lar(con)
    falso = _foto(dados, b"\xff\xd8\xff\xe0" + b"nao e jpeg" * 100, "falso.jpg")
    assert encartes.main(["importar", str(fonte_id), str(falso)]) == 1
    assert "IMAGEM_INVALIDA" in capsys.readouterr().out
    assert _contagens(con)["encarte"] == 0
    assert _arquivos(dados / "encartes") == []


def test_importar_grande_demais(dados, con, capsys):
    fonte_id = _lar(con)
    grande = _foto(dados, LAR + b"\0" * (encartes.LIMITE_IMAGEM + 1 - len(LAR)), "grande.png")
    assert encartes.main(["importar", str(fonte_id), str(grande)]) == 1
    assert "GRANDE_DEMAIS" in capsys.readouterr().out
    assert _contagens(con)["encarte"] == 0
    assert _arquivos(dados / "encartes") == []


def test_importar_fonte_inativa_ou_inexistente(dados, con, capsys):
    fonte_id = _lar(con)
    assert encartes.desativar_fonte(con, fonte_id)
    foto = _foto(dados)
    assert encartes.main(["importar", str(fonte_id), str(foto)]) == 1
    assert "inativa" in capsys.readouterr().out
    assert encartes.main(["importar", "999", str(foto)]) == 1
    assert "não encontrada" in capsys.readouterr().out
    assert encartes.main(["importar", GRUPO_ESTRANHO, str(foto)]) == 1
    assert "não encontrada" in capsys.readouterr().out
    assert _contagens(con)["encarte"] == 0
    assert _arquivos(dados / "encartes") == []


def test_importar_arquivo_que_nao_existe(dados, con, capsys):
    fonte_id = _lar(con)
    assert encartes.main(["importar", str(fonte_id), str(dados / "nao-existe.png")]) == 1
    assert "não deu para ler" in capsys.readouterr().out
    assert _contagens(con)["encarte"] == 0


def test_importar_mesma_arte_em_duas_fontes_divide_o_arquivo(dados, con):
    a = _lar(con)
    b = encartes.adicionar_fonte(con, GRUPO_ESTRANHO, "LAR São Cristóvão")
    foto = _foto(dados)
    for fonte_id in (a, b):
        argv = ["importar", str(fonte_id), str(foto), "--recebido-em", "2026-11-21"]
        assert encartes.main(argv) == 0
    caminhos = [r[0] for r in con.execute("SELECT arquivo FROM encarte ORDER BY id")]
    assert len(caminhos) == 2 and len(set(caminhos)) == 1
    assert len(_arquivos(dados / "encartes")) == 1


def test_importar_no_demo_recusa_sem_gravar(dados, con, pasta, monkeypatch, capsys):
    fonte_id = _lar(con)
    foto = _foto(dados)
    antes = _contagens(con)
    _demo(monkeypatch)
    assert encartes.main(["importar", str(fonte_id), str(foto)]) == 1
    assert "demonstração" in capsys.readouterr().out
    with pytest.raises(ModoDemo):
        encartes.importar_imagem(con, fonte_id, foto, pasta, AGORA)
    assert _contagens(con) == antes
    assert not pasta.exists()


def test_cli_no_demo_nem_abre_o_banco(dados, monkeypatch):
    foto = _foto(dados)
    _demo(monkeypatch)
    assert encartes.main(["importar", "1", str(foto)]) == 1
    assert encartes.main(["reler", "1"]) == 1
    assert not (dados / "mise_en_prato.db").exists()
    assert not (dados / "encartes").exists()


# ── reler ────────────────────────────────────────────────────────────────────
async def _lido(con, dados, pasta, imagem: bytes = LAR) -> int:
    """Encarte importado em 21/11 e já lido pelo leitor falso, com duas ofertas."""
    fonte_id = encartes.adicionar_fonte(con, GRUPO_LAR, "LAR Centro")
    recebido = datetime(2026, 11, 21, 12, 0, tzinfo=FUSO)
    _, encarte_id, _ = encartes.importar_imagem(
        con, fonte_id, _foto(dados, imagem), pasta, recebido
    )
    leitor = LeitorFalso(_leitura(_oferta("Arroz", 24.9, 21.9), _oferta("Café", 19.99, None)))
    assert await encartes.processar_proximo(con, leitor, AGORA, pasta) == encarte_id
    assert _encarte(con, encarte_id)["status"] == "lido"
    return encarte_id


async def test_reler_apaga_ofertas_e_volta_para_pendente(dados, con, pasta, capsys):
    encarte_id = await _lido(con, dados, pasta)
    assert len(_ofertas_do(con, encarte_id)) == 2

    assert encartes.main(["reler", str(encarte_id)]) == 0
    assert "pendente" in capsys.readouterr().out
    e = _encarte(con, encarte_id)
    assert e["status"] == "pendente"
    for campo in (
        "proxima_tentativa_em",
        "erro",
        "leitor",
        "validade_inicio",
        "validade_fim",
        "observacoes",
        "avisos",
    ):
        assert e[campo] is None, campo
    assert e["tentativas"] == 0
    assert e["arquivo"] is not None
    assert _ofertas_do(con, encarte_id) == []

    # O laço lê de novo e grava a leitura nova.
    leitor = LeitorFalso(_leitura(_oferta("Feijão", 8.9, 7.9)))
    assert await encartes.processar_proximo(con, leitor, AGORA, pasta) == encarte_id
    e = _encarte(con, encarte_id)
    assert (e["status"], e["tentativas"], e["leitor"]) == ("lido", 1, "leitor-falso")
    assert [o["produto"] for o in _ofertas_do(con, encarte_id)] == ["Feijão"]


async def test_reler_depois_de_falhar(dados, con, pasta):
    fonte_id = _lar(con)
    _, encarte_id, _ = encartes.importar_imagem(con, fonte_id, _foto(dados), pasta, AGORA)
    con.execute(
        "UPDATE encarte SET status = 'falhou', tentativas = 8, erro = 'x' WHERE id = ?",
        (encarte_id,),
    )
    con.commit()
    assert encartes.reler_encarte(con, encarte_id, pasta) is True
    e = _encarte(con, encarte_id)
    assert (e["status"], e["tentativas"], e["erro"]) == ("pendente", 0, None)


@pytest.mark.parametrize("como", ["podada", "sumida"])
async def test_reler_sem_imagem_recusa_sem_mudar_nada(dados, con, pasta, capsys, como):
    encarte_id = await _lido(con, dados, pasta)
    if como == "podada":
        con.execute("UPDATE encarte SET arquivo = NULL WHERE id = ?", (encarte_id,))
        con.commit()
    else:
        (dados / _encarte(con, encarte_id)["arquivo"]).unlink()
    antes, ofertas = _encarte(con, encarte_id), _ofertas_do(con, encarte_id)

    assert encartes.main(["reler", str(encarte_id)]) == 1
    assert "imagem já apagada; reimporte com `importar`" in capsys.readouterr().out
    assert _encarte(con, encarte_id) == antes
    assert _ofertas_do(con, encarte_id) == ofertas


def test_reler_inexistente(dados, con, capsys):
    assert encartes.main(["reler", "999"]) == 1
    assert "não encontrado" in capsys.readouterr().out


def test_reler_ja_pendente_so_zera_as_tentativas(dados, con, pasta, capsys):
    fonte_id = _lar(con)
    _, encarte_id, _ = encartes.importar_imagem(con, fonte_id, _foto(dados), pasta, AGORA)
    con.execute(
        """UPDATE encarte SET tentativas = 3, proxima_tentativa_em = '2026-11-21T14:00:00-03:00',
                              erro = 'HTTP 503' WHERE id = ?""",
        (encarte_id,),
    )
    con.commit()
    assert encartes.main(["reler", str(encarte_id)]) == 0
    assert "já estava pendente" in capsys.readouterr().out
    e = _encarte(con, encarte_id)
    assert (e["status"], e["tentativas"], e["proxima_tentativa_em"]) == ("pendente", 0, None)
    assert e["erro"] == "HTTP 503"


async def test_reler_mantem_oferta_identica_de_outro_encarte(dados, con, pasta):
    """A oferta repetida fica presa ao encarte que a gravou primeiro."""
    encarte_id = await _lido(con, dados, pasta)
    fonte_id = _encarte(con, encarte_id)["fonte_id"]
    recebido = datetime(2026, 11, 21, 12, 0, tzinfo=FUSO)
    _, outro, _ = encartes.importar_imagem(
        con, fonte_id, _foto(dados, MAX, "max.png"), pasta, recebido
    )
    leitor = LeitorFalso(_leitura(_oferta("Arroz", 24.9, 21.9), _oferta("Sal", 2.5, None)))
    assert await encartes.processar_proximo(con, leitor, AGORA, pasta) == outro
    assert [o["produto"] for o in _ofertas_do(con, outro)] == ["Sal"]

    assert encartes.reler_encarte(con, outro, pasta) is True
    assert _ofertas_do(con, outro) == []
    assert [o["produto"] for o in _ofertas_do(con, encarte_id)] == ["Arroz", "Café"]


async def test_reler_no_demo_recusa_sem_mudar_nada(dados, con, pasta, monkeypatch, capsys):
    encarte_id = await _lido(con, dados, pasta)
    antes, ofertas = _encarte(con, encarte_id), _ofertas_do(con, encarte_id)
    _demo(monkeypatch)
    assert encartes.main(["reler", str(encarte_id)]) == 1
    assert "demonstração" in capsys.readouterr().out
    with pytest.raises(ModoDemo):
        encartes.reler_encarte(con, encarte_id, pasta)
    assert _encarte(con, encarte_id) == antes
    assert _ofertas_do(con, encarte_id) == ofertas
