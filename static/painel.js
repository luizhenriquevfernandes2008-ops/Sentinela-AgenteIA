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
  inicio: { rotulo: "Tarefa recebida", cor: "var(--info)" },
  modelo: { rotulo: "Chamada ao modelo", cor: "var(--suave)" },
  pensamento: { rotulo: "Raciocínio", cor: "var(--lilas)" },
  texto: { rotulo: "Agente", cor: "var(--info)" },
  chamada: { rotulo: "Quer usar ferramenta", cor: "var(--info)" },
  ferramenta_executada: { rotulo: "Executada", cor: "var(--ok)" },
  erro_ferramenta: { rotulo: "Ferramenta falhou", cor: "var(--perigo)" },
  bloqueio: { rotulo: "Bloqueada pelas regras", cor: "var(--perigo)" },
  aguardando_aprovacao: { rotulo: "Aguardando aprovação", cor: "var(--alerta)" },
  aprovada: { rotulo: "Aprovada por humano", cor: "var(--ok)" },
  rejeitada: { rotulo: "Rejeitada por humano", cor: "var(--perigo)" },
  fallback: { rotulo: "Fallback de modelo", cor: "var(--alerta)" },
  recusa: { rotulo: "Modelo recusou", cor: "var(--perigo)" },
  limite: { rotulo: "Limite atingido", cor: "var(--perigo)" },
  erro: { rotulo: "Erro", cor: "var(--perigo)" },
  cancelada: { rotulo: "Cancelada", cor: "var(--perigo)" },
  fim: { rotulo: "Concluída", cor: "var(--ok)" },
};

const STATUS = {
  executando: ["Executando", "info"],
  aguardando_aprovacao: ["Aguardando aprovação", "alerta"],
  concluida: ["Concluída", "ok"],
  interrompida: ["Interrompida", "perigo"],
  cancelada: ["Cancelada", "perigo"],
  recusada: ["Recusada", "perigo"],
  erro: ["Erro", "perigo"],
};

const RISCO = { leitura: ["leitura", "ok"], escrita: ["escrita", "info"], sensivel: ["sensível", "alerta"] };

const estado = { selecionada: null, controles: null, aba: "controles", status: null };

// ------------------------------------------------------------------ utilidades
const $ = (sel) => document.querySelector(sel);

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
  if (!resp.ok) throw new Error(dados.detail || `Erro ${resp.status}`);
  return dados;
}

const reais = (v) => Number(v).toLocaleString("pt-BR", { style: "currency", currency: "BRL" });
const numero = (v) => Number(v).toLocaleString("pt-BR");
const hora = (iso) => (iso ? iso.slice(11, 19) : "");
const selo = (texto, tipo = "") => `<span class="selo ${tipo}">${esc(texto)}</span>`;
const seloStatus = (s) => selo(...(STATUS[s] || [s, ""]));

function json(valor) {
  if (typeof valor === "string") {
    try { valor = JSON.parse(valor); } catch { return esc(valor); }
  }
  return esc(JSON.stringify(valor, null, 2));
}

// --------------------------------------------------------------------- topo
async function atualizarTopo() {
  const s = await api("/api/status");
  estado.status = s;
  $("#modo").className = "selo " + (s.modo === "real" ? "info" : "");
  $("#modo").textContent = s.modo === "real" ? `Modo real · ${s.modelo}` : "Modo simulado · sem custo";
  $("#estado-agente").className = "selo " + (s.agente_ativo ? "ok" : "perigo");
  $("#estado-agente").textContent = s.agente_ativo ? "Agente ativo" : "Agente pausado";
  const btn = $("#btn-pausa");
  btn.textContent = s.agente_ativo ? "Pausar agente" : "Reativar agente";
  btn.className = "botao " + (s.agente_ativo ? "perigo" : "sucesso");
}

$("#btn-pausa").addEventListener("click", async () => {
  await api("/api/controles", { method: "PUT", body: { agente_ativo: !estado.status.agente_ativo } });
  await atualizarTudo();
});

// ----------------------------------------------------------------- métricas
async function atualizarMetricas() {
  const [m, c] = await Promise.all([api("/api/metricas"), api("/api/controles")]);
  const uso = c.orcamento_tokens_dia ? Math.min(100, (100 * m.tokens_hoje) / c.orcamento_tokens_dia) : 100;
  const custo = `US$ ${m.custo_usd.toFixed(m.modo === "real" ? 4 : 0)}`;
  const cartoes = [
    ["Execuções", numero(m.execucoes)],
    ["Ações executadas", numero(m.acoes_executadas)],
    ["Ações bloqueadas", numero(m.acoes_bloqueadas)],
    ["Aprovações pendentes", numero(m.aprovacoes_pendentes), m.aprovacoes_pendentes > 0],
    ["Aprovadas / rejeitadas", `${m.aprovadas} / ${m.rejeitadas}`],
    ["Tokens hoje", numero(m.tokens_hoje), false, uso],
    [m.modo === "real" ? "Custo total" : "Custo (simulado)", custo],
  ];
  $("#metricas").innerHTML = cartoes
    .map(([rotulo, valor, destaque, barra]) => `
      <div class="metrica ${destaque ? "destaque" : ""}">
        <div class="rotulo">${esc(rotulo)}</div>
        <div class="valor">${esc(valor)}</div>
        ${barra !== undefined ? `<div class="barra" title="${barra.toFixed(0)}% do orçamento diário"><span style="width:${barra}%"></span></div>` : ""}
      </div>`)
    .join("");
}

// -------------------------------------------------------------- nova tarefa
$("#exemplos").innerHTML = EXEMPLOS.map((e) => `<button type="button" class="exemplo">${esc(e)}</button>`).join("");
$("#exemplos").addEventListener("click", (ev) => {
  if (ev.target.classList.contains("exemplo")) $("#tarefa").value = ev.target.textContent;
});

$("#form-tarefa").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const botao = ev.submitter;
  const erro = $("#erro-tarefa");
  erro.hidden = true;
  botao.disabled = true;
  try {
    const { execucao_id } = await api("/api/tarefas", { method: "POST", body: { tarefa: $("#tarefa").value } });
    $("#tarefa").value = "";
    estado.selecionada = execucao_id;
    await atualizarTudo();
  } catch (e) {
    erro.textContent = e.message;
    erro.hidden = false;
  } finally {
    botao.disabled = false;
  }
});

// ---------------------------------------------------------------- aprovações
async function atualizarAprovacoes() {
  const lista = await api("/api/aprovacoes");
  $("#qtd-aprovacoes").textContent = lista.length;
  $("#cartao-aprovacoes").classList.toggle("tem", lista.length > 0);
  const alvo = $("#aprovacoes");
  // Não redesenhar enquanto a pessoa digita um comentário.
  if (alvo.contains(document.activeElement) && document.activeElement.tagName === "INPUT") return;
  const assinatura = lista.map((a) => a.id).join(",");
  if (alvo.dataset.assinatura === assinatura) return;
  alvo.dataset.assinatura = assinatura;
  alvo.innerHTML = lista.length
    ? lista.map((a) => `
      <div class="aprovacao" data-id="${a.id}">
        <div class="cab"><strong>${esc(a.ferramenta)}</strong>${selo(`execução #${a.execucao_id}`)}</div>
        <div class="tarefa">Tarefa: ${esc(a.tarefa)}</div>
        <pre>${json(a.entrada)}</pre>
        <div class="motivo">${esc(a.motivo)}</div>
        <div class="acoes">
          <input type="text" placeholder="Comentário (opcional, vai para o agente)" maxlength="500">
          <button class="botao sucesso" data-decisao="1">Aprovar</button>
          <button class="botao perigo" data-decisao="0">Rejeitar</button>
        </div>
      </div>`).join("")
    : '<p class="vazio">Nada esperando por você. Ações sensíveis aparecem aqui antes de acontecer.</p>';
}

$("#aprovacoes").addEventListener("click", async (ev) => {
  const botao = ev.target.closest("[data-decisao]");
  if (!botao) return;
  const cartao = botao.closest(".aprovacao");
  cartao.querySelectorAll("button").forEach((b) => (b.disabled = true));
  try {
    const r = await api(`/api/aprovacoes/${cartao.dataset.id}/decisao`, {
      method: "POST",
      body: { aprovar: botao.dataset.decisao === "1", comentario: cartao.querySelector("input").value },
    });
    estado.selecionada = r.execucao_id;
  } catch (e) {
    alert(e.message);
  }
  $("#aprovacoes").dataset.assinatura = "";
  await atualizarTudo();
});

// ----------------------------------------------------------------- execuções
async function atualizarExecucoes() {
  const lista = await api("/api/execucoes");
  $("#execucoes").innerHTML = lista.length
    ? lista.map((e) => `
      <button class="item-exec ${e.id === estado.selecionada ? "selecionado" : ""}" data-id="${e.id}">
        <span class="t">#${e.id} · ${esc(e.tarefa)}</span>${seloStatus(e.status)}
        <span class="m">${e.passos} passo(s) · ${numero(e.tokens_entrada + e.tokens_saida)} tokens${e.modo === "real" ? ` · US$ ${e.custo_usd.toFixed(4)}` : ""} · ${hora(e.criado_em)}</span>
      </button>`).join("")
    : '<p class="vazio">Nenhuma execução ainda. Envie uma tarefa acima.</p>';
}

$("#execucoes").addEventListener("click", (ev) => {
  const item = ev.target.closest(".item-exec");
  if (!item) return;
  estado.selecionada = Number(item.dataset.id);
  atualizarTudo();
});

function corpoEvento(e) {
  const d = e.dados || {};
  switch (e.tipo) {
    case "inicio": return `<div class="corpo">${esc(d.tarefa)}</div>`;
    case "texto": return `<div class="corpo">${esc(d.texto)}</div>`;
    case "pensamento": return `<div class="corpo suave">${esc(d.texto)}</div>`;
    case "modelo":
      return `<div class="corpo suave">${esc(d.modelo)} · ${numero(d.tokens_entrada)} entrada / ${numero(d.tokens_saida)} saída${d.custo_usd ? ` · US$ ${d.custo_usd.toFixed(5)}` : ""}</div>`;
    case "chamada":
      return `<div class="corpo"><code>${esc(d.ferramenta)}</code></div><pre>${json(d.entrada)}</pre>`;
    case "ferramenta_executada":
      return `<div class="corpo"><code>${esc(d.ferramenta)}</code> → resultado</div><pre>${json(d.resultado)}</pre>`;
    case "bloqueio":
    case "aguardando_aprovacao":
      return `<div class="corpo"><code>${esc(d.ferramenta)}</code>: ${esc(d.motivo)}</div>`;
    case "aprovada":
    case "rejeitada":
      return `<div class="corpo"><code>${esc(d.ferramenta)}</code>${d.comentario ? ` · “${esc(d.comentario)}”` : ""}</div>`;
    case "erro_ferramenta":
      return `<div class="corpo"><code>${esc(d.ferramenta)}</code>: ${esc(d.erro)}</div>`;
    case "erro": return `<div class="corpo">${esc(d.erro || e.titulo)}</div>`;
    case "recusa": return `<div class="corpo">${esc(d?.explicacao || e.titulo)}</div>`;
    case "fim": return "";
    default: return `<div class="corpo">${esc(e.titulo)}</div>`;
  }
}

async function atualizarDetalhe() {
  if (!estado.selecionada) return;
  const ex = await api(`/api/execucoes/${estado.selecionada}`);
  $("#titulo-detalhe").textContent = `Execução #${ex.id}`;
  $("#btn-cancelar-exec").hidden = !["executando", "aguardando_aprovacao"].includes(ex.status);
  const final = ex.status === "concluida" && ex.resposta_final
    ? `<div class="resposta-final">${esc(ex.resposta_final)}</div>`
    : "";
  $("#detalhe").innerHTML = `
    <div class="resumo-exec">
      ${seloStatus(ex.status)}
      ${selo(`${ex.passos} passo(s)`)}
      ${selo(`${numero(ex.tokens_entrada + ex.tokens_saida)} tokens`)}
      ${ex.modo === "real" ? selo(`US$ ${ex.custo_usd.toFixed(4)}`) : selo("simulado")}
    </div>
    <p class="vazio" style="margin-bottom:12px">“${esc(ex.tarefa)}”</p>
    ${final}
    <ol class="linha-tempo">
      ${ex.eventos.map((e) => {
        const info = EVENTOS[e.tipo] || { rotulo: e.tipo, cor: "var(--suave)" };
        return `<li class="evento" style="--cor:${info.cor}">
          <div class="cab"><strong>${esc(info.rotulo)}</strong><span class="hora">${hora(e.criado_em)}</span></div>
          ${corpoEvento(e)}
        </li>`;
      }).join("")}
    </ol>`;
}

$("#btn-cancelar-exec").addEventListener("click", async () => {
  if (!confirm("Cancelar esta execução? Ações pendentes serão rejeitadas.")) return;
  try {
    await api(`/api/execucoes/${estado.selecionada}/cancelar`, { method: "POST" });
  } catch (e) {
    alert(e.message);
  }
  await atualizarTudo();
});

// ----------------------------------------------------------------- controles
async function carregarControles() {
  const c = await api("/api/controles");
  estado.controles = c;
  const riscos = estado.status?.ferramentas || {};
  $("#tabela-ferramentas tbody").innerHTML = Object.entries(c.ferramentas)
    .map(([nome, conf]) => {
      const [rotulo, tipo] = RISCO[riscos[nome]?.risco] || ["?", ""];
      return `<tr>
        <td><code>${esc(nome)}</code></td>
        <td>${selo(rotulo, tipo)}</td>
        <td><input type="checkbox" data-ferramenta="${esc(nome)}" data-campo="ativa" ${conf.ativa ? "checked" : ""} aria-label="${esc(nome)} ativa"></td>
        <td><input type="checkbox" data-ferramenta="${esc(nome)}" data-campo="exige_aprovacao" ${conf.exige_aprovacao ? "checked" : ""} aria-label="${esc(nome)} exige aprovação"></td>
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

// ---------------------------------------------------------------------- loja
async function atualizarLoja() {
  const l = await api("/api/loja");
  $("#tabela-pedidos").innerHTML = `
    <thead><tr><th>#</th><th>Cliente</th><th>Status</th><th>Itens</th><th class="num">Total</th><th class="num">Reembolsado</th><th>Observação</th></tr></thead>
    <tbody>${l.pedidos.map((p) => `<tr>
      <td>${p.id}</td><td>${esc(p.cliente.nome)}</td><td>${esc(p.status)}</td>
      <td>${p.itens.map((i) => `${i.quantidade}× ${esc(i.nome)}`).join("<br>")}</td>
      <td class="num">${reais(p.total)}</td><td class="num">${reais(p.total_reembolsado)}</td>
      <td>${esc(p.observacao)}</td></tr>`).join("")}</tbody>`;
  $("#tabela-produtos").innerHTML = `
    <thead><tr><th>#</th><th>Produto</th><th class="num">Preço</th><th class="num">Estoque</th></tr></thead>
    <tbody>${l.produtos.map((p) => `<tr><td>${p.id}</td><td>${esc(p.nome)}</td><td class="num">${reais(p.preco)}</td><td class="num">${p.estoque}</td></tr>`).join("")}</tbody>`;
  $("#lista-emails").innerHTML = l.emails.length
    ? l.emails.map((e) => `<div class="email"><div class="de">Para ${esc(e.para)} · ${hora(e.enviado_em)}</div><strong>${esc(e.assunto)}</strong><pre>${esc(e.corpo)}</pre></div>`).join("")
    : '<p class="vazio">Nenhum e-mail enviado.</p>';
  $("#tabela-reembolsos").innerHTML = l.reembolsos.length
    ? `<thead><tr><th>Pedido</th><th class="num">Valor</th><th>Motivo</th></tr></thead>
       <tbody>${l.reembolsos.map((r) => `<tr><td>${r.pedido_id}</td><td class="num">${reais(r.valor)}</td><td>${esc(r.motivo)}</td></tr>`).join("")}</tbody>`
    : '<tbody><tr><td class="vazio">Nenhum reembolso.</td></tr></tbody>';
}

$("#btn-resetar-loja").addEventListener("click", async () => {
  if (!confirm("Restaurar pedidos, estoque e e-mails para os dados de exemplo?")) return;
  await api("/api/loja/resetar", { method: "POST" });
  await atualizarLoja();
});

// ---------------------------------------------------------------------- abas
document.querySelectorAll(".aba").forEach((aba) =>
  aba.addEventListener("click", () => {
    estado.aba = aba.dataset.aba;
    document.querySelectorAll(".aba").forEach((a) => a.classList.toggle("ativa", a === aba));
    $("#aba-controles").hidden = estado.aba !== "controles";
    $("#aba-loja").hidden = estado.aba !== "loja";
    if (estado.aba === "controles") carregarControles();
    else atualizarLoja();
  })
);

// ------------------------------------------------------------------- ciclo
async function atualizarTudo() {
  try {
    await atualizarTopo();
    await Promise.all([atualizarMetricas(), atualizarAprovacoes(), atualizarExecucoes(), atualizarDetalhe()]);
    if (estado.aba === "loja") await atualizarLoja();
  } catch (e) {
    console.error(e);
  }
}

(async () => {
  await atualizarTudo();
  await carregarControles();
  setInterval(atualizarTudo, 1500);
})();
