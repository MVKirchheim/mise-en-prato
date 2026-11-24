/* Ofertas do dia: o que os encartes das lojas cadastradas dizem que vale hoje.
   Só leitura (nenhuma correção pela tela). Filtro por loja e busca rodam aqui,
   sobre o que já chegou, sem nova chamada.

   "Hoje" é o do servidor (fuso de São Paulo), não o do relógio do aparelho:
   quem decide o que está vigente é a API. */
'use strict';

(function () {
  const $ = (s) => document.querySelector(s);
  // escapar() e dinheiro() vêm de app.js (globais).

  const DIAS = ['domingo', 'segunda', 'terça', 'quarta', 'quinta', 'sexta', 'sábado'];
  const MESES = ['janeiro', 'fevereiro', 'março', 'abril', 'maio', 'junho',
                 'julho', 'agosto', 'setembro', 'outubro', 'novembro', 'dezembro'];

  let dados = null;
  let lojaFiltro = null; // id da loja, ou null para todas
  let pedido = 0;

  /* Minúsculas e sem acento: "açúcar" acha "Acucar". */
  const normalizar = (t) => String(t ?? '').normalize('NFD').replace(/[̀-ͯ]/g, '').toLowerCase().trim();
  const plural = (n, um, varios) => `${n} ${n === 1 ? um : varios}`;
  const diaMes = (iso) => `${iso.slice(8, 10)}/${iso.slice(5, 7)}`;
  const reais = (centavos) => dinheiro(Number(centavos) / 100);

  /* "segunda, 23 de novembro", a partir do AAAA-MM-DD (meia-noite local). */
  function porExtenso(iso) {
    const [a, m, d] = iso.split('-').map(Number);
    const data = new Date(a, m - 1, d);
    return `${DIAS[data.getDay()]}, ${d} de ${MESES[m - 1]}`;
  }

  function haQuanto(iso) {
    const horas = Math.floor((Date.now() - Date.parse(iso)) / 3600000);
    if (!Number.isFinite(horas) || horas < 1) return 'há menos de 1 h';
    if (horas < 48) return `há ${horas} h`;
    return `há ${Math.floor(horas / 24)} dias`;
  }

  // ── pedaços ───────────────────────────────────────────────────────────────
  function renderSaude(saude) {
    const caixa = $('#ofertas-saude');
    const ultimo = saude.ultimo_recebido_em
      ? `Último encarte ${haQuanto(saude.ultimo_recebido_em)}`
      : 'Nenhum encarte recebido ainda';
    const partes = [
      ultimo,
      plural(saude.pendentes, 'pendente', 'pendentes'),
      plural(saude.falhas_recentes, 'falha', 'falhas'),
    ];
    caixa.className = 'estado ofertas-saude' + (saude.alerta ? ' ruim' : '');
    caixa.textContent = partes.join(' · ');
  }

  function renderChips() {
    const caixa = $('#ofertas-chips');
    if (!dados.lojas.length) {
      caixa.innerHTML = '';
      return;
    }
    const chip = (id, nome) => {
      const ativo = (id === null ? lojaFiltro === null : lojaFiltro === id);
      return `<button type="button" class="chip-sabor" data-loja="${id === null ? '' : Number(id)}"
                aria-pressed="${ativo}">${escapar(nome)}</button>`;
    };
    caixa.innerHTML = chip(null, 'Todas') + dados.lojas.map((l) => chip(l.id, l.nome)).join('');
  }

  const avisos = (lista) => (lista && lista.length
    ? `<span class="oferta-aviso">⚠ ${lista.map(escapar).join('; ')}</span>` : '');

  function renderEncarte(e) {
    const link = e.tem_imagem
      ? `<a class="ofertas-ver" href="/api/encartes/${Number(e.id)}/imagem" target="_blank" rel="noopener">ver encarte</a>`
      : '';
    const obs = e.observacoes ? `<span>${escapar(e.observacoes)}</span>` : '';
    return `
      <div class="ofertas-encarte fraca">
        <span class="ofertas-encarte-texto">
          <span>Encarte de ${escapar(diaMes(String(e.recebido_em || '')))}${obs ? ':' : ''}</span>
          ${obs}
          ${avisos(e.avisos)}
        </span>
        ${link}
      </div>`;
  }

  function renderOferta(o) {
    const sufixo = o.unidade === 'kg' ? '/kg' : o.unidade === 'un' ? '/un' : '';
    const detalhes = [o.embalagem, o.condicao].filter(Boolean).map(escapar).join(' · ');
    const validade = o.validade_fim && o.validade_fim !== dados.hoje
      ? `<span class="fraca">até ${escapar(diaMes(o.validade_fim))}</span>` : '';
    const conferir = o.avisos && o.avisos.length
      ? `<span class="oferta-aviso">⚠ conferir: ${o.avisos.map(escapar).join('; ')}</span>` : '';
    // Os dois preços valem, para públicos diferentes: nenhum é riscado.
    const precos = o.preco_clube_centavos != null
      ? `<span><span class="oferta-preco num">${reais(o.preco_clube_centavos)}${sufixo}</span>
         <span class="oferta-rotulo">clube</span></span>
         <span class="oferta-normal num">${reais(o.preco_normal_centavos)}${sufixo} <span class="oferta-rotulo">normal</span></span>`
      : `<span class="oferta-preco num">${reais(o.preco_normal_centavos)}${sufixo}</span>`;
    return `
      <li class="oferta">
        <div class="oferta-texto">
          <span class="oferta-produto">${escapar(o.produto)}</span>
          ${detalhes ? `<span class="fraca">${detalhes}</span>` : ''}
          ${validade}
          ${conferir}
        </div>
        <div class="oferta-precos">${precos}</div>
      </li>`;
  }

  function renderLojas() {
    const caixa = $('#ofertas-lojas');
    if (!dados.lojas.length) {
      caixa.innerHTML = '<div class="estado">Nenhuma loja de encartes cadastrada.</div>';
      return;
    }
    const busca = normalizar($('#ofertas-busca').value);
    const lojas = dados.lojas.filter((l) => lojaFiltro === null || l.id === lojaFiltro);

    const blocos = lojas.map((loja) => {
      const ofertas = busca
        ? loja.ofertas.filter((o) => normalizar(o.produto).includes(busca))
        : loja.ofertas;
      // Na busca, loja sem nada que case some; sem busca, aparece com o aviso.
      if (busca && !ofertas.length) return '';
      const comOferta = new Set(ofertas.map((o) => o.encarte_id));
      const encartes = loja.encartes.filter((e) => comOferta.has(e.id));
      const corpo = ofertas.length
        ? `${encartes.map(renderEncarte).join('')}
           <ul class="ofertas-lista">${ofertas.map(renderOferta).join('')}</ul>`
        : '<div class="fraca ofertas-vazia">sem ofertas hoje</div>';
      return `
        <div class="cartao ofertas-loja">
          <h3 class="ofertas-loja-nome">${escapar(loja.nome)}</h3>
          ${corpo}
        </div>`;
    }).join('');

    caixa.innerHTML = blocos
      || `<div class="estado">Nenhuma oferta de hoje com “${escapar($('#ofertas-busca').value.trim())}”.</div>`;
  }

  function renderRodape(saude) {
    const rodape = $('#ofertas-rodape');
    const n = Number(saude.sem_ofertas_recentes) || 0;
    rodape.hidden = n === 0;
    rodape.textContent = n === 1
      ? '1 imagem sem ofertas nos últimos 7 dias'
      : `${n} imagens sem ofertas nos últimos 7 dias`;
  }

  // ── carga ─────────────────────────────────────────────────────────────────
  async function carregar() {
    const meu = ++pedido;
    $('#ofertas-saude').className = '';
    $('#ofertas-saude').innerHTML = '';
    $('#ofertas-chips').innerHTML = '';
    $('#ofertas-rodape').hidden = true;
    $('#ofertas-lojas').innerHTML = '<div class="cartao"><div class="fraca">carregando…</div></div>';
    let resposta;
    try {
      resposta = await api('/api/encartes/ofertas');
    } catch (erro) {
      if (meu !== pedido) return;
      $('#ofertas-lojas').innerHTML = `<div class="estado ruim">${escapar(erro.message)}</div>`;
      return;
    }
    if (meu !== pedido) return;
    dados = resposta;
    // Loja que sumiu do cadastro não fica filtrando uma tela vazia.
    if (lojaFiltro !== null && !dados.lojas.some((l) => l.id === lojaFiltro)) lojaFiltro = null;
    $('#ofertas-data').textContent = porExtenso(dados.hoje);
    renderSaude(dados.saude);
    renderChips();
    renderLojas();
    renderRodape(dados.saude);
  }

  function init() {
    $('#ofertas-chips')?.addEventListener('click', (ev) => {
      const botao = ev.target.closest('button[data-loja]');
      if (!botao || !dados) return;
      lojaFiltro = botao.dataset.loja === '' ? null : Number(botao.dataset.loja);
      renderChips();
      renderLojas();
    });
    $('#ofertas-busca')?.addEventListener('input', () => {
      if (dados) renderLojas();
    });
    if (window.carregadores) window.carregadores.ofertas = carregar;
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
