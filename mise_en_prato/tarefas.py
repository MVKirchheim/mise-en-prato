#!/usr/bin/env python3
"""As tarefas de fundo do servidor.

Rodam dentro do MESMO processo que atende a PWA, e não em contêiner separado.
Não é economia de RAM: é que só um processo pode escrever neste sqlite sem
convidar o "database is locked". O padrão da casa (laço em contêiner, ver o
vigia) vale para quem não divide banco com ninguém.

Hoje há duas tarefas: o backup diário e a leitura dos encartes. O vigia de
preços entra aqui quando o detector de dado envenenado estiver de pé — ver o
README.
"""

import asyncio
import logging
import sqlite3
from datetime import date, datetime
from pathlib import Path

from . import encartes
from .leitor_encartes import Leitor

log = logging.getLogger("mise_en_prato.tarefas")

INTERVALO_BACKUP = 6 * 60 * 60  # confere de 6 em 6 horas; grava uma vez por dia
GUARDAR = 7  # mesma conta do GUARDAR_BACKUPS do vigia

ESPERA_ENCARTES = 60  # segundos sem evento até olhar a fila de novo
# Entre duas leituras seguidas: o plano gratuito do Gemini limita por minuto, e
# uma rajada de encartes (a rede manda 5 de uma vez) estouraria o limite.
PAUSA_ENTRE_LEITURAS = 4.0


def fazer_backup(con: sqlite3.Connection, pasta: Path, guardar: int = GUARDAR) -> Path | None:
    """Cópia consistente do banco do dia, e poda as antigas.

    `VACUUM INTO` e não `cp`: com WAL ligado, copiar o arquivo por fora pega o
    banco sem o que ainda está no log, e o backup nasce truncado. O VACUUM
    escreve um banco íntegro e já compactado, sem parar quem está lendo.
    """
    pasta.mkdir(parents=True, exist_ok=True)
    alvo = pasta / f"cupomzinho_{date.today():%Y-%m-%d}.db"

    novo = None
    if not alvo.exists():
        con.execute("VACUUM INTO ?", (str(alvo),))
        log.info("backup gravado: %s (%d KB)", alvo.name, alvo.stat().st_size // 1024)
        novo = alvo

    # A poda roda SEMPRE, mesmo quando o backup de hoje já existia. Podar só
    # junto com a gravação amarraria o teto de cópias à sorte de o dia ter
    # virado — e o nome do arquivo ordena por data porque é ISO.
    for velho in sorted(pasta.glob("cupomzinho_*.db"))[:-guardar]:
        velho.unlink()
        log.info("backup antigo removido: %s", velho.name)
    return novo


async def laco_de_backup(con: sqlite3.Connection, pasta: Path) -> None:
    """Laço infinito de backup. Erro aqui NUNCA pode derrubar o servidor."""
    while True:
        try:
            fazer_backup(con, pasta)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Servidor que morre porque o backup falhou é pior que servidor sem
            # backup: o cupom do dia deixaria de entrar.
            log.exception("backup falhou em %s", datetime.now())
        await asyncio.sleep(INTERVALO_BACKUP)


async def laco_de_encartes(
    con: sqlite3.Connection,
    leitor: Leitor,
    pasta: Path,
    evento: asyncio.Event,
    *,
    pausa: float = PAUSA_ENTRE_LEITURAS,
    espera: float = ESPERA_ENCARTES,
) -> None:
    """Lê os encartes pendentes, um por vez, e poda as imagens velhas uma vez por dia.

    Acorda com o evento que o webhook sinaliza a cada imagem guardada, ou a
    cada `espera` segundos para pegar as novas tentativas que venceram. Erro
    aqui NUNCA pode derrubar o servidor, como no backup.
    """
    ultima_limpeza: date | None = None
    while True:
        # Limpa ANTES de processar: imagem que chega durante a leitura sinaliza
        # de novo, e o wait lá embaixo volta na hora em vez de perdê-la.
        evento.clear()
        try:
            hoje = datetime.now(encartes.FUSO).date()
            if ultima_limpeza != hoje:
                # Marca antes de rodar: limpeza que falha tenta de novo amanhã,
                # e não a cada minuto enchendo o log.
                ultima_limpeza = hoje
                encartes.limpar_imagens(con, pasta, datetime.now(encartes.FUSO))
            while (
                await encartes.processar_proximo(con, leitor, datetime.now(encartes.FUSO), pasta)
                is not None
            ):
                await asyncio.sleep(pausa)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("laço de encartes falhou em %s", datetime.now())
        try:
            await asyncio.wait_for(evento.wait(), espera)
        except TimeoutError:
            pass
