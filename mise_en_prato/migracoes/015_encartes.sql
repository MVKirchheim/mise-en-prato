-- Encartes de mercado: as imagens de oferta que chegam pelas comunidades do
-- WhatsApp, e as ofertas lidas delas. Ver docs/encartes-design.md.
--
-- O webhook recebe TODA mensagem do número, inclusive as pessoais. Por isso só
-- as fontes cadastradas aqui têm a imagem guardada; o resto é descartado sem
-- deixar rastro de conteúdo.

-- As comunidades aceitas. Desativar em vez de apagar: os encartes antigos
-- continuam apontando para a fonte. O cnpj fica pronto para a fase 2 ligar a
-- oferta ao histórico de preços da loja, sem outra migração.
CREATE TABLE encarte_fonte (
    id          INTEGER PRIMARY KEY,
    grupo_jid   TEXT NOT NULL UNIQUE,
    nome        TEXT NOT NULL,
    cnpj        TEXT REFERENCES estabelecimento(cnpj),
    ativo       INTEGER NOT NULL DEFAULT 1,
    criado_em   TEXT NOT NULL
);

-- Cada imagem recebida. O webhook só grava como 'pendente'; quem lê é o laço
-- de fundo, que guarda aqui as tentativas e o porquê da falha. A Evolution
-- reenvia a mesma mensagem, e a rede manda a mesma arte de novo com outro id:
-- daí as duas unicidades, pelo id e pelo hash. `arquivo` vira NULL quando a
-- imagem é podada; a linha fica, porque as ofertas apontam para ela.
CREATE TABLE encarte (
    id                      INTEGER PRIMARY KEY,
    fonte_id                INTEGER NOT NULL REFERENCES encarte_fonte(id),
    mensagem_id             TEXT NOT NULL,
    recebido_em             TEXT NOT NULL,
    legenda                 TEXT,
    sha256                  TEXT,
    arquivo                 TEXT,
    mime                    TEXT,
    status                  TEXT NOT NULL
                            CHECK (status IN ('pendente', 'lido', 'sem_ofertas', 'falhou')),
    tentativas              INTEGER NOT NULL DEFAULT 0,
    proxima_tentativa_em    TEXT,
    erro                    TEXT,
    leitor                  TEXT,
    validade_inicio         TEXT,
    validade_fim            TEXT,
    observacoes             TEXT,
    avisos                  TEXT,
    UNIQUE (fonte_id, mensagem_id)
);

-- Parcial porque o encarte que chegou sem mídia não tem hash, e vários desses
-- não podem colidir entre si.
CREATE UNIQUE INDEX idx_encarte_fonte_sha256 ON encarte(fonte_id, sha256)
    WHERE sha256 IS NOT NULL;
CREATE INDEX idx_encarte_fila ON encarte(status, proxima_tentativa_em);

-- As ofertas lidas de cada encarte, guardadas para sempre (alimentam a fase 2).
-- Preço em centavos, como no resto do banco. A chave_dedupe junta a mesma
-- oferta repetida em vários encartes da mesma loja.
CREATE TABLE oferta (
    id                      INTEGER PRIMARY KEY,
    encarte_id              INTEGER NOT NULL REFERENCES encarte(id),
    fonte_id                INTEGER NOT NULL REFERENCES encarte_fonte(id),
    produto                 TEXT NOT NULL,
    embalagem               TEXT,
    unidade                 TEXT CHECK (unidade IN ('kg', 'un')),
    preco_normal_centavos   INTEGER NOT NULL,
    preco_clube_centavos    INTEGER,
    condicao                TEXT,
    validade_inicio         TEXT,
    validade_fim            TEXT,
    avisos                  TEXT,
    chave_dedupe            TEXT NOT NULL,
    UNIQUE (fonte_id, chave_dedupe)
);

CREATE INDEX idx_oferta_validade ON oferta(validade_inicio, validade_fim);

-- Modo descoberta: grupos que mandaram imagem mas não são fonte, para achar o
-- jid de uma comunidade nova sem ler o log. Só o jid, quando e quantas vezes:
-- nada de nome, legenda, conteúdo ou remetente.
CREATE TABLE encarte_grupo_visto (
    grupo_jid       TEXT PRIMARY KEY,
    primeira_vez    TEXT NOT NULL,
    ultima_vez      TEXT NOT NULL,
    imagens         INTEGER NOT NULL DEFAULT 0
);
