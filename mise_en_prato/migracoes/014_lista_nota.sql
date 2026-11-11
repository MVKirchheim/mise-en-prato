-- Nota do item da lista: o que as receitas pedem daquele produto.
--
-- quantidade_milesimos continua sendo quantos COMPRAR (1 pacote, 2 latas); a
-- receita pede outra coisa ("500 g + 2 xícaras (Lasanha, Bolo)"), em unidades
-- que a loja não vende. Misturar as duas no mesmo número transformava 500 g de
-- farinha em 500 pacotes. Itens que já estavam na lista ficam sem nota.

ALTER TABLE lista_item ADD COLUMN nota TEXT;
