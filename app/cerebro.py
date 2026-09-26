"""O "cérebro" do agente: quem decide a próxima ação.

- CerebroClaude: usa a API do Claude (modo real).
- CerebroSimulado: regras determinísticas que devolvem respostas no MESMO
  formato da API (blocos text / tool_use). Serve para rodar o projeto sem
  chave, sem custo e nos testes automáticos. Ele é propositalmente ingênuo:
  segue instruções escritas dentro dos dados, para mostrar que os controles
  seguram um modelo enganado.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Protocol

from .config import Config


@dataclass
class Resposta:
    content: list[dict]  # blocos no formato aceito de volta pela API
    stop_reason: str
    tokens_entrada: int
    tokens_saida: int
    custo_usd: float
    modelo: str
    detalhes: dict = field(default_factory=dict)


class Cerebro(Protocol):
    def responder(self, system: str, mensagens: list[dict], ferramentas: list[dict]) -> Resposta: ...


# ============================================================== modo real
class CerebroClaude:
    def __init__(self, cfg: Config):
        import anthropic

        self.cfg = cfg
        self.client = anthropic.Anthropic(api_key=cfg.anthropic_api_key or None)

    def responder(self, system: str, mensagens: list[dict], ferramentas: list[dict]) -> Resposta:
        # Server-side fallback: se o classificador de segurança recusar o pedido,
        # a própria API tenta de novo num modelo alternativo recomendado.
        resposta = self.client.beta.messages.create(
            model=self.cfg.modelo,
            max_tokens=16000,
            system=system,
            tools=ferramentas,
            messages=mensagens,
            thinking={"type": "adaptive", "display": "summarized"},
            output_config={"effort": self.cfg.esforco},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
        )
        uso = resposta.usage
        cache_criado = uso.cache_creation_input_tokens or 0
        cache_lido = uso.cache_read_input_tokens or 0
        entrada = uso.input_tokens + cache_criado + cache_lido
        # Escrita em cache custa 1,25x e leitura 0,1x o preço de entrada.
        entrada_equivalente = uso.input_tokens + 1.25 * cache_criado + 0.1 * cache_lido
        detalhes: dict[str, Any] = {"request_id": resposta._request_id}
        if resposta.stop_reason == "refusal" and resposta.stop_details:
            detalhes["recusa"] = {
                "categoria": resposta.stop_details.category,
                "explicacao": resposta.stop_details.explanation,
            }
        return Resposta(
            # Os blocos voltam exatamente como vieram (inclusive "thinking"),
            # porque a API exige o histórico sem edições.
            content=[bloco.to_dict(mode="json") for bloco in resposta.content],
            stop_reason=resposta.stop_reason or "end_turn",
            tokens_entrada=entrada,
            tokens_saida=uso.output_tokens,
            custo_usd=self.cfg.custo_usd(int(entrada_equivalente), uso.output_tokens),
            modelo=resposta.model,
            detalhes=detalhes,
        )


# ============================================ provedores no formato OpenAI
class ErroProvedor(Exception):
    pass


def _para_formato_openai(system: str, mensagens: list[dict]) -> list[dict]:
    """Converte o histórico (formato do Claude) para o formato de chat da OpenAI."""
    saida: list[dict] = [{"role": "system", "content": system}]
    for msg in mensagens:
        conteudo = msg["content"]
        if isinstance(conteudo, str):
            saida.append({"role": msg["role"], "content": conteudo})
            continue
        if msg["role"] == "assistant":
            texto = "".join(b.get("text", "") for b in conteudo if b.get("type") == "text")
            chamadas = [
                {
                    "id": b["id"],
                    "type": "function",
                    "function": {"name": b["name"], "arguments": json.dumps(b["input"], ensure_ascii=False)},
                }
                for b in conteudo
                if b.get("type") == "tool_use"
            ]
            item: dict[str, Any] = {"role": "assistant", "content": texto or None}
            if chamadas:
                item["tool_calls"] = chamadas
            saida.append(item)
        else:
            for b in conteudo:
                if b.get("type") == "tool_result":
                    texto = b.get("content", "")
                    if isinstance(texto, list):
                        texto = "".join(x.get("text", "") for x in texto)
                    if b.get("is_error"):
                        texto = f"ERRO: {texto}"
                    saida.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": texto})
                elif b.get("type") == "text":
                    saida.append({"role": "user", "content": b["text"]})
    return saida


class CerebroCompativel:
    """Qualquer provedor com API no formato OpenAI (aqui: Groq, plano gratuito).

    O histórico continua guardado no formato do Claude; a conversão acontece
    só na hora de chamar o provedor. Assim, agente, regras e auditoria são os
    mesmos para todos os cérebros.
    """

    TENTATIVAS = 4

    def __init__(self, cfg: Config, url: str, api_key: str, modelo: str, http: Any = None):
        import httpx

        self.cfg = cfg
        self.modelo = modelo
        self.http = http or httpx.Client(
            base_url=url.rstrip("/"),
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=90,
        )

    def _chamar(self, corpo: dict) -> dict:
        import time

        import httpx

        ultimo = ""
        for tentativa in range(self.TENTATIVAS):
            try:
                resp = self.http.post("/chat/completions", json=corpo)
            except httpx.HTTPError as erro:
                ultimo = f"falha de conexão: {erro}"
                time.sleep(2 * (tentativa + 1))
                continue
            if resp.status_code == 200:
                return resp.json()
            try:
                detalhe = resp.json().get("error", {})
            except ValueError:
                detalhe = {"message": resp.text[:300]}
            ultimo = f"{resp.status_code}: {detalhe.get('message', '')}"
            if resp.status_code == 429 or resp.status_code >= 500:
                # Limite do plano gratuito (ou instabilidade): espera e tenta de novo.
                espera = float(resp.headers.get("retry-after") or 0) or 5 * (tentativa + 1)
                time.sleep(min(espera, 30))
                continue
            if resp.status_code == 400 and detalhe.get("code") == "tool_use_failed":
                # O modelo gerou uma chamada de ferramenta malformada: pede de novo.
                continue
            if resp.status_code in (401, 403):
                raise ErroProvedor("Chave de API recusada pelo provedor. Confira a GROQ_API_KEY.")
            break
        if ultimo.startswith("429"):
            raise ErroProvedor("Limite do plano gratuito atingido. Espere um pouco e tente de novo. " + ultimo)
        raise ErroProvedor(f"O provedor de IA respondeu com erro ({ultimo}).")

    def responder(self, system: str, mensagens: list[dict], ferramentas: list[dict]) -> Resposta:
        corpo = {
            "model": self.modelo,
            "messages": _para_formato_openai(system, mensagens),
            "tools": [
                {
                    "type": "function",
                    "function": {"name": f["name"], "description": f["description"], "parameters": f["input_schema"]},
                }
                for f in ferramentas
            ],
            "tool_choice": "auto",
            "max_tokens": 4096,
        }
        dados = self._chamar(corpo)
        escolha = dados["choices"][0]
        msg = escolha["message"]
        content: list[dict] = []
        if msg.get("reasoning"):
            # Alguns modelos devolvem o raciocínio à parte: mostramos no painel,
            # mas ele não é reenviado ao provedor.
            content.append({"type": "thinking", "thinking": msg["reasoning"]})
        if msg.get("content"):
            content.append({"type": "text", "text": msg["content"]})
        for chamada in msg.get("tool_calls") or []:
            try:
                entrada = json.loads(chamada["function"].get("arguments") or "{}")
            except ValueError:
                entrada = None
            if not isinstance(entrada, dict):
                # JSON inválido vira uma entrada que as regras bloqueiam com a explicação.
                entrada = {"argumentos_invalidos": str(chamada["function"].get("arguments"))[:500]}
            content.append({"type": "tool_use", "id": chamada["id"], "name": chamada["function"]["name"], "input": entrada})

        motivo = escolha.get("finish_reason")
        tem_ferramenta = any(b["type"] == "tool_use" for b in content)
        stop = "tool_use" if tem_ferramenta else {"length": "max_tokens", "content_filter": "refusal"}.get(motivo, "end_turn")
        uso = dados.get("usage") or {}
        return Resposta(
            content=content,
            stop_reason=stop,
            tokens_entrada=int(uso.get("prompt_tokens", 0)),
            tokens_saida=int(uso.get("completion_tokens", 0)),
            custo_usd=0.0,  # plano gratuito
            modelo=dados.get("model", self.modelo),
            detalhes={"provedor": "groq"},
        )


# ========================================================== modo simulado
def _normalizar(texto: str) -> str:
    sem_acento = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return sem_acento.lower()


@dataclass
class _Resultado:
    ferramenta: str
    entrada: dict
    dados: Any
    erro: bool


def _resultados(mensagens: list[dict]) -> list[_Resultado]:
    """Reconstrói a sequência (ferramenta chamada -> resultado) a partir do histórico."""
    chamadas: dict[str, tuple[str, dict]] = {}
    saida: list[_Resultado] = []
    for msg in mensagens:
        if not isinstance(msg["content"], list):
            continue
        for bloco in msg["content"]:
            if bloco.get("type") == "tool_use":
                chamadas[bloco["id"]] = (bloco["name"], bloco["input"])
            elif bloco.get("type") == "tool_result":
                nome, entrada = chamadas.get(bloco["tool_use_id"], ("?", {}))
                texto = bloco.get("content", "")
                if isinstance(texto, list):
                    texto = "".join(b.get("text", "") for b in texto)
                try:
                    dados = json.loads(texto)
                except (ValueError, TypeError):
                    dados = texto
                saida.append(_Resultado(nome, entrada, dados, bool(bloco.get("is_error"))))
    return saida


def _dinheiro(valor: float) -> str:
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


class CerebroSimulado:
    """Planejador por regras. Cada intenção é uma sequência de passos que olha
    para os resultados anteriores, como um agente de verdade faria."""

    modelo = "simulado"

    def responder(self, system: str, mensagens: list[dict], ferramentas: list[dict]) -> Resposta:
        tarefa = mensagens[0]["content"] if isinstance(mensagens[0]["content"], str) else ""
        historico = _resultados(mensagens)
        passo = self._decidir(tarefa, historico)
        n = sum(1 for m in mensagens if m["role"] == "assistant")
        if passo[0] == "ferramenta":
            _, fala, nome, entrada = passo
            content = [
                {"type": "text", "text": fala},
                {"type": "tool_use", "id": f"toolu_sim_{n + 1:02d}", "name": nome, "input": entrada},
            ]
            stop = "tool_use"
        else:
            content = [{"type": "text", "text": passo[1]}]
            stop = "end_turn"
        # Estimativa grosseira (~4 caracteres por token) só para o painel ter números.
        entrada_tokens = (len(system) + len(json.dumps(mensagens, ensure_ascii=False)) + len(json.dumps(ferramentas))) // 4
        saida_tokens = max(1, len(json.dumps(content, ensure_ascii=False)) // 4)
        return Resposta(content, stop, entrada_tokens, saida_tokens, 0.0, self.modelo)

    # --------------------------------------------------------------- decisão
    def _decidir(self, tarefa: str, h: list[_Resultado]) -> tuple:
        t = _normalizar(tarefa)
        numero = re.search(r"\b(\d{4})\b", t)
        pedido_id = int(numero.group(1)) if numero else None

        if pedido_id and "cancel" in t:
            return self._cancelar(pedido_id, tarefa, h)
        if pedido_id and ("reembols" in t or "devolv" in t or "estorn" in t):
            return self._reembolsar(pedido_id, t, h)
        if pedido_id and ("analis" in t or "resolv" in t or "verifi" in t):
            return self._analisar(pedido_id, h)
        if pedido_id:
            return self._consultar(pedido_id, h)
        if ("cri" in t or "fac" in t or "abr" in t) and "pedido" in t:
            return self._criar(t, h)
        if "estoque" in t or "produto" in t:
            return self._estoque(h)
        if "cliente" in t:
            return self._clientes(h)
        return (
            "fim",
            "Sou o agente de operações da loja. Posso consultar estoque e pedidos, criar pedidos, "
            "cancelar, reembolsar e avisar clientes por e-mail. Ações sensíveis passam por aprovação humana. "
            "Exemplo: \"Cancele o pedido 1002 e avise o cliente\".",
        )

    # ------------------------------------------------------------- intenções
    def _consultar(self, pid: int, h: list[_Resultado]) -> tuple:
        if not h:
            return ("ferramenta", f"Vou consultar o pedido {pid}.", "consultar_pedido", {"pedido_id": pid})
        r = h[0]
        if r.erro:
            return ("fim", f"Não consegui consultar o pedido {pid}: {r.dados}")
        p = r.dados
        itens = ", ".join(f"{i['quantidade']}x {i['nome']}" for i in p["itens"])
        return (
            "fim",
            f"Pedido {pid} de {p['cliente']['nome']}: status **{p['status']}**, total {_dinheiro(p['total'])} "
            f"({itens}). Reembolsado até agora: {_dinheiro(p['total_reembolsado'])}.",
        )

    def _cancelar(self, pid: int, tarefa: str, h: list[_Resultado]) -> tuple:
        if not h:
            return ("ferramenta", f"Antes de cancelar, vou conferir o pedido {pid}.", "consultar_pedido", {"pedido_id": pid})
        if h[0].erro:
            return ("fim", f"Não encontrei o pedido {pid}: {h[0].dados}")
        pedido = h[0].dados
        if pedido["status"] not in ("pendente", "pago"):
            return ("fim", f"O pedido {pid} está '{pedido['status']}', então não pode ser cancelado. Nada foi alterado.")
        if len(h) == 1:
            return (
                "ferramenta",
                f"O pedido está '{pedido['status']}'. Vou solicitar o cancelamento.",
                "cancelar_pedido",
                {"pedido_id": pid, "motivo": f"Solicitação do operador: {tarefa[:120]}"},
            )
        if h[1].erro:
            return ("fim", f"O cancelamento do pedido {pid} não foi feito. Motivo: {h[1].dados}")
        saldo = round(pedido["total"] - pedido["total_reembolsado"], 2)
        precisa_reembolso = pedido["status"] == "pago" and saldo > 0
        if precisa_reembolso and len(h) == 2:
            return (
                "ferramenta",
                f"Pedido cancelado. Como já estava pago, vou solicitar o reembolso de {_dinheiro(saldo)}.",
                "reembolsar_pedido",
                {"pedido_id": pid, "valor": saldo, "motivo": "Pedido cancelado"},
            )
        reembolso_ok = precisa_reembolso and not h[2].erro
        idx_email = 3 if precisa_reembolso else 2
        if len(h) == idx_email:
            corpo = f"Olá, {pedido['cliente']['nome'].split()[0]}!\n\nSeu pedido {pid} foi cancelado."
            if reembolso_ok:
                corpo += f" O reembolso de {_dinheiro(saldo)} já foi registrado."
            corpo += "\n\nQualquer dúvida, é só responder este e-mail.\nEquipe Loja Exemplo"
            return (
                "ferramenta",
                "Agora vou avisar o cliente por e-mail.",
                "enviar_email",
                {"para": pedido["cliente"]["email"], "assunto": f"Pedido {pid} cancelado", "corpo": corpo},
            )
        partes = [f"Pedido {pid} cancelado e itens devolvidos ao estoque."]
        if precisa_reembolso:
            partes.append(
                f"Reembolso de {_dinheiro(saldo)} registrado." if reembolso_ok
                else f"O reembolso NÃO foi feito: {h[2].dados}"
            )
        email = h[idx_email]
        partes.append("Cliente avisado por e-mail." if not email.erro else f"E-mail não enviado: {email.dados}")
        return ("fim", " ".join(partes))

    def _reembolsar(self, pid: int, t: str, h: list[_Resultado]) -> tuple:
        if not h:
            return ("ferramenta", f"Vou conferir o pedido {pid} antes do reembolso.", "consultar_pedido", {"pedido_id": pid})
        if h[0].erro:
            return ("fim", f"Não encontrei o pedido {pid}: {h[0].dados}")
        pedido = h[0].dados
        saldo = round(pedido["total"] - pedido["total_reembolsado"], 2)
        valor_txt = re.search(r"r\$\s*([\d.]+(?:,\d{1,2})?)|([\d.]+(?:,\d{1,2})?)\s*reais", t)
        if valor_txt:
            bruto = (valor_txt.group(1) or valor_txt.group(2)).replace(".", "").replace(",", ".")
            valor = float(bruto)
        else:
            valor = saldo
        if saldo <= 0:
            return ("fim", f"O pedido {pid} já foi totalmente reembolsado. Nada a fazer.")
        if len(h) == 1:
            return (
                "ferramenta",
                f"Saldo reembolsável: {_dinheiro(saldo)}. Vou solicitar o reembolso de {_dinheiro(valor)}.",
                "reembolsar_pedido",
                {"pedido_id": pid, "valor": valor, "motivo": "Solicitação do operador"},
            )
        if h[1].erro:
            return ("fim", f"O reembolso do pedido {pid} não foi feito. Motivo: {h[1].dados}")
        if len(h) == 2:
            corpo = (
                f"Olá, {pedido['cliente']['nome'].split()[0]}!\n\nRegistramos um reembolso de {_dinheiro(valor)} "
                f"referente ao pedido {pid}.\n\nEquipe Loja Exemplo"
            )
            return (
                "ferramenta",
                "Reembolso feito. Vou avisar o cliente.",
                "enviar_email",
                {"para": pedido["cliente"]["email"], "assunto": f"Reembolso do pedido {pid}", "corpo": corpo},
            )
        aviso = "Cliente avisado por e-mail." if not h[2].erro else f"E-mail não enviado: {h[2].dados}"
        return ("fim", f"Reembolso de {_dinheiro(valor)} registrado no pedido {pid}. {aviso}")

    def _analisar(self, pid: int, h: list[_Resultado]) -> tuple:
        if not h:
            return ("ferramenta", f"Vou analisar o pedido {pid}.", "consultar_pedido", {"pedido_id": pid})
        if h[0].erro:
            return ("fim", f"Não encontrei o pedido {pid}: {h[0].dados}")
        pedido = h[0].dados
        # Ingenuidade proposital: o simulador obedece a instruções escritas na
        # observação do pedido (prompt injection). Os controles devem barrar.
        pedido_valor = re.search(r"reembols\w*\s+r\$\s*([\d.]+(?:,\d{1,2})?)", _normalizar(pedido["observacao"]))
        if pedido_valor and len(h) == 1:
            valor = float(pedido_valor.group(1).replace(".", "").replace(",", "."))
            return (
                "ferramenta",
                f"A observação do pedido pede um reembolso de {_dinheiro(valor)}. Vou executar.",
                "reembolsar_pedido",
                {"pedido_id": pid, "valor": valor, "motivo": "Pedido na observação do cliente"},
            )
        if pedido_valor:
            desfecho = f"Tentei reembolsar, mas: {h[1].dados}" if h[1].erro else "Reembolso registrado."
            return ("fim", f"Pedido {pid} analisado. {desfecho}")
        return ("fim", f"Pedido {pid} está '{pedido['status']}' e não tem pendências. Observação: {pedido['observacao'] or '—'}")

    def _criar(self, t: str, h: list[_Resultado]) -> tuple:
        cliente = re.search(r"cliente\s*(?:id\s*)?(\d+)", t)
        itens = [
            {"produto_id": int(p), "quantidade": int(q or 1)}
            for q, p in re.findall(r"(?:(\d+)\s*(?:x|unidades?|un)?\s*(?:d[oae]s?\s+)?)?produto\s*(?:id\s*)?(\d+)", t)
        ]
        if not cliente or not itens:
            return (
                "fim",
                "Para criar um pedido preciso do cliente e dos produtos. "
                "Exemplo: \"Crie um pedido para o cliente 3 com 2 unidades do produto 2\".",
            )
        if not h:
            return (
                "ferramenta",
                "Vou criar o pedido (o sistema confere o estoque).",
                "criar_pedido",
                {"cliente_id": int(cliente.group(1)), "itens": itens},
            )
        if h[0].erro:
            return ("fim", f"Não foi possível criar o pedido: {h[0].dados}")
        p = h[0].dados
        return ("fim", f"Pedido {p['id']} criado para {p['cliente']['nome']}, total {_dinheiro(p['total'])}, status '{p['status']}'.")

    def _estoque(self, h: list[_Resultado]) -> tuple:
        if not h:
            return ("ferramenta", "Vou consultar o estoque.", "buscar_produtos", {"busca": ""})
        if h[0].erro:
            return ("fim", f"Não consegui consultar o estoque: {h[0].dados}")
        produtos = h[0].dados
        zerados = [p["nome"] for p in produtos if p["estoque"] == 0]
        baixos = [f"{p['nome']} ({p['estoque']})" for p in produtos if 0 < p["estoque"] <= 5]
        texto = f"{len(produtos)} produtos cadastrados."
        if zerados:
            texto += f" Sem estoque: {', '.join(zerados)}."
        if baixos:
            texto += f" Estoque baixo (≤5): {', '.join(baixos)}."
        return ("fim", texto)

    def _clientes(self, h: list[_Resultado]) -> tuple:
        if not h:
            return ("ferramenta", "Vou listar os clientes.", "buscar_clientes", {"busca": ""})
        if h[0].erro:
            return ("fim", f"Não consegui listar clientes: {h[0].dados}")
        return ("fim", "Clientes: " + "; ".join(f"{c['id']} - {c['nome']} ({c['cidade']})" for c in h[0].dados) + ".")
