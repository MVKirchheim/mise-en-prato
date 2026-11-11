/* Cardápio semanal do mise-en-prato: sete cartões, de segunda a domingo, cada
   um com almoço e jantar. Vanilla JS sem bundler e sem framework.

   A tela nunca guarda estado otimista: toda gravação que dá certo recarrega a
   semana do servidor, e toda que falha mostra a mensagem dele e deixa a tela
   como estava. A data de "hoje" é sempre a LOCAL do navegador — toISOString()
   é UTC e, à noite no Brasil, já seria o dia seguinte. */
'use strict';

(function () {
  const $ = (s) => document.querySelector(s);

  const DIAS_SEMANA = ['Segunda', 'Terça', 'Quarta', 'Quinta', 'Sexta', 'Sábado', 'Domingo'];
  const ROTULO_REFEICAO = { almoco: 'Almoço', jantar: 'Jantar' };

  let semanaAtual = null; // AAAA-MM-DD da segunda-feira exibida
  let entradasPorId = {};
  let todasReceitas = [];
  let receitaEscolhida = null;
  let contexto = null; // { modo: 'adicionar', data, refeicao } | { modo: 'editar', entrada }
  let focoAoFechar = null;
  let buscaAgendada = null;

  // ── utilitários ───────────────────────────────────────────────────────────
  function escapar(texto) {
    return String(texto ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  const doisDigitos = (n) => String(n).padStart(2, '0');

  /* AAAA-MM-DD na hora local do navegador. */
  function isoLocal(d) {
    return `${d.getFullYear()}-${doisDigitos(d.getMonth() + 1)}-${doisDigitos(d.getDate())}`;
  }

  /* "AAAA-MM-DD" vira Date à meia-noite LOCAL (new Date("AAAA-MM-DD") seria UTC). */
  function dataLocal(iso) {
    const [a, m, d] = iso.split('-').map(Number);
    return new Date(a, m - 1, d);
  }

  function somarDias(iso, dias) {
    const d = dataLocal(iso);
    d.setDate(d.getDate() + dias);
    return isoLocal(d);
  }

  const hojeIso = () => isoLocal(new Date());
  const diaMes = (iso) => `${iso.slice(8, 10)}/${iso.slice(5, 7)}`;

  function nomeDoDia(iso) {
    // getDay(): 0 = domingo; a semana da tela começa na segunda.
    return DIAS_SEMANA[(dataLocal(iso).getDay() + 6) % 7];
  }

  const MESES_CURTOS = ['jan', 'fev', 'mar', 'abr', 'mai', 'jun', 'jul', 'ago', 'set', 'out', 'nov', 'dez'];

  /* "16 – 22 nov", ou "30 nov – 6 dez" quando a semana vira o mês. Curto de
     propósito: o toLocaleDateString dá "16 de nov – 22 de nov", que quebra em
     duas linhas num celular de 360px. */
  function intervaloLegivel(inicio, fim) {
    const [, mi, di] = inicio.split('-').map(Number);
    const [, mf, df] = fim.split('-').map(Number);
    return mi === mf
      ? `${di} – ${df} ${MESES_CURTOS[mf - 1]}`
      : `${di} ${MESES_CURTOS[mi - 1]} – ${df} ${MESES_CURTOS[mf - 1]}`;
  }

  async function enviar(caminho, metodo, corpo) {
    const opcoes = { method: metodo };
    if (corpo !== undefined) {
      opcoes.headers = { 'Content-Type': 'application/json' };
      opcoes.body = JSON.stringify(corpo);
    }
    return api(caminho, opcoes);
  }

  // ── semana ────────────────────────────────────────────────────────────────
  async function carregarSemana(dataReferencia, { rolarParaHoje = false } = {}) {
    const ref = dataReferencia || semanaAtual || hojeIso();
    let dados;
    try {
      dados = await api(`/api/cardapio?semana=${encodeURIComponent(ref)}`);
    } catch (err) {
      alert(`Falha ao carregar o cardápio: ${err.message}`);
      return;
    }
    semanaAtual = dados.inicio;
    renderizarSemana(dados);
    if (rolarParaHoje) {
      const cartaoHoje = document.querySelector('#cardapio-dias .cardapio-dia.hoje');
      if (cartaoHoje) cartaoHoje.scrollIntoView({ block: 'center' });
    }
  }

  function renderizarEntrada(e) {
    const porcoes = e.porcoes ? `<span class="cardapio-entrada-porcoes">${Number(e.porcoes)} porç${e.porcoes === 1 ? 'ão' : 'ões'}</span>` : '';
    const miniatura = e.receita_id && e.receita_tem_foto
      ? `<img class="cardapio-miniatura" src="/api/receitas/${Number(e.receita_id)}/foto" alt="" loading="lazy">`
      : '';
    const titulo = escapar(e.titulo);
    const dica = e.receita_id ? 'Abrir no Modo Cozinha' : 'Editar entrada';
    return `
      <li class="cardapio-entrada${e.feito ? ' feita' : ''}">
        <input type="checkbox" class="cardapio-check" data-acao="feito" data-id="${Number(e.id)}"
               ${e.feito ? 'checked' : ''} aria-label="Marcar &quot;${titulo}&quot; como feito">
        <button type="button" class="cardapio-entrada-abrir" data-acao="abrir" data-id="${Number(e.id)}" title="${dica}">
          ${miniatura}
          <span class="cardapio-entrada-texto">
            <span class="cardapio-entrada-titulo">${titulo}</span>
            ${porcoes}
          </span>
        </button>
        <button type="button" class="cardapio-entrada-editar" data-acao="editar" data-id="${Number(e.id)}"
                aria-label="Editar &quot;${titulo}&quot;" title="Editar">✎</button>
      </li>`;
  }

  function renderizarCasa(dia, refeicao) {
    const entradas = dia[refeicao] || [];
    const rotulo = ROTULO_REFEICAO[refeicao];
    const lista = entradas.length
      ? `<ul class="cardapio-entradas">${entradas.map(renderizarEntrada).join('')}</ul>`
      : '<div class="cardapio-casa-vazia fraca">Nada planejado</div>';
    return `
      <div class="cardapio-casa">
        <div class="cardapio-casa-cabeca">
          <span class="cardapio-casa-rotulo">${rotulo}</span>
          <button type="button" class="cardapio-adicionar" data-acao="adicionar"
                  data-data="${dia.data}" data-refeicao="${refeicao}"
                  aria-label="Adicionar ao ${rotulo.toLowerCase()} de ${nomeDoDia(dia.data).toLowerCase()}, ${diaMes(dia.data)}">+</button>
        </div>
        ${lista}
      </div>`;
  }

  function renderizarSemana(dados) {
    const hoje = hojeIso();
    entradasPorId = {};
    let total = 0;
    dados.dias.forEach((dia) => {
      ['almoco', 'jantar'].forEach((r) => {
        (dia[r] || []).forEach((e) => {
          entradasPorId[e.id] = e;
          total += 1;
        });
      });
    });

    $('#cardapio-intervalo').textContent = intervaloLegivel(dados.inicio, dados.fim);
    $('#cardapio-guia').hidden = total > 0;
    $('#cardapio-dias').innerHTML = dados.dias
      .map((dia) => {
        const ehHoje = dia.data === hoje;
        return `
        <article class="cartao cardapio-dia${ehHoje ? ' hoje' : ''}" data-data="${dia.data}"
                 ${ehHoje ? 'aria-current="date"' : ''}>
          <header class="cardapio-dia-cabeca">
            <h3 class="cardapio-dia-nome">${nomeDoDia(dia.data)} <span class="fraca">${diaMes(dia.data)}</span></h3>
            ${ehHoje ? '<span class="selo cardapio-selo-hoje">Hoje</span>' : ''}
          </header>
          ${renderizarCasa(dia, 'almoco')}
          ${renderizarCasa(dia, 'jantar')}
        </article>`;
      })
      .join('');
  }

  // ── ações na lista ────────────────────────────────────────────────────────
  async function alternarFeito(caixa) {
    const id = Number(caixa.dataset.id);
    const marcado = caixa.checked;
    try {
      await enviar(`/api/cardapio/${id}`, 'PUT', { feito: marcado });
    } catch (err) {
      caixa.checked = !marcado; // a tela fica como estava
      alert(`Falha ao marcar a entrada: ${err.message}`);
      return;
    }
    await carregarSemana();
    // O recarregamento troca o DOM; devolve o foco à mesma caixa.
    document.querySelector(`#cardapio-dias .cardapio-check[data-id="${id}"]`)?.focus();
  }

  function abrirEntrada(id, origem) {
    const e = entradasPorId[id];
    if (!e) return;
    if (e.receita_id && typeof window.abrirModoCozinha === 'function') {
      window.abrirModoCozinha(e.receita_id);
    } else {
      abrirSheetEditar(e, origem);
    }
  }

  // ── painel inferior ───────────────────────────────────────────────────────
  function mostrarSheet(origem) {
    focoAoFechar = origem || document.activeElement;
    $('#sheet-cardapio-backdrop').hidden = false;
    $('#sheet-cardapio').hidden = false;
    document.body.style.overflow = 'hidden';
  }

  function sheetAberto() {
    return !$('#sheet-cardapio').hidden;
  }

  function fecharSheet() {
    if (!sheetAberto()) return;
    $('#sheet-cardapio-backdrop').hidden = true;
    $('#sheet-cardapio').hidden = true;
    document.body.style.overflow = '';
    devolverFoco();
    contexto = null;
  }

  /* Volta o foco a quem abriu o painel; se o recarregamento trocou o DOM, ao
     "+" da mesma casa, que é o equivalente mais próximo. */
  function devolverFoco() {
    const alvo = focoAoFechar;
    focoAoFechar = null;
    if (alvo && alvo.isConnected) {
      alvo.focus();
      return;
    }
    const casa = contexto?.modo === 'adicionar' ? contexto : contexto?.entrada;
    if (casa) {
      const botao = document.querySelector(
        `#cardapio-dias .cardapio-adicionar[data-data="${casa.data}"][data-refeicao="${casa.refeicao}"]`
      );
      if (botao) {
        botao.focus();
        return;
      }
    }
    $('#cardapio-intervalo')?.focus?.();
  }

  function legendaCasa(data, refeicao) {
    return `${ROTULO_REFEICAO[refeicao]} · ${nomeDoDia(data)}, ${diaMes(data)}`;
  }

  async function abrirSheetAdicionar(data, refeicao, origem) {
    contexto = { modo: 'adicionar', data, refeicao };
    receitaEscolhida = null;
    $('#sheet-cardapio-titulo').textContent = 'Adicionar ao cardápio';
    $('#sheet-cardapio-subtitulo').textContent = legendaCasa(data, refeicao);
    $('#cardapio-modo-adicionar').hidden = false;
    $('#cardapio-modo-editar').hidden = true;
    $('#cardapio-escolha-receita').hidden = false;
    $('#cardapio-receita-escolhida').hidden = true;
    $('#cardapio-busca').value = '';
    $('#cardapio-texto').value = '';
    $('#cardapio-resultados').innerHTML = '<div class="fraca">Carregando receitas…</div>';
    mostrarSheet(origem);
    $('#cardapio-busca').focus();
    await buscarReceitas('');
  }

  async function buscarReceitas(termo) {
    const pedido = termo;
    try {
      const dados = await api(`/api/receitas?q=${encodeURIComponent(termo)}`);
      // Resposta atrasada de uma busca antiga não sobrescreve a atual.
      if (pedido !== $('#cardapio-busca').value.trim()) return;
      todasReceitas = dados.receitas || [];
    } catch (err) {
      $('#cardapio-resultados').innerHTML = `<div class="fraca">${escapar(err.message)}</div>`;
      return;
    }
    renderizarResultados();
  }

  function renderizarResultados() {
    const caixa = $('#cardapio-resultados');
    if (!todasReceitas.length) {
      caixa.innerHTML = $('#cardapio-busca').value.trim()
        ? '<div class="fraca">Nenhuma receita com esse nome. Use o texto livre abaixo.</div>'
        : '<div class="fraca">O livro de receitas está vazio. Use o texto livre abaixo.</div>';
      return;
    }
    caixa.innerHTML = todasReceitas
      .slice(0, 8)
      .map(
        (r) => `
        <button type="button" class="cardapio-resultado" role="listitem" data-receita-id="${Number(r.id)}">
          <span class="cardapio-resultado-nome">${escapar(r.nome)}</span>
          <span class="fraca">${Number(r.porcoes)} porç${r.porcoes === 1 ? 'ão' : 'ões'}</span>
        </button>`
      )
      .join('');
  }

  function escolherReceita(id) {
    const r = todasReceitas.find((x) => x.id === id);
    if (!r) return;
    receitaEscolhida = r;
    $('#cardapio-escolhida-nome').textContent = r.nome;
    $('#cardapio-add-porcoes').value = String(r.porcoes || 1);
    $('#cardapio-escolha-receita').hidden = true;
    $('#cardapio-receita-escolhida').hidden = false;
    $('#cardapio-add-receita').focus();
  }

  function trocarReceita() {
    receitaEscolhida = null;
    $('#cardapio-receita-escolhida').hidden = true;
    $('#cardapio-escolha-receita').hidden = false;
    $('#cardapio-busca').focus();
  }

  /* Valor do campo de porções: inteiro >= 1, null se vazio, NaN se inválido. */
  function lerPorcoes(campo) {
    const bruto = campo.value.trim();
    if (!bruto) return null;
    const n = Number(bruto);
    return Number.isInteger(n) && n >= 1 ? n : NaN;
  }

  function passoPorcoes(botao) {
    const campo = document.getElementById(botao.dataset.alvo);
    if (!campo) return;
    const atual = Number.parseInt(campo.value, 10);
    const base = Number.isInteger(atual) ? atual : 0;
    campo.value = String(Math.max(1, base + Number(botao.dataset.passo)));
  }

  async function gravar(caminho, metodo, corpo, botao) {
    if (botao) botao.disabled = true;
    try {
      await enviar(caminho, metodo, corpo);
    } catch (err) {
      alert(err.message);
      return false;
    } finally {
      if (botao) botao.disabled = false;
    }
    const semana = contexto?.modo === 'editar' && corpo?.data ? corpo.data : semanaAtual;
    await carregarSemana(semana);
    fecharSheet();
    return true;
  }

  async function adicionarReceita() {
    if (!contexto || contexto.modo !== 'adicionar' || !receitaEscolhida) return;
    const porcoes = lerPorcoes($('#cardapio-add-porcoes'));
    if (Number.isNaN(porcoes)) {
      alert('Porções devem ser um número inteiro maior que zero.');
      return;
    }
    const corpo = { data: contexto.data, refeicao: contexto.refeicao, receita_id: receitaEscolhida.id };
    if (porcoes !== null) corpo.porcoes = porcoes;
    await gravar('/api/cardapio', 'POST', corpo, $('#cardapio-add-receita'));
  }

  async function adicionarTexto(ev) {
    ev.preventDefault();
    if (!contexto || contexto.modo !== 'adicionar') return;
    const titulo = $('#cardapio-texto').value.trim();
    if (!titulo) {
      $('#cardapio-texto').focus();
      return;
    }
    await gravar(
      '/api/cardapio',
      'POST',
      { data: contexto.data, refeicao: contexto.refeicao, titulo },
      ev.submitter || null
    );
  }

  function abrirSheetEditar(e, origem) {
    contexto = { modo: 'editar', entrada: e };
    $('#sheet-cardapio-titulo').textContent = 'Editar entrada';
    $('#sheet-cardapio-subtitulo').textContent = e.receita_id
      ? legendaCasa(e.data, e.refeicao)
      : `${legendaCasa(e.data, e.refeicao)} · texto livre`;
    $('#cardapio-modo-adicionar').hidden = true;
    $('#cardapio-modo-editar').hidden = false;
    $('#cardapio-edit-titulo').value = e.titulo;
    $('#cardapio-edit-porcoes').value = e.porcoes ? String(e.porcoes) : '';
    $('#cardapio-edit-data').value = e.data;
    $('#cardapio-edit-refeicao').value = e.refeicao;
    $('#cardapio-edit-cozinha').hidden = !e.receita_id;
    mostrarSheet(origem);
    $('#cardapio-edit-titulo').focus();
  }

  async function salvarEdicao(ev) {
    ev.preventDefault();
    if (!contexto || contexto.modo !== 'editar') return;
    const titulo = $('#cardapio-edit-titulo').value.trim();
    const porcoes = lerPorcoes($('#cardapio-edit-porcoes'));
    const data = $('#cardapio-edit-data').value;
    const refeicao = $('#cardapio-edit-refeicao').value;
    if (!titulo) {
      alert('O título não pode ficar vazio.');
      return;
    }
    if (Number.isNaN(porcoes)) {
      alert('Porções devem ser um número inteiro maior que zero.');
      return;
    }
    if (!data) {
      alert('Escolha o dia.');
      return;
    }
    await gravar(
      `/api/cardapio/${contexto.entrada.id}`,
      'PUT',
      { titulo, porcoes, data, refeicao },
      $('#cardapio-edit-salvar')
    );
  }

  async function removerEntrada() {
    if (!contexto || contexto.modo !== 'editar') return;
    const e = contexto.entrada;
    if (!confirm(`Remover "${e.titulo}" do cardápio?`)) return;
    await gravar(`/api/cardapio/${e.id}`, 'DELETE', undefined, $('#cardapio-edit-remover'));
  }

  function abrirCozinhaDaEdicao() {
    if (!contexto || contexto.modo !== 'editar' || !contexto.entrada.receita_id) return;
    const receitaId = contexto.entrada.receita_id;
    fecharSheet();
    if (typeof window.abrirModoCozinha === 'function') window.abrirModoCozinha(receitaId);
  }

  // ── inicialização ─────────────────────────────────────────────────────────
  function init() {
    $('#cardapio-anterior')?.addEventListener('click', () => carregarSemana(somarDias(semanaAtual || hojeIso(), -7)));
    $('#cardapio-proxima')?.addEventListener('click', () => carregarSemana(somarDias(semanaAtual || hojeIso(), 7)));
    $('#cardapio-hoje')?.addEventListener('click', () => carregarSemana(hojeIso(), { rolarParaHoje: true }));

    const dias = $('#cardapio-dias');
    dias?.addEventListener('click', (ev) => {
      const alvo = ev.target.closest('[data-acao]');
      if (!alvo || alvo.dataset.acao === 'feito') return;
      if (alvo.dataset.acao === 'adicionar') {
        abrirSheetAdicionar(alvo.dataset.data, alvo.dataset.refeicao, alvo);
      } else if (alvo.dataset.acao === 'abrir') {
        abrirEntrada(Number(alvo.dataset.id), alvo);
      } else if (alvo.dataset.acao === 'editar') {
        const e = entradasPorId[Number(alvo.dataset.id)];
        if (e) abrirSheetEditar(e, alvo);
      }
    });
    dias?.addEventListener('change', (ev) => {
      if (ev.target.matches('.cardapio-check')) alternarFeito(ev.target);
    });

    $('#sheet-cardapio-backdrop')?.addEventListener('click', fecharSheet);
    $('#sheet-cardapio-fechar')?.addEventListener('click', fecharSheet);
    $('#sheet-cardapio')?.addEventListener('click', (ev) => {
      const passo = ev.target.closest('.cardapio-stepper-btn');
      if (passo) passoPorcoes(passo);
    });

    $('#cardapio-busca')?.addEventListener('input', (ev) => {
      clearTimeout(buscaAgendada);
      buscaAgendada = setTimeout(() => buscarReceitas(ev.target.value.trim()), 250);
    });
    $('#cardapio-resultados')?.addEventListener('click', (ev) => {
      const item = ev.target.closest('[data-receita-id]');
      if (item) escolherReceita(Number(item.dataset.receitaId));
    });
    $('#cardapio-trocar-receita')?.addEventListener('click', trocarReceita);
    $('#cardapio-add-receita')?.addEventListener('click', adicionarReceita);
    $('#cardapio-form-texto')?.addEventListener('submit', adicionarTexto);
    $('#cardapio-modo-editar')?.addEventListener('submit', salvarEdicao);
    $('#cardapio-edit-remover')?.addEventListener('click', removerEntrada);
    $('#cardapio-edit-cozinha')?.addEventListener('click', abrirCozinhaDaEdicao);

    document.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape' && sheetAberto()) fecharSheet();
    });

    // Registra carregador na navegação de abas do app.js
    if (window.carregadores) {
      // Abrir a aba sempre volta para a semana de hoje, com o dia em foco.
      window.carregadores['cardapio'] = () => carregarSemana(hojeIso(), { rolarParaHoje: true });
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
