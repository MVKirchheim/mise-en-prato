-- Cardápio semanal: o que a casa planeja comer em cada almoço e jantar.
--
-- Cada linha é uma entrada numa "casa" (data + refeição); várias entradas na
-- mesma casa são várias linhas. Marcada como feita (feito_em preenchido), a
-- entrada vira diário do que foi cozinhado. O título fica gravado: apagar ou
-- renomear a receita não reescreve o que já foi planejado ou comido.

CREATE TABLE cardapio_entrada (
    id          INTEGER PRIMARY KEY,
    data        TEXT NOT NULL,
    refeicao    TEXT NOT NULL CHECK (refeicao IN ('almoco', 'jantar')),
    receita_id  INTEGER REFERENCES receitas(id) ON DELETE SET NULL,
    titulo      TEXT NOT NULL,
    porcoes     INTEGER CHECK (porcoes IS NULL OR porcoes > 0),
    ordem       INTEGER NOT NULL DEFAULT 0,
    feito_em    TEXT,
    morador     TEXT,
    criada_em   TEXT NOT NULL
);

CREATE INDEX idx_cardapio_entrada_casa ON cardapio_entrada(data, refeicao);
CREATE INDEX idx_cardapio_entrada_receita ON cardapio_entrada(receita_id);
