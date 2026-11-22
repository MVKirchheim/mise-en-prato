# Encartes de mercado: design (fase 1)

Data: 2026-11-22. Estado: design validado, implementação não iniciada.

## 1. Resumo de entendimento

- **O quê:** módulo que recebe automaticamente os encartes de 3 lojas de Medianeira (Max Atacadista, LAR São Cristóvão e LAR Centro), extrai as ofertas e mostra as do dia numa tela do PWA.
- **Por quê:** ver as promoções do dia num lugar só, sem abrir três comunidades do WhatsApp. É a base da fase 2 (avisos ligados à lista da semana e comparação com o histórico de preços).
- **Para quem:** o uso doméstico (`home`). No `demo`, a tela aparece com dados de exemplo.
- **Entrada:** comunidades do WhatsApp, via Evolution API num contêiner próprio no casa-srv, que manda um webhook para o FastAPI. Funciona só como leitor: nunca envia mensagens.
- **Leitura:** Gemini `flash-lite`, no plano gratuito, transforma a imagem em JSON. O leitor fica atrás de uma interface própria.
- **Dados:** ofertas separadas por loja, repetidas juntadas dentro da loja, guardadas para sempre. Imagens apagadas depois de 7 dias.

### Fora da fase 1
Instagram, mensagem de resumo no WhatsApp, avisos ligados à lista, comparação com o histórico, editar ou ocultar ofertas, ligação com `produto`/GTIN, recuperação de encartes perdidos com o app fora do ar.

## 2. Suposições

1. O risco de banimento da Evolution API (cliente não oficial, Baileys) usada só para leitura é baixo, e foi aceito para o número pessoal.
2. Os encartes chegam como imagem. PDF ou texto puro ficam para depois, se aparecerem.
3. O plano gratuito do Gemini continua existindo. Se acabar, o leitor é trocado por um pago, mexendo só em `leitor.py`.
4. Mensagens fora das 3 fontes cadastradas são descartadas sem gravar nada.
5. A oferta aparece alguns minutos depois de o encarte chegar. Não há prazo rígido.
6. Escala: 3 lojas, alguns encartes por dia, dezenas de ofertas. O SQLite atual aguenta com folga.

## 3. Teste de leitura (2026-11-22)

Feito com 2 encartes reais (LAR: 12 ofertas em grade; Max: 3 ofertas em layout livre, com a data do rodapé em contradição com a do topo), comparados com um gabarito escrito à mão.

| Leitor | Resultado |
| --- | --- |
| Gemini `flash-lite-latest` (gratuito) | 15/15 ofertas, 30/30 preços certos, 0 inventadas, 3–4 s. Preencheu a data do Max e apontou o 22/10 do rodapé como erro. |
| Gemini `flash-latest` e `3.8-flash` | Preços todos certos no que respondeu, mas 2–4 min e data do Max nula. |
| qwen2.5vl:3b local (GTX 1050 Ti, 4 GB) | Não coube na GPU e caiu para a CPU: 305 s, 3 de 24 preços errados, nenhuma embalagem lida. |
| qwen3-vl:2b local | Coube na GPU, mas devolveu resposta vazia. |
| Tesseract | Descartado pela análise dos layouts (centavos em sobrescrito, dois preços por selo, layout livre). |

Todo modelo do Gemini respondeu 503 ("high demand") na primeira tentativa, e nenhum deu 429. Por isso o laço de novas tentativas é obrigatório.

## 4. Log de decisões

| # | Decisão | Alternativas | Por quê |
| --- | --- | --- | --- |
| 1 | Fase 1: ler encartes e mostrar as ofertas do dia | Avisos ligados à lista, comparação com o histórico | Começar pelo núcleo. O resto vem depois. |
| 2 | Entrada pelo WhatsApp, via Evolution API no número pessoal | Compartilhamento manual, raspagem do Instagram | As 3 lojas mandam por comunidade, e o processo fica automático. |
| 3 | Instagram fora | Raspagem | Frágil, e as lojas que importam usam o WhatsApp. |
| 4 | Ofertas por loja, com as repetidas juntadas dentro da loja | Juntar por rede | As ofertas valem por loja, mas ofertas da rede chegam repetidas. |
| 5 | Modelo: preço normal + preço de clube + condição | Um preço só | O LAR ("Sem/Com Clube") e o Max ("Varejo/Clube Max") têm os dois. |
| 6 | Saída: só uma tela no PWA | Mensagem no WhatsApp | Simplicidade, e a integração fica só de leitura. |
| 7 | Leitor: Gemini `flash-lite`, plano gratuito, com novas tentativas | Gemini maior, modelo local, Tesseract | Ver seção 3. |
| 8 | Ofertas guardadas para sempre, imagens por 7 dias | Imagens para sempre, apagar tudo ao vencer | Os dados alimentam a fase 2. A imagem só serve para conferir a leitura. |
| 9 | Oferta suspeita aparece com "⚠ conferir" e o link da imagem | Fila de revisão, nova leitura automática | A oferta do dia não pode esperar revisão. |
| 10 | Nenhuma ação de correção na tela | Ocultar, editar, pedir nova leitura | YAGNI. Erro frequente é problema do leitor, não da tela. |
| 11 | Arquitetura A: webhook só guarda, laço em processo lê | Consulta periódica à Evolution (B), worker separado (C) | Segue o padrão de `tarefas.py`, com um único processo escrevendo no SQLite. A B exigiria guardar todas as conversas no banco da Evolution. |
| 12 | Fontes cadastradas por comando de linha, descobertas pelo log do webhook | Tela de cadastro, fontes no `.env` | Só 3 fontes, raramente mudam. |
| 13 | Tela "Ofertas" como 6º item do menu inferior | Dentro de Lista ou de Gastos | Consulta diária merece acesso direto. |
| 14 | Imagens dos 2 encartes de exemplo entram em `tests/` | Só o JSON das respostas | Aprovado. São anúncios públicos. |
| 15 | Entrega em 3 fatias, uma PR cada | Uma PR única | Mesmo ritmo do cardápio, e cada fatia é verificável sozinha. |

## 5. Design

### 5.1 Modelo de dados (`015_encartes.sql`)

**`encarte_fonte`**: as comunidades aceitas
- `id`, `grupo_jid` (único), `nome` ("LAR Centro"), `ativo`
- `cnpj`, opcional, referenciando `estabelecimento`. Não é usado na fase 1, mas deixa a fase 2 pronta sem outra migração.

**`encarte`**: cada imagem recebida
- `fonte_id`, `mensagem_id` (único), `recebido_em`, `legenda`
- `sha256`, único por fonte
- `arquivo`: caminho da imagem, que vira `NULL` depois de 7 dias
- `status` (`pendente` | `lido` | `sem_ofertas` | `falhou`), `tentativas`, `proxima_tentativa_em`, `erro`
- `leitor` (o nome do modelo), `validade_inicio`, `validade_fim`, `observacoes`, `avisos`

**`oferta`**
- `encarte_id`, `fonte_id`
- `produto`, `embalagem`, `unidade` (`kg` | `un`)
- `preco_normal_centavos`, `preco_clube_centavos` (pode ser nulo), `condicao`
- `validade_inicio`, `validade_fim`, `avisos`
- `chave_dedupe`, única por fonte: produto normalizado, os dois preços e a validade

### 5.2 Webhook: `POST /encartes/webhook`

A Evolution manda só o evento `messages.upsert`, com `webhookBase64` ativo. O fluxo para no primeiro "não":

1. `APP_MODE=demo`: responde 403.
2. Segredo `ENCARTE_WEBHOOK_SEGREDO`, enviado pela Evolution no cabeçalho `X-Webhook-Segredo` (campo `headers` do webhook) e comparado em tempo constante. Errado ou ausente, responde 401. Vazio na configuração, responde 403. A rota fica fora da autenticação de morador.
3. O `remoteJid` precisa terminar em `@g.us` e ser uma fonte ativa. Se não for, responde 200 sem gravar nada. **Modo descoberta:** imagem de grupo desconhecido grava só o `jid`, as datas e um contador em `encarte_grupo_visto`, sem nome, legenda ou remetente, e o log não registra nada que a identifique. O comando `python -m mise_en_prato.encartes grupos` lista esses grupos.
4. Só `imageMessage`. O resto recebe 200 e é descartado.
5. `mensagem_id` ou `sha256` repetido na fonte: responde 200 sem gravar.
6. No máximo 10 MB, e o Pillow precisa abrir o arquivo como JPEG, PNG ou WebP.
7. Salva em `/dados/encartes/<data>/<sha256>.<ext>`, cria o `encarte` como `pendente` (com a legenda) e sinaliza o `asyncio.Event` do laço.
8. Responde 200 sem chamar o leitor.

Os descartes respondem 200 para a Evolution não reenviar. Falha de banco ou de disco responde 5xx, e a Evolution reenvia sozinha (até 10 vezes, com espera crescente até 5 min). Por isso, um deploy rápido não perde encarte.

A mesma arte recebida por duas lojas da rede no mesmo dia gera duas linhas `encarte` apontando para **o mesmo arquivo** (o nome é o hash). A limpeza de 7 dias (fatia 2) só pode apagar um arquivo quando nenhuma linha ainda o usa.

### 5.3 Leitor: `encartes/leitor.py`

- `ler(imagem, mime, legenda) -> LeituraEncarte` (pydantic).
- `LeitorGemini` usa `httpx` (sem SDK novo), `ENCARTE_MODELO` (padrão `gemini-flash-lite-latest`), `GEMINI_API_KEY`, `temperature: 0`, `responseMimeType: application/json` e `responseSchema`.
- O prompt parte de `scratchpad/encartes/prompt.txt` do teste, com o campo `condicao` acrescentado e a legenda da mensagem como contexto.
- O pydantic valida a resposta. Os preços são convertidos para centavos inteiros.
- Nos testes, entra um leitor falso.

### 5.4 Laço: `laco_de_encartes` em `tarefas.py`

- Iniciado no `ciclo_de_vida`, como o backup. Acorda a cada 60 s ou com o `asyncio.Event`. Processa um pendente por vez, por ordem de chegada.
- **Temporários** (503, 429, timeout, rede): esperas de 1, 5, 15 e 60 min, depois de hora em hora. Depois de 8 tentativas, o status vira `falhou`.
- **Permanentes** (chave inválida, imagem recusada, JSON inválido depois de 2 tentativas): o status vira `falhou` na hora.
- Uma vez por dia, apaga as imagens com mais de 7 dias e grava `arquivo = NULL`.

**Avisos depois da leitura**
- **Na oferta:** preço de clube maior que o normal, preço abaixo de R$ 0,10 ou acima de R$ 2.000, produto sem nome.
- **No encarte:**
  - sem validade: assume o dia do recebimento e avisa "validade não encontrada";
  - validade vencida quando o encarte chega: avisa "vencido ao chegar";
  - `observacoes` do modelo: mostradas como vieram.
- **Zero ofertas:** o status vira `sem_ofertas`, sem aviso, e só entra num contador.

### 5.5 Tela "Ofertas"

**API**
- `GET /encartes/ofertas`: ofertas vigentes hoje (`validade_inicio ≤ hoje ≤ validade_fim`, fuso America/Sao_Paulo), agrupadas por loja, com o estado da entrada (último encarte, pendentes, falhas, sem ofertas).
- `GET /encartes/{id}/imagem`: devolve 404 depois de 7 dias.
- As duas rotas usam autenticação de morador.

**Layout**
1. Cabeçalho com a data.
2. Linha de saúde, em destaque se passar de 48 h sem encarte ou se houver falhas.
3. Filtro por loja e busca, ambos no navegador.
4. Lista por loja, com os encartes do dia (observações e "ver encarte") e as ofertas: preço de clube em destaque, preço normal ao lado (sem riscar), `/kg` ou `/un`, condição, validade quando não for só hoje, e ⚠ com o motivo.
5. Contador de imagens sem ofertas.

**Regras gerais**
- Todo texto passa pelo escape da PR #17, e o novo JS entra no `test_frontend_escape.py`.
- 6º item do menu inferior, com ícone de etiqueta. É preciso conferir se cabe na largura de um celular.
- No `demo`, a tela usa as ofertas de exemplo do `demo.db`, e a data "de hoje" é a das ofertas de exemplo.

### 5.6 Evolution API (stack separada no Dockge, fora do repositório)

- Evolution v2 com o banco que ela exigir (provavelmente Postgres).
- Gravação de mensagens, contatos e conversas desligada (`DATABASE_SAVE_*`).
- Webhook pela rede Docker, com `messages.upsert` e `webhookBase64`.
- Pareamento por QR Code uma vez, com a sessão guardada num volume.

### 5.7 Testes (pytest no Docker, sem API real)

- **Webhook:** segredo errado, `demo` 403, grupo desconhecido sem gravar, texto e figurinha, repetição por id e por hash, arquivo que não é imagem, caminho feliz, log do modo descoberta.
- **Leitor:** as respostas reais do Gemini do teste viram fixtures, com JSON quebrado e campos faltando.
- **Laço:** 503, 429, chave inválida, zero ofertas, sequência de esperas, limite de tentativas.
- **Avisos:** um caso por regra.
- **Vigentes hoje:** virada de dia no fuso e ofertas de vários dias.
- **Limpeza:** a imagem some depois de 7 dias e as ofertas ficam.
- **Escape do frontend.**
- **Fixtures:** `lar_2026-11-21_super_promocao.png` e `max_2026-11-22_esquenta_black_friday.png` em `tests/`.

## 6. Entrega

1. **Fatia 1:** migração `015`, webhook, modo descoberta, comando `fonte-encarte adicionar/listar`. Ao fim, os encartes já chegam e ficam guardados.
2. **Fatia 2:** leitor Gemini, laço com novas tentativas, avisos, limpeza de imagens. As ofertas passam a existir no banco.
3. **Fatia 3:** tela "Ofertas" e o 6º item do menu.

## 7. A verificar na implementação

- ~~Se a Evolution v2 manda cabeçalhos personalizados no webhook~~: manda (campo `headers`). O base64 chega em `data.message.base64` com `base64: true`. Lido no código-fonte da v2.3.x.
- Se o canal de avisos de uma comunidade chega com o próprio `@g.us`. Vai ser confirmado com a tabela de descoberta.
- A Evolution v2 exige banco próprio (Prisma, Postgres ou MySQL). As opções `DATABASE_SAVE_DATA_NEW_MESSAGE`, `_CONTACTS`, `_CHATS`, `_HISTORIC`, `_LABELS` e `DATABASE_SAVE_MESSAGE_UPDATE` ficam em `false`. O Redis é opcional.
- Os limites atuais do plano gratuito para `gemini-flash-lite-latest` no AI Studio.
- Se 6 itens cabem no menu inferior em telas de 360 px.

## 8. Riscos

- **Banimento do número:** mitigado com uso só de leitura e um interruptor (parar o contêiner da Evolution).
- **Fim ou piora do plano gratuito:** mitigado pela interface do leitor.
- **Indisponibilidade (503) do Gemini:** mitigada pela fila e pelas novas tentativas.
- **Encarte perdido durante deploy ou queda do app:** aceito na fase 1 e visível pela linha de saúde.
- **Mudança de layout dos encartes:** o modelo de visão é genérico, e os avisos e a imagem por 7 dias permitem perceber.
