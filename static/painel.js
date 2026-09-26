"use strict";

const EXEMPLOS = [
  "Como está o estoque?",
  "Qual o status do pedido 1004?",
  "Cancele o pedido 1003 e avise o cliente",
  "Cancele o pedido 1002",
  "Reembolse R$ 50 do pedido 1001, o mouse veio arranhado",
  "Crie um pedido para o cliente 3 com 2 unidades do produto 2",
  "Analise o pedido 1005 e resolva o que for preciso",
];

const EVENTOS = {
  inicio: { rotulo: "Tarefa recebida", cor: "var(--destaque)" },
  modelo: { rotulo: "Chamada ao modelo", cor: "var(--suave)" },
  pensamento: { rotulo: "Raciocínio", cor: "var(--suave)" },
  texto: { rotulo: "Agente", cor: "var(--destaque)" },
  chamada: { rotulo: "Quer usar uma ferramenta", cor: "var(--destaque)" },
  ferramenta_executada: { rotulo: "Executada", cor: "var(--ok)" },
  erro_ferramenta: { rotulo: "A ferramenta falhou", cor: "var(--perigo)" },
  bloqueio: { rotulo: "Bloqueada pelas regras", cor: "var(--perigo)" },
  aguardando_aprovacao: { rotulo: "Precisa da sua aprovação", cor: "var(--alerta)" },
  aprovada: { rotulo: "Aprovada", cor: "var(--ok)" },
  rejeitada: { rotulo: "Rejeitada", cor: "var(--perigo)" },
  fallback: { rotulo: "Troca de modelo", cor: "var(--alerta)" },
  recusa: { rotulo: "O modelo recusou", cor: "var(--perigo)" },
  limite: { rotulo: "Limite atingido", cor: "var(--perigo)" },
  erro: { rotulo: "Erro", cor: "var(--perigo)" },
  cancelada: { rotulo: "Cancelada", cor: "var(--perigo)" },
  fim: { rotulo: "Concluída", cor: "var(--ok)" },
};

const STATUS = {
  executando: ["Executando", "destaque"],
  aguardando_aprovacao: ["Aguardando aprovação", "alerta"],
  concluida: ["Concluída", "ok"],
  interrompida: ["Interrompida", "perigo"],
  cancelada: ["Cancelada", "perigo"],
  recusada: ["Recusada", "perigo"],
  erro: ["Erro", "perigo"],
};

const STATUS_PEDIDO = {
  pendente: "", pago: "destaque", enviado: "destaque", entregue: "ok", cancelado: "perigo", reembolsado: "alerta",
};

const RISCO = { leitura: ["leitura", "ok"], escrita: ["escrita", "destaque"], sensivel: ["sensível", "alerta"] };
const PAGINAS = ["inicio", "agente", "aprovacoes", "historico", "loja", "controles"];
const CHAVE_VISITOU = "sentinela.visitou";
const CHAVE_TECNICO = "sentinela.detalhes_tecnicos";

const NOMES_CAMPOS = {
  pedido_id: "Pedido", cliente_id: "Cliente", valor: "Valor", motivo: "Motivo",
  para: "Para", assunto: "Assunto", corpo: "Mensagem", itens: "Itens", busca: "Busca",
};

const estado = {
  pagina: null,
  status: null,
  selecionada: null,
  subLoja: "pedidos",
  assinaturas: {},          // evita redesenhar o que não mudou
  pendentesVistas: null,    // ids de aprovações já conhecidas (para o aviso)
  verTecnico: false,
};

// ================================================================ utilidades
const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];

function esc(valor) {
  return String(valor ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
}

async function api(caminho, opcoes = {}) {
  const resp = await fetch(caminho, {
    headers: { "Content-Type": "application/json" },
    ...opcoes,
    body: opcoes.body ? JSON.stringify(opcoes.body) : undefined,
  });
  const dados = await resp.json().catch(() => ({}));
  if (resp.status === 401 && caminho !== "/api/login") mostrarLogin();
  if (!resp.ok) throw new Error(dados.detail || `Erro ${resp.status}`);
  return dados;
}

const reais = (v) => Number(v).toLocaleString("pt-BR", { style: "currency", currency: "BRL" });
const numero = (v) => Number(v).toLocaleString("pt-BR");
const hora = (iso) => (iso ? iso.slice(11, 16) : "");
const chip = (texto, tipo = "") => `<span class="chip ${tipo}">${esc(texto)}</span>`;
const chipStatus = (s) => chip(...(STATUS[s] || [s, ""]));

function json(valor) {
  if (typeof valor === "string") {
    try { valor = JSON.parse(valor); } catch { return esc(valor); }
  }
  return esc(JSON.stringify(valor, null, 2));
}

/** Mostra a entrada de uma ferramenta como uma lista legível, e não como JSON. */
function campos(entrada) {
  const valor = (chave, v) => {
    if (chave === "valor" && typeof v === "number") return reais(v);
    if (chave === "itens" && Array.isArray(v)) return v.map((i) => `${i.quantidade}× produto ${i.produto_id}`).join(", ");
    if (typeof v === "object" && v !== null) return JSON.stringify(v);
    return String(v);
  };
  return `<dl class="campos">${Object.entries(entrada || {})
    .map(([k, v]) => `<dt>${esc(NOMES_CAMPOS[k] || k)}</dt><dd>${esc(valor(k, v))}</dd>`)
    .join("")}</dl>`;
}

function lerPreferencia(chave) {
  try { return localStorage.getItem(chave); } catch { return null; }
}
function gravarPreferencia(chave, valor) {
  try { localStorage.setItem(chave, valor); } catch { /* sem armazenamento: tudo bem */ }
}

/** Redesenha um elemento só quando o conteúdo muda e ninguém está digitando nele. */
function desenhar(alvo, chave, assinatura, html) {
  const ativo = document.activeElement;
  if (ativo && alvo.contains(ativo) && ativo.tagName === "INPUT") return false;
  if (estado.assinaturas[chave] === assinatura) return false;
  estado.assinaturas[chave] = assinatura;
  alvo.innerHTML = html;
  return true;
}

// ================================================================ navegação
function paginaDoEndereco() {
  const nome = location.hash.slice(1);
  if (PAGINAS.includes(nome)) return nome;
  return lerPreferencia(CHAVE_VISITOU) ? "agente" : "inicio";
}

function mostrarPagina(nome) {
  estado.pagina = nome;
  gravarPreferencia(CHAVE_VISITOU, "1");
  $$(".pagina").forEach((p) => (p.hidden = p.dataset.pagina !== nome));
  $$(".menu a").forEach((a) => {
    const ativo = a.dataset.pagina === nome;
    a.classList.toggle("ativo", ativo);
    if (ativo) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  window.scrollTo(0, 0);
  if (nome === "controles") carregarControles();
  atualizarPagina();
}

window.addEventListener("hashchange", () => mostrarPagina(paginaDoEndereco()));

function irPara(nome) {
  if (location.hash === "#" + nome) mostrarPagina(nome);
  else location.hash = nome;
}

// ===================================================================== topo
async function atualizarStatus() {
  const s = await api("/api/status");
  estado.status = s;
  const real = s.modo === "real";
  $("#modo").className = "chip " + (real ? "destaque" : "");
  $("#modo").textContent = real ? `${s.provedor} · ${s.modelo}` : "Modo simulado";
  $("#modo").title = s.aviso
    || (real
      ? `IA de verdade: ${s.provedor} (${s.gratuito ? "plano gratuito" : "pago por uso"})`
      : "Sem IA: um planejador por regras imita o modelo, sem custo");
  const indicador = $("#estado-agente");
  indicador.className = "status " + (s.agente_ativo ? "ativo" : "pausado");
  indicador.querySelector("span").textContent = s.agente_ativo ? "Agente ativo" : "Agente pausado";
  const btn = $("#btn-pausa");
  btn.textContent = s.agente_ativo ? "Pausar agente" : "Reativar agente";
  btn.classList.toggle("perigo", s.agente_ativo);
  btn.classList.toggle("sucesso", !s.agente_ativo);
}

$("#btn-pausa").addEventListener("click", async () => {
  await api("/api/controles", { method: "PUT", body: { agente_ativo: !estado.status.agente_ativo } });
  await ciclo();
});

// ============================================== aprovações: selo e aviso
async function atualizarPendentes() {
  const lista = await api("/api/aprovacoes");
  const selo = $("#selo-aprovacoes");
  selo.textContent = lista.length;
  selo.hidden = lista.length === 0;

  const ids = new Set(lista.map((a) => a.id));
  if (estado.pendentesVistas) {
    const novas = lista.filter((a) => !estado.pendentesVistas.has(a.id));
    const jaVendo = (a) => estado.pagina === "aprovacoes" || (estado.pagina === "agente" && a.execucao_id === estado.selecionada);
    const avisar = novas.filter((a) => !jaVendo(a));
    if (avisar.length) mostrarAviso(`Ação aguardando sua aprovação: ${avisar[0].ferramenta}`, () => irPara("aprovacoes"));
  }
  estado.pendentesVistas = ids;
  return lista;
}

let temporizadorAviso = null;
function mostrarAviso(texto, aoClicar) {
  const aviso = $("#aviso");
  aviso.textContent = texto;
  aviso.hidden = false;
  aviso.onclick = () => { aviso.hidden = true; aoClicar(); };
  clearTimeout(temporizadorAviso);
  temporizadorAviso = setTimeout(() => (aviso.hidden = true), 7000);
}

async function decidir(aprovacaoId, aprovar, comentario, botoes) {
  botoes.forEach((b) => (b.disabled = true));
  try {
    const r = await api(`/api/aprovacoes/${aprovacaoId}/decisao`, { method: "POST", body: { aprovar, comentario } });
    estado.selecionada = r.execucao_id;
  } catch (e) {
    alert(e.message);
  }
  if (document.activeElement) document.activeElement.blur();
  estado.assinaturas = {};
  await ciclo();
}

// Um único tratador para os botões Aprovar/Rejeitar, na aba Aprovações e na linha do tempo.
document.addEventListener("click", (ev) => {
  const botao = ev.target.closest("[data-decisao]");
  if (!botao) return;
  const caixa = botao.closest("[data-aprovacao]");
  decidir(
    Number(caixa.dataset.aprovacao),
    botao.dataset.decisao === "1",
    caixa.querySelector("input")?.value || "",
    [...caixa.querySelectorAll("button")],
  );
});

function caixaDecisao() {
  return `
    <div class="acoes-decisao">
      <input type="text" placeholder="Comentário para o agente (opcional)" maxlength="500" aria-label="Comentário">
      <button class="botao sucesso" data-decisao="1" type="button">Aprovar</button>
      <button class="botao perigo" data-decisao="0" type="button">Rejeitar</button>
    </div>`;
}

// =================================================================== Início
async function renderInicio() {
  const [m, c] = await Promise.all([api("/api/metricas"), api("/api/controles")]);
  const uso = c.orcamento_tokens_dia ? Math.min(100, (100 * m.tokens_hoje) / c.orcamento_tokens_dia) : 100;
  const cartoes = [
    { rotulo: "Tarefas", valor: numero(m.execucoes) },
    { rotulo: "Ações executadas", valor: numero(m.acoes_executadas) },
    { rotulo: "Bloqueadas pelas regras", valor: numero(m.acoes_bloqueadas) },
    { rotulo: "Aguardando você", valor: numero(m.aprovacoes_pendentes), chamativa: m.aprovacoes_pendentes > 0 },
    { rotulo: "Tokens hoje", valor: numero(m.tokens_hoje), barra: uso, extra: `${uso.toFixed(0)}% do orçamento diário` },
    {
      rotulo: "Custo",
      valor: m.modo === "real" && !m.gratuito ? `US$ ${m.custo_usd.toFixed(3)}` : "US$ 0",
      extra: m.modo !== "real" ? "modo simulado" : m.gratuito ? `plano gratuito do ${m.provedor}` : "total gasto",
    },
  ];
  const html = cartoes.map((k) => `
    <div class="metrica ${k.chamativa ? "chamativa" : ""}">
      <div class="rotulo">${esc(k.rotulo)}</div>
      <div class="valor">${esc(k.valor)}</div>
      ${k.barra !== undefined ? `<div class="barra-uso"><span style="width:${k.barra}%"></span></div>` : ""}
      ${k.extra ? `<div class="extra">${esc(k.extra)}</div>` : ""}
    </div>`).join("");
  desenhar($("#metricas"), "metricas", html, html);
}

// =================================================================== Agente
$("#exemplos").innerHTML = EXEMPLOS.map((e) => `<button type="button" class="exemplo">${esc(e)}</button>`).join("");
$("#exemplos").addEventListener("click", (ev) => {
  if (!ev.target.classList.contains("exemplo")) return;
  $("#tarefa").value = ev.target.textContent;
  $("#tarefa").focus();
});

$("#form-tarefa").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const botao = $("#form-tarefa button[type=submit]");
  const erro = $("#erro-tarefa");
  erro.hidden = true;
  botao.disabled = true;
  try {
    const { execucao_id } = await api("/api/tarefas", { method: "POST", body: { tarefa: $("#tarefa").value } });
    $("#tarefa").value = "";
    estado.selecionada = execucao_id;
    await ciclo();
  } catch (e) {
    erro.textContent = e.message;
    erro.hidden = false;
  } finally {
    botao.disabled = false;
  }
});

function corpoEvento(e, aprovacoes) {
  const d = e.dados || {};
  const detalhes = (rotulo, valor) => `<details data-id="${e.id}"><summary>${rotulo}</summary><pre>${json(valor)}</pre></details>`;
  switch (e.tipo) {
    case "inicio": return `<div class="corpo">${esc(d.tarefa)}</div>${d.por ? `<div class="corpo suave">por ${esc(d.por)}</div>` : ""}`;
    case "texto": return `<div class="corpo">${esc(d.texto)}</div>`;
    case "pensamento": return `<div class="corpo suave">${esc(d.texto)}</div>`;
    case "modelo":
      return `<div class="corpo suave">${esc(d.modelo)} · ${numero(d.tokens_entrada)} tokens de entrada, ${numero(d.tokens_saida)} de saída${d.custo_usd ? ` · US$ ${d.custo_usd.toFixed(5)}` : ""}</div>`;
    case "chamada":
      return `<div class="corpo"><code>${esc(d.ferramenta)}</code></div>${detalhes("ver parâmetros", d.entrada)}`;
    case "ferramenta_executada":
      return `<div class="corpo"><code>${esc(d.ferramenta)}</code></div>${detalhes("ver resultado", d.resultado)}`;
    case "aguardando_aprovacao": {
      const ap = aprovacoes.find((a) => a.id === d.aprovacao_id);
      const pendente = ap && ap.status === "pendente";
      return `<div class="corpo"><code>${esc(d.ferramenta)}</code>${pendente ? " · decida no topo da tarefa ↑" : ""}</div>`;
    }
    case "bloqueio":
      return `<div class="corpo"><code>${esc(d.ferramenta)}</code> ${esc(d.motivo)}</div>`;
    case "aprovada":
    case "rejeitada":
      return `<div class="corpo"><code>${esc(d.ferramenta)}</code>${d.por ? ` por ${esc(d.por)}` : ""}${d.comentario ? ` “${esc(d.comentario)}”` : ""}</div>`;
    case "erro_ferramenta":
      return `<div class="corpo"><code>${esc(d.ferramenta)}</code> ${esc(d.erro)}</div>`;
    case "erro": return `<div class="corpo">${esc(d.erro || e.titulo)}</div>`;
    case "recusa": return `<div class="corpo">${esc(d?.explicacao || e.titulo)}</div>`;
    case "fim": return "";
    default: return `<div class="corpo">${esc(e.titulo)}</div>`;
  }
}

async function renderAgente() {
  if (!estado.selecionada) {
    const lista = await api("/api/execucoes");
    if (!lista.length) return;
    estado.selecionada = lista[0].id;
  }
  const ex = await api(`/api/execucoes/${estado.selecionada}`);
  const assinatura = JSON.stringify([ex.id, ex.status, ex.eventos.length, ex.aprovacoes.map((a) => a.status), estado.verTecnico]);
  const eventos = ex.eventos.filter((e) => estado.verTecnico || e.tipo !== "modelo");
  const decisoes = ex.aprovacoes
    .filter((a) => a.status === "pendente")
    .map((a) => `
      <div class="decidir" data-aprovacao="${a.id}">
        <p class="titulo-decisao"><strong>Precisa da sua decisão:</strong> <code>${esc(a.ferramenta)}</code></p>
        ${campos(a.entrada)}
        <p class="motivo">${esc(a.motivo)}</p>
        ${caixaDecisao()}
      </div>`)
    .join("");
  const abertos = new Set($$("#detalhe details[open]").map((d) => d.dataset.id));
  const final = ex.status === "concluida" && ex.resposta_final
    ? `<div class="resposta-final"><span class="rotulo">Resposta do agente</span>${esc(ex.resposta_final)}</div>`
    : "";
  const html = `
    <div class="resumo-exec">
      ${chipStatus(ex.status)}
      ${chip(`${ex.passos} passo(s)`)}
      ${chip(`${numero(ex.tokens_entrada + ex.tokens_saida)} tokens`)}
      ${ex.modo === "real" ? chip(`US$ ${ex.custo_usd.toFixed(4)}`) : ""}
    </div>
    <p class="tarefa-citada">“${esc(ex.tarefa)}”</p>
    ${decisoes}
    ${final}
    <ol class="linha-tempo">
      ${eventos.map((e) => {
        const info = EVENTOS[e.tipo] || { rotulo: e.tipo, cor: "var(--suave)" };
        return `<li class="evento" style="--cor:${info.cor}">
          <div class="cab"><strong>${esc(info.rotulo)}</strong><span class="hora">${hora(e.criado_em)}</span></div>
          ${corpoEvento(e, ex.aprovacoes)}
        </li>`;
      }).join("")}
    </ol>`;
  $("#titulo-detalhe").textContent = `Tarefa #${ex.id}`;
  $("#btn-cancelar-exec").hidden = !["executando", "aguardando_aprovacao"].includes(ex.status);
  if (desenhar($("#detalhe"), "agente", assinatura, html)) {
    abertos.forEach((id) => {
      const d = $(`#detalhe details[data-id="${id}"]`);
      if (d) d.open = true;
    });
  }
}

estado.verTecnico = lerPreferencia(CHAVE_TECNICO) === "1";
$("#ver-tecnico").checked = estado.verTecnico;
$("#ver-tecnico").addEventListener("change", (ev) => {
  estado.verTecnico = ev.target.checked;
  gravarPreferencia(CHAVE_TECNICO, estado.verTecnico ? "1" : "0");
  renderAgente();
});

$("#btn-cancelar-exec").addEventListener("click", async () => {
  if (!confirm("Cancelar esta tarefa? Ações pendentes serão rejeitadas.")) return;
  try {
    await api(`/api/execucoes/${estado.selecionada}/cancelar`, { method: "POST" });
  } catch (e) {
    alert(e.message);
  }
  await ciclo();
});

// =============================================================== Aprovações
function renderAprovacoes(lista) {
  const assinatura = lista.map((a) => a.id).join(",");
  const html = lista.length
    ? lista.map((a) => `
      <div class="cartao aprovacao" data-aprovacao="${a.id}">
        <div class="cab"><strong>${esc(a.ferramenta)}</strong>${chip(`tarefa #${a.execucao_id}`)}</div>
        <p class="tarefa">“${esc(a.tarefa)}”</p>
        ${campos(a.entrada)}
        <p class="motivo">${esc(a.motivo)}</p>
        ${caixaDecisao()}
      </div>`).join("")
    : `<div class="vazio-grande">
        <strong>Nada esperando por você</strong>
        <span>Quando o agente quiser cancelar, reembolsar ou mandar e-mail, a ação aparece aqui antes de acontecer.</span>
      </div>`;
  desenhar($("#aprovacoes"), "aprovacoes", assinatura, html);
}

// ================================================================ Histórico
async function renderHistorico() {
  const lista = await api("/api/execucoes");
  const assinatura = JSON.stringify(lista.map((e) => [e.id, e.status, e.passos]));
  const html = lista.length
    ? lista.map((e) => `
      <button class="item-exec" data-id="${e.id}" type="button">
        <span class="t">${esc(e.tarefa)}</span>
        ${chipStatus(e.status)}
        <span class="m">#${e.id} · ${hora(e.criado_em)}${e.autor ? ` · por ${esc(e.autor)}` : ""} · ${e.passos} passo(s) · ${numero(e.tokens_entrada + e.tokens_saida)} tokens${e.modo === "real" ? ` · US$ ${e.custo_usd.toFixed(4)}` : ""}</span>
      </button>`).join("")
    : `<div class="vazio-grande"><strong>Nenhuma tarefa ainda</strong><span>As tarefas que você der ao agente aparecem aqui.</span></div>`;
  desenhar($("#execucoes"), "historico", assinatura, html);
}

$("#execucoes").addEventListener("click", (ev) => {
  const item = ev.target.closest(".item-exec");
  if (!item) return;
  estado.selecionada = Number(item.dataset.id);
  irPara("agente");
});

// ===================================================================== Loja
async function renderLoja() {
  const l = await api("/api/loja");
  $("#qtd-emails").textContent = l.emails.length || "";
  $("#qtd-reembolsos").textContent = l.reembolsos.length || "";
  $("#tabela-pedidos").innerHTML = `
    <thead><tr><th>Pedido</th><th>Cliente</th><th>Status</th><th>Itens</th><th class="num">Total</th><th class="num">Reembolsado</th><th>Observação</th></tr></thead>
    <tbody>${l.pedidos.map((p) => `<tr>
      <td><strong>${p.id}</strong></td><td>${esc(p.cliente.nome)}</td><td>${chip(p.status, STATUS_PEDIDO[p.status] || "")}</td>
      <td>${p.itens.map((i) => `${i.quantidade}× ${esc(i.nome)}`).join("<br>")}</td>
      <td class="num">${reais(p.total)}</td><td class="num">${reais(p.total_reembolsado)}</td>
      <td class="obs">${esc(p.observacao)}</td></tr>`).join("")}</tbody>`;
  $("#tabela-produtos").innerHTML = `
    <thead><tr><th>#</th><th>Produto</th><th>SKU</th><th class="num">Preço</th><th class="num">Estoque</th></tr></thead>
    <tbody>${l.produtos.map((p) => `<tr><td>${p.id}</td><td>${esc(p.nome)}</td><td><code>${esc(p.sku)}</code></td>
      <td class="num">${reais(p.preco)}</td>
      <td class="num">${p.estoque === 0 ? chip("esgotado", "perigo") : p.estoque <= 5 ? chip(p.estoque, "alerta") : p.estoque}</td></tr>`).join("")}</tbody>`;
  $("#lista-emails").innerHTML = l.emails.length
    ? l.emails.map((e) => `<div class="email"><div class="de">Para ${esc(e.para)} · ${hora(e.enviado_em)}</div><strong>${esc(e.assunto)}</strong><pre>${esc(e.corpo)}</pre></div>`).join("")
    : '<div class="vazio-grande"><strong>Nenhum e-mail enviado</strong><span>E-mails que o agente enviar aparecem aqui.</span></div>';
  $("#tabela-reembolsos").innerHTML = l.reembolsos.length
    ? `<thead><tr><th>Pedido</th><th class="num">Valor</th><th>Motivo</th><th>Quando</th></tr></thead>
       <tbody>${l.reembolsos.map((r) => `<tr><td><strong>${r.pedido_id}</strong></td><td class="num">${reais(r.valor)}</td><td>${esc(r.motivo)}</td><td>${hora(r.criado_em)}</td></tr>`).join("")}</tbody>`
    : '<tbody><tr><td><div class="vazio-grande"><strong>Nenhum reembolso</strong><span>Reembolsos aprovados aparecem aqui.</span></div></td></tr></tbody>';
}

$("#abas-loja").addEventListener("click", (ev) => {
  const seg = ev.target.closest(".segmento");
  if (!seg) return;
  estado.subLoja = seg.dataset.sub;
  $$("#abas-loja .segmento").forEach((s) => s.classList.toggle("ativo", s === seg));
  $$('.pagina[data-pagina="loja"] .cartao > [data-sub]').forEach((p) => (p.hidden = p.dataset.sub !== estado.subLoja));
});

$("#btn-resetar").addEventListener("click", async () => {
  if (!confirm("Restaurar a demonstração? Pedidos, estoque, histórico e controles voltam ao início.")) return;
  await api("/api/demo/resetar", { method: "POST" });
  estado.selecionada = null;
  estado.assinaturas = {};
  $("#titulo-detalhe").textContent = "Tarefa atual";
  $("#btn-cancelar-exec").hidden = true;
  $("#detalhe").innerHTML = '<div class="vazio-grande"><strong>Demonstração restaurada</strong><span>Tudo voltou ao estado inicial.</span></div>';
  await ciclo();
});

// ================================================================ Controles
// ------------------------------------------------------- IA pela aba Controles
function mostrarIA(d) {
  const ligada = d.modo === "groq";
  const chipIA = $("#ia-estado");
  chipIA.className = "chip " + (ligada ? "ok" : "");
  chipIA.textContent = ligada ? `Ligada · ${d.provedor} · ${d.modelo}` : "Desligada · modo simulado";
  $("#btn-ligar-ia").textContent = ligada ? "Trocar chave" : d.chave_configurada ? "Ligar IA" : "Ligar IA";
  $("#chave-groq").placeholder = d.chave_configurada
    ? `Chave salva terminando em …${d.chave_final}. Cole outra para trocar.`
    : "Cole aqui a chave do Groq (começa com gsk_)";
  $("#btn-desligar-ia").hidden = !ligada;
  $("#btn-remover-chave").hidden = !d.chave_pelo_painel;
}

function mensagemIA(texto, tipo = "") {
  const m = $("#ia-mensagem");
  m.textContent = texto;
  m.className = "dica " + tipo;
}

async function carregarIA() {
  try { mostrarIA(await api("/api/ia")); } catch (e) { console.error(e); }
}

async function mudarIA(corpo, botao, textoEspera) {
  const botoes = $$("#form-ia button");
  botoes.forEach((b) => (b.disabled = true));
  mensagemIA(textoEspera);
  try {
    const d = corpo === null
      ? await api("/api/ia/chave", { method: "DELETE" })
      : await api("/api/ia", { method: "PUT", body: corpo });
    $("#chave-groq").value = "";
    mostrarIA(d);
    const aviso = d.guardada_temporariamente
      ? " Na hospedagem grátis a chave some quando o site hiberna ou é atualizado; se a IA desligar sozinha, é só colar de novo."
      : "";
    mensagemIA(
      d.modo === "groq" ? "Pronto! A IA está ligada. Teste na aba Agente." + aviso : "IA desligada: o agente voltou ao modo simulado.",
      "ok",
    );
    await atualizarStatus();
  } catch (e) {
    mensagemIA(e.message, "erro");
  } finally {
    botoes.forEach((b) => (b.disabled = false));
  }
}

$("#form-ia").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const chave = $("#chave-groq").value.trim();
  mudarIA(chave ? { modo: "groq", chave } : { modo: "groq" }, ev.submitter, "Conferindo a chave com o Groq…");
});
$("#btn-desligar-ia").addEventListener("click", () => mudarIA({ modo: "simulado" }, null, "Desligando…"));
$("#btn-remover-chave").addEventListener("click", () => {
  if (confirm("Remover a chave salva? A IA volta para o modo simulado.")) mudarIA(null, null, "Removendo…");
});

async function carregarControles() {
  carregarIA();
  const c = await api("/api/controles");
  const riscos = estado.status?.ferramentas || (await api("/api/status")).ferramentas;
  $("#tabela-ferramentas tbody").innerHTML = Object.entries(c.ferramentas)
    .map(([nome, conf]) => {
      const [rotulo, tipo] = RISCO[riscos[nome]?.risco] || ["?", ""];
      return `<tr>
        <td><code>${esc(nome)}</code></td>
        <td>${chip(rotulo, tipo)}</td>
        <td><input type="checkbox" class="switch" data-ferramenta="${esc(nome)}" data-campo="ativa" ${conf.ativa ? "checked" : ""} aria-label="${esc(nome)} ativa"></td>
        <td><input type="checkbox" class="switch" data-ferramenta="${esc(nome)}" data-campo="exige_aprovacao" ${conf.exige_aprovacao ? "checked" : ""} aria-label="${esc(nome)} exige aprovação"></td>
      </tr>`;
    })
    .join("");
  for (const campo of ["limite_reembolso", "max_passos", "orcamento_tokens_dia"]) $("#" + campo).value = c[campo];
  $("#email_somente_clientes").checked = c.email_somente_clientes;
}

$("#tabela-ferramentas").addEventListener("change", async (ev) => {
  const alvo = ev.target;
  if (!alvo.dataset.ferramenta) return;
  await api("/api/controles", {
    method: "PUT",
    body: { ferramentas: { [alvo.dataset.ferramenta]: { [alvo.dataset.campo]: alvo.checked } } },
  });
});

$("#btn-salvar-limites").addEventListener("click", async () => {
  try {
    await api("/api/controles", {
      method: "PUT",
      body: {
        limite_reembolso: Number($("#limite_reembolso").value),
        max_passos: Number($("#max_passos").value),
        orcamento_tokens_dia: Number($("#orcamento_tokens_dia").value),
        email_somente_clientes: $("#email_somente_clientes").checked,
      },
    });
    $("#salvo").hidden = false;
    setTimeout(() => ($("#salvo").hidden = true), 1500);
  } catch (e) {
    alert(e.message);
  }
});

// ==================================================================== ciclo
let pendentesAtuais = [];

async function atualizarPagina() {
  switch (estado.pagina) {
    case "inicio": return renderInicio();
    case "agente": return renderAgente();
    case "aprovacoes": return renderAprovacoes(pendentesAtuais);
    case "historico": return renderHistorico();
    case "loja": return renderLoja();
    default: return undefined;
  }
}

async function ciclo() {
  try {
    await atualizarStatus();
    pendentesAtuais = await atualizarPendentes();
    await atualizarPagina();
  } catch (e) {
    console.error(e);
  }
}

// ==================================================================== login
let cicloAtivo = null;
estado.papel = "admin";

function trocarFormLogin(nome) {
  $$(".abas-login .segmento").forEach((b) => b.classList.toggle("ativo", b.dataset.form === nome));
  $$(".form-login").forEach((f) => (f.hidden = f.dataset.form !== nome));
  $("#erro-login").hidden = true;
  const primeiro = $(`.form-login[data-form="${nome}"] input`);
  if (primeiro) setTimeout(() => primeiro.focus(), 50);
}
$$(".abas-login .segmento").forEach((b) => b.addEventListener("click", () => trocarFormLogin(b.dataset.form)));

async function mostrarLogin() {
  if (cicloAtivo) clearInterval(cicloAtivo);
  cicloAtivo = null;
  $$("#tela-login input").forEach((i) => (i.value = ""));
  $("#tela-login").hidden = false;
  let s = {};
  try { s = await (await fetch("/api/sessao")).json(); } catch { /* segue com o padrão */ }
  const precisaAdmin = s.admin_configurado === false;
  $("#aba-admin").hidden = !precisaAdmin;
  trocarFormLogin(precisaAdmin ? "admin" : "entrar");
}

/** Mostra ou esconde o que é só do administrador. */
function aplicarPapel(sessao) {
  estado.papel = sessao.papel || "admin";
  const admin = estado.papel === "admin";
  $("#btn-pausa").hidden = !admin;
  $("#btn-resetar").hidden = !admin;
  $(".cartao-ia").hidden = !admin;
  $$('.pagina[data-pagina="controles"] .duas-colunas').forEach((el) => el.classList.toggle("so-leitura", !admin));
  $("#btn-salvar-limites").hidden = !admin;
  let aviso = $("#aviso-visitante");
  if (!admin && !aviso) {
    aviso = document.createElement("p");
    aviso.id = "aviso-visitante";
    aviso.className = "aviso-visitante";
    aviso.textContent = "Você entrou como visitante: pode ver as regras, mas só o administrador altera.";
    $('.pagina[data-pagina="controles"] .cab-pagina').after(aviso);
  }
  if (aviso) aviso.hidden = admin;
  $("#btn-sair").hidden = !sessao.login_necessario;
  const quem = $("#quem-sou");
  quem.hidden = !sessao.login_necessario;
  quem.innerHTML = sessao.usuario ? `<b>${esc(sessao.usuario)}</b> · ${admin ? "administrador" : "visitante"}` : "";
}

async function iniciarPainel(sessao) {
  $("#tela-login").hidden = true;
  if (sessao) aplicarPapel(sessao);
  await atualizarStatus().catch(console.error);
  mostrarPagina(paginaDoEndereco());
  await ciclo();
  if (!cicloAtivo) cicloAtivo = setInterval(ciclo, 1500);
}

async function enviarLogin(ev, caminho, corpo, conferir) {
  ev.preventDefault();
  const erro = $("#erro-login");
  const botao = ev.target.querySelector("button[type=submit]");
  erro.hidden = true;
  const problema = conferir ? conferir() : "";
  if (problema) {
    erro.textContent = problema;
    erro.hidden = false;
    return;
  }
  botao.disabled = true;
  try {
    const r = await api(caminho, { method: "POST", body: corpo() });
    estado.pendentesVistas = null;
    estado.assinaturas = {};
    await iniciarPainel({ login_necessario: true, ...r });
  } catch (e) {
    erro.textContent = e.message;
    erro.hidden = false;
  } finally {
    botao.disabled = false;
  }
}

$("#form-entrar").addEventListener("submit", (ev) =>
  enviarLogin(ev, "/api/login", () => ({ usuario: $("#login-usuario").value.trim(), senha: $("#login-senha").value })));

$("#form-criar").addEventListener("submit", (ev) =>
  enviarLogin(
    ev, "/api/contas",
    () => ({ usuario: $("#criar-usuario").value.trim(), senha: $("#criar-senha").value }),
    () => ($("#criar-senha").value !== $("#criar-senha2").value ? "As duas senhas não são iguais." : ""),
  ));

$("#form-admin").addEventListener("submit", (ev) =>
  enviarLogin(
    ev, "/api/admin/configurar",
    () => ({ codigo: $("#admin-codigo").value.trim(), senha: $("#admin-senha").value }),
    () => ($("#admin-senha").value !== $("#admin-senha2").value ? "As duas senhas não são iguais." : ""),
  ));

$("#btn-sair").addEventListener("click", async () => {
  await api("/api/logout", { method: "POST", body: {} }).catch(() => {});
  mostrarLogin();
});

(async () => {
  let sessao = { login_necessario: false, autenticado: true, papel: "admin" };
  try { sessao = await api("/api/sessao"); } catch (e) { console.error(e); }
  $("#btn-sair").hidden = !sessao.login_necessario;
  if (sessao.autenticado) await iniciarPainel(sessao);
  else mostrarLogin();
})();
