# Cardápio semanal e melhorias em Gastos — Design

Data: 2026-11-11
Estado: design validado, implementação não iniciada.

## 1. Resumo do entendimento

- **Cardápio (novo):** grade semanal de segunda a domingo, com almoço e jantar. Cada casa aceita uma ou mais receitas do livro ou um texto livre ("sobras", "pedimos pizza").
- **Plano vira histórico:** a semana é planejada e depois cada entrada é marcada como feita. O mesmo registro serve de diário do que foi cozinhado.
- **Lista da semana:** um botão junta os ingredientes das receitas planejadas, soma os repetidos e permite revisar antes de enviar para a lista de compras.
- **Gastos:** navegar entre meses, abrir o detalhe do mês, ver gastos por categoria e ter uma visão de tendência.
- **Categorias:** lista curta e fixa, sugerida automaticamente e corrigível na tela do produto. A correção manual nunca é sobrescrita.
- **Entrega:** quatro fatias intercaladas, cada uma um PR utilizável.

### Fora de escopo

Despensa e estoque, custo por receita, conversão de unidades culinárias em embalagens, refeições configuráveis, metas de orçamento, exportação, intervalo de datas livre, visão anual e a criação do `demo.db`.

## 2. Suposições

- **Uso:** um cardápio único para a casa, compartilhado entre os moradores. O app registra quem criou cada entrada.
- **Escala:** poucos moradores e alguns milhares de itens de cupom. SQLite com consultas diretas basta, sem cache.
- **Desempenho:** telas abrem em menos de um segundo na rede da casa.
- **Segurança:** acesso só pela tailnet, identidade via Tailscale, sem senhas.
- **Modo demo:** leitura liberada e escrita bloqueada com 403, no mesmo padrão das receitas.
- **Stack:** sem novas dependências. Gráficos em SVG escrito à mão, migrações SQL numeradas, testes em pytest, tudo pelo Docker.
- **Porções:** uma receita no cardápio pode ter porções próprias, e a lista da semana escala os ingredientes por elas.
- **Histórico:** as categorias são aplicadas também aos produtos já cadastrados.

## 3. Fatos do código que orientaram o design

Levantados por leitura do código em `main` (27beba1). Nada foi executado.

- O livro de receitas está implementado (`receitas.py`, `web/receitas.js`). Não existe nada de cardápio no código.
- `escalar_receita` e `calcular_fator_rendimento` existem em `receitas.py`, mas nenhuma rota ou tela os usa.
- A tela de Gastos nunca envia `mes` (`web/app.js:837`), embora `GET /api/gastos?mes=` aceite.
- `produto.categoria` não é gravada por nenhum caminho além da herança na unificação. O relatório por categoria agrupa por uma coluna vazia.
- NCM só é lido no layout SPED do cupom (`nfce_pr.py:478-532`) e gravado em `produto.ncm`. O layout DANFE não traz NCM.
- A categoria do Open Food Facts fica em `catalogo_produto.categoria` como texto livre e não é copiada para `produto`.
- Não existe rota nem tela de edição de produto.
- `lista_item` não tem unidade. `listas.por_produto` sobrescreve a quantidade em vez de somar (`listas.py:55-59`).
- `POST /api/receitas/{id}/enviar-para-lista` descarta a unidade do ingrediente (`main.py:1724`): "500 g" vira 500 unidades. A escala da tela não chega ao servidor.
- Chaves estrangeiras estão ativas (`banco.py:56`).
- As abas são trocadas por uma lista fixa em `web/app.js:107`. O `sw.js` tem cache `mise-en-prato-v1` e uma lista de arquivos.

## 4. Registro de decisões

| # | Decisão | Alternativas consideradas | Motivo |
| --- | --- | --- | --- |
| 1 | Cardápio é plano e diário juntos | Só diário; só planejamento | Escolha do dono |
| 2 | Almoço e jantar fixos | Lista livre por dia; quatro refeições; configurável | Grade simples no celular |
| 3 | Casa aceita receitas ou texto livre | Só receitas; uma receita por casa | Histórico completo sem cadastro prévio |
| 4 | Lista de compras da semana inteira | Não gerar; descontar despensa | Despensa fora de escopo |
| 5 | Categoria automática com correção manual | Só automático; só manual | Dados automáticos são fracos; manual puro dá trabalho |
| 6 | Entrega intercalada em quatro fatias | Cardápio primeiro; Gastos primeiro | Cada fatia utilizável cedo |
| 7 | Uma tabela de entradas por data (abordagem A) | Semana como entidade; plano e diário separados | Menor modelo que cobre o combinado; as outras podem ser acrescentadas depois |
| 8 | Entrada guarda o nome da receita | Depender só do vínculo | Excluir ou renomear a receita não apaga o histórico |
| 9 | Aba própria "Cardápio" na barra inferior | Embutir em Receitas | Uso diário e independente |
| 10 | Semana como lista vertical de dias | Grade de sete colunas | Legibilidade no celular |
| 11 | Sem arrastar e soltar; mover pelo painel de edição | Arrastar e soltar | Custo alto para pouco ganho |
| 12 | Item da lista mantém "quantos comprar" e ganha uma nota com o que a receita pede | Unidade no item; converter em embalagens | Preserva a comparação de preços e é honesto sobre o que o app sabe |
| 13 | Categoria manual protegida por `categoria_origem` | Sem distinção de origem | Permite recalcular o automático sem perder correções |
| 14 | Total de Gastos continua somando o valor pago | Somar valor bruto | Mantém o comportamento atual; desconto é informação à parte |

## 5. Design

### 5.1 Fatia 1 — Cardápio mínimo

**Banco: `013_cardapio.sql`**

Tabela `cardapio_entrada`:

| Coluna | Tipo | Regra |
| --- | --- | --- |
| `id` | INTEGER | chave primária |
| `data` | TEXT `AAAA-MM-DD` | obrigatória |
| `refeicao` | TEXT | CHECK em (`almoco`, `jantar`) |
| `receita_id` | INTEGER, opcional | REFERENCES `receitas(id)` ON DELETE SET NULL |
| `titulo` | TEXT | obrigatório; nome da receita no momento, ou o texto livre |
| `porcoes` | INTEGER, opcional | maior que zero; padrão é o rendimento da receita |
| `ordem` | INTEGER | posição dentro da casa |
| `feito_em` | TEXT, opcional | nulo é planejado; preenchido é feito |
| `morador` | TEXT | quem criou |
| `criada_em` | TEXT | automático |

Índice em (`data`, `refeicao`).

Regras:

- Várias entradas por casa são várias linhas com a mesma data e refeição.
- Qualquer data pode ser marcada como feita, inclusive sem plano prévio.
- Renomear uma receita não altera entradas antigas.
- Sem limite de entradas por casa.

**Backend: `mise_en_prato/cardapio.py`**

Funções puras sobre a conexão, no estilo de `receitas.py`. Rotas em `main.py`, todas com `Morador`:

| Rota | Comportamento |
| --- | --- |
| `GET /api/cardapio?semana=AAAA-MM-DD` | Semana (segunda a domingo) que contém a data; sem parâmetro, a atual |
| `POST /api/cardapio` | Cria entrada: `data`, `refeicao`, e `receita_id` ou `titulo`; `porcoes` opcional |
| `PUT /api/cardapio/{id}` | Altera texto, porções, dia, refeição, ordem e feito |
| `DELETE /api/cardapio/{id}` | Remove |

- A resposta do `GET` traz sete dias, cada um com `almoco` e `jantar` como listas ordenadas. Cada entrada traz título, porções, feito e, havendo receita, o id e se tem foto.
- Sem `receita_id` e sem `titulo`: 422. `receita_id` inexistente: 404. Data ou refeição inválida: 422.
- Com `receita_id`, o servidor preenche `titulo` e `porcoes` quando não vierem.
- Modo demo: `POST`, `PUT` e `DELETE` respondem 403.
- O navegador envia a data local; o servidor só calcula a segunda-feira.

**Tela: `web/cardapio.js` e `#aba-cardapio`**

- Aba depois de Receitas, com o ícone `assets/icons/navigation/cardapio.svg` embutido no HTML.
- Incluir `cardapio` na lista de `web/app.js:107`, registrar o carregador, acrescentar o arquivo ao `sw.js` e trocar o cache para `v2`.
- Sete cartões verticais, cada um com Almoço e Jantar. Hoje destacado e em foco ao abrir.
- Topo com semana anterior, seguinte e "Hoje".
- "+" abre painel inferior com busca de receitas e campo de texto livre; com receita, ajuste de porções.
- Caixa de seleção marca feito. Tocar numa entrada com receita abre o Modo Cozinha.
- Editar, mover e remover pelo mesmo painel.
- Após cada alteração, recarrega a semana. Em falha, mostra a mensagem do servidor e não muda a tela.

**Testes**

- `tests/test_cardapio.py`: cálculo da semana (virada de mês e de ano), ordenação, marcar e desmarcar feito, exclusão de receita preservando o título.
- `tests/test_api_cardapio.py`: ciclo completo, validações e bloqueio em demo.

### 5.2 Fatia 2 — Gastos: meses e detalhe do mês

Sem mudança de banco.

**Navegação**

- A tela guarda o mês em foco e o envia em `GET /api/gastos?mes=AAAA-MM`.
- Colunas do gráfico clicáveis; setas de mês anterior e seguinte, com "seguinte" desativada no mês atual.
- Janela de 12 meses que sempre contém o mês em foco: `meses_com_gasto` recebe o fim da janela.
- Meses sem cupom aparecem como coluna zerada.

**Detalhe**

| Bloco | Conteúdo | Origem |
| --- | --- | --- |
| Cupons do mês | Data, loja, morador, valor pago, desconto; tocar expande os itens | `GET /api/gastos/cupons?mes=` |
| Todos os produtos | "Ver todos" além dos 15 | Parâmetro `limite` |
| Pagamento e descontos | Total de descontos e barras por forma de pagamento | Campos novos no resumo |

- Cupons incompletos entram no total e recebem um aviso na lista.
- Valores em centavos inteiros no banco e texto na API.

**A verificar antes de implementar:** se `desconto_centavos` e `forma_pagamento` são preenchidos pelo leitor de cupons. Se vierem vazios na maioria, o bloco de pagamento e descontos sai da fatia.

**Testes**

- Janela com mês em foco antigo e meses vazios como zero.
- Soma dos cupons listados igual ao total do mês.
- "Ver todos" com mais de 15 produtos e os mesmos 15 primeiros.

### 5.3 Fatia 3 — Lista de compras da semana

**Banco: `014_lista_nota.sql`**

Coluna `nota TEXT` em `lista_item`.

**Regra da lista (vale também para o envio por receita)**

- `quantidade_milesimos` continua significando quantos comprar. Padrão 1. Não é sobrescrita se o produto já estiver na lista.
- `nota` guarda o que as receitas pedem, por exemplo "500 g + 2 xícaras (lasanha, bolo)". Quantidades na mesma unidade são somadas com `Decimal`.

**Fluxo**

- Botão "Lista da semana" no cardápio abre uma prévia calculada no servidor (`GET /api/cardapio/lista?semana=`).
- Entram receitas planejadas e ainda não feitas. Texto livre e entradas feitas ficam de fora.
- Cada receita é escalada pelas porções da entrada com `escalar_receita`.
- Ingredientes são agrupados por nome normalizado.
- O usuário desmarca o que já tem e envia para uma lista nova ou existente (`POST /api/cardapio/lista`).
- `POST /api/receitas/{id}/enviar-para-lista` passa a usar a mesma regra e a receber a escala escolhida.
- A tela da lista mostra a nota.

**Testes**

- Soma por unidade igual, e unidades diferentes concatenadas na nota.
- Escala por porções sem ponto flutuante.
- Quantidade existente na lista preservada.
- Entradas feitas e de texto livre excluídas.

### 5.4 Fatia 4 — Categorias e tendência

**Lista fixa**

Hortifrúti, Carnes e peixes, Laticínios e ovos, Padaria, Mercearia, Bebidas, Congelados, Doces e lanches, Limpeza, Higiene e beleza, Pet, Farmácia, Outros.

**Banco: `015_categoria_origem.sql`**

Coluna `categoria_origem TEXT` em `produto` (`manual` ou a fonte automática).

**Sugestão: `mise_en_prato/categorias.py`**

Função pura, nesta ordem de prioridade:

1. NCM, pelos dois primeiros dígitos.
2. Categoria do catálogo (Open Food Facts ou CMED), por palavras-chave.
3. Descrição do produto, por palavras-chave.
4. Sem categoria.

**Correção manual**

- `manual` nunca é recalculado. O automático é recalculado quando as regras mudam.
- Rota `POST /api/produto/{id}/categoria` e seletor no cabeçalho da tela do produto.
- Em Gastos, tocar em "sem categoria" lista esses produtos por valor gasto.
- Na unificação, a categoria manual prevalece.

**Produtos existentes**

Rotina de classificação na subida da versão, repetível sem efeito colateral.

**Tela de Gastos**

- Barras por categoria no mês em foco; tocar mostra os produtos da categoria no mês.
- Tendência (proposta inicial, a revisar no início da fatia): média dos últimos 3 e 12 meses ao lado do total; variação de cada categoria e loja contra a própria média de 3 meses; mesmo mês do ano anterior quando houver dados.

**Testes**

- Tabela de casos da função de sugestão, incluindo prioridade.
- Categoria manual sobrevivendo a recálculo e a unificação.
- Soma das categorias igual ao total de itens do mês.

## 6. Entrega e verificação

- Quatro PRs, um por fatia, cada um de um branch próprio a partir de `main`.
- Antes de cada commit: `docker compose run --rm dev ruff check .` e `docker compose run --rm dev pytest -q`.
- Telas alteradas passam por verificação visual em largura de celular (360 px).
- Fatias 3 e 4 recebem revisão adicional por outro modelo antes de serem dadas por prontas.

## 7. Riscos

| Risco | Fatia | Tratamento |
| --- | --- | --- |
| Barra inferior apertada com cinco abas | 1 | Verificação em 360 px; plano B é encurtar rótulos |
| App instalado com arquivos antigos | 1 | Troca do nome do cache no `sw.js` |
| Desconto e forma de pagamento vazios | 2 | Checar os dados antes; o bloco sai da fatia se vazios |
| Mudança no envio por receita afetar listas em uso | 3 | Quantidade existente não é sobrescrita |
| Ingredientes com nomes diferentes não se agruparem | 3 | Nome normalizado; ajuste manual na prévia |
| Classificação automática fraca | 4 | Fila de "sem categoria" por valor e correção manual protegida |
| Migrações no banco real da casa | 1, 3, 4 | Só acrescentam tabela ou coluna |

## 8. Questões em aberto

- Quais comparações de tendência importam de fato (decidir no início da fatia 4).
- Preenchimento real de `desconto_centavos` e `forma_pagamento` (verificar antes da fatia 2).
