"""Modo Groq sem internet: a API do Groq é trocada por um transporte HTTP falso."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import cerebro as modulo_cerebro
from app.cerebro import CerebroCompativel
from app.config import Config
from app.main import criar_app


def _resposta(mensagem, finish="tool_calls", modelo="openai/gpt-oss-120b"):
    return {
        "id": "chatcmpl-teste",
        "model": modelo,
        "choices": [{"index": 0, "message": {"role": "assistant", **mensagem}, "finish_reason": finish}],
        "usage": {"prompt_tokens": 900, "completion_tokens": 120},
    }


def _chamada(ident, nome, argumentos):
    texto = argumentos if isinstance(argumentos, str) else json.dumps(argumentos)
    return {"id": ident, "type": "function", "function": {"name": nome, "arguments": texto}}


@pytest.fixture(autouse=True)
def sem_espera(monkeypatch):
    # As esperas entre tentativas não precisam acontecer de verdade nos testes.
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)


def _montar(cfg, roteiro):
    """Sobe o app com o Groq falso. `roteiro` é uma lista de httpx.Response."""
    enviados = []

    def responder(request):
        enviados.append({"corpo": json.loads(request.content), "headers": dict(request.headers)})
        return roteiro[len(enviados) - 1]

    cfg.modo = "groq"
    cfg.groq_api_key = "gsk_teste"
    http = httpx.Client(
        base_url="https://groq.falso/openai/v1",
        headers={"Authorization": "Bearer gsk_teste"},
        transport=httpx.MockTransport(responder),
    )
    cerebro = CerebroCompativel(cfg, cfg.groq_url, cfg.groq_api_key, cfg.groq_modelo, http=http)
    app = criar_app(cfg, cerebro=cerebro)
    app.state.agente.loja.http = TestClient(app, base_url="http://testserver/loja")
    return app, TestClient(app), enviados


def test_fluxo_completo_com_groq(cfg):
    roteiro = [
        httpx.Response(200, json=_resposta({
            "content": "Vou conferir o pedido.",
            "reasoning": "Preciso ver o status antes de cancelar.",
            "tool_calls": [_chamada("call_1", "consultar_pedido", {"pedido_id": 1003})],
        })),
        httpx.Response(200, json=_resposta({
            "content": None,
            "tool_calls": [_chamada("call_2", "cancelar_pedido", {"pedido_id": 1003, "motivo": "Pedido do operador"})],
        })),
        httpx.Response(200, json=_resposta({"content": "Pedido 1003 cancelado."}, finish="stop")),
    ]
    app, cliente, enviados = _montar(cfg, roteiro)

    status = cliente.get("/api/status").json()
    assert status["provedor"] == "Groq" and status["gratuito"] is True
    assert status["modelo"] == "openai/gpt-oss-120b"

    eid = cliente.post("/api/tarefas", json={"tarefa": "Cancele o pedido 1003"}).json()["execucao_id"]
    [ap] = cliente.get("/api/aprovacoes").json()
    assert ap["ferramenta"] == "cancelar_pedido"  # a IA continua passando pelas regras
    cliente.post(f"/api/aprovacoes/{ap['id']}/decisao", json={"aprovar": True})

    ex = cliente.get(f"/api/execucoes/{eid}").json()
    assert ex["status"] == "concluida"
    assert ex["resposta_final"] == "Pedido 1003 cancelado."
    assert ex["custo_usd"] == 0
    assert ex["tokens_entrada"] == 3 * 900
    assert any(e["tipo"] == "pensamento" for e in ex["eventos"])
    assert app.state.loja.pedido(1003)["status"] == "cancelado"

    primeira, segunda, terceira = (e["corpo"] for e in enviados)
    assert enviados[0]["headers"]["authorization"] == "Bearer gsk_teste"
    assert primeira["model"] == "openai/gpt-oss-120b"
    assert primeira["messages"][0]["role"] == "system"
    assert primeira["messages"][1] == {"role": "user", "content": "Cancele o pedido 1003"}
    assert {t["function"]["name"] for t in primeira["tools"]} >= {"consultar_pedido", "cancelar_pedido"}
    # O raciocínio não é reenviado; a chamada e o resultado seguem o formato OpenAI.
    assistente, ferramenta = segunda["messages"][2], segunda["messages"][3]
    assert assistente["tool_calls"][0]["id"] == "call_1"
    assert ferramenta["role"] == "tool" and ferramenta["tool_call_id"] == "call_1"
    assert '"status": "pendente"' in ferramenta["content"]
    assert terceira["messages"][-1]["tool_call_id"] == "call_2"


def test_espera_e_tenta_de_novo_no_limite_do_plano_gratis(cfg):
    roteiro = [
        httpx.Response(429, headers={"retry-after": "2"}, json={"error": {"message": "Rate limit reached"}}),
        httpx.Response(200, json=_resposta({"content": "Tudo certo."}, finish="stop")),
    ]
    _, cliente, enviados = _montar(cfg, roteiro)
    eid = cliente.post("/api/tarefas", json={"tarefa": "Oi"}).json()["execucao_id"]
    assert cliente.get(f"/api/execucoes/{eid}").json()["status"] == "concluida"
    assert len(enviados) == 2


def test_limite_persistente_vira_erro_explicado(cfg):
    roteiro = [httpx.Response(429, json={"error": {"message": "Rate limit reached"}})] * 4
    _, cliente, _ = _montar(cfg, roteiro)
    eid = cliente.post("/api/tarefas", json={"tarefa": "Oi"}).json()["execucao_id"]
    ex = cliente.get(f"/api/execucoes/{eid}").json()
    assert ex["status"] == "erro"
    assert "plano gratuito" in json.dumps(ex["eventos"], ensure_ascii=False)


def test_chave_recusada(cfg):
    _, cliente, _ = _montar(cfg, [httpx.Response(401, json={"error": {"message": "Invalid API Key"}})])
    eid = cliente.post("/api/tarefas", json={"tarefa": "Oi"}).json()["execucao_id"]
    ex = cliente.get(f"/api/execucoes/{eid}").json()
    assert ex["status"] == "erro"
    assert "GROQ_API_KEY" in json.dumps(ex["eventos"], ensure_ascii=False)


def test_json_quebrado_da_ia_e_barrado_pelas_regras(cfg):
    roteiro = [
        httpx.Response(200, json=_resposta({"tool_calls": [_chamada("call_1", "reembolsar_pedido", "{valor: muito")]})),
        httpx.Response(200, json=_resposta({"content": "Não consegui."}, finish="stop")),
    ]
    app, cliente, enviados = _montar(cfg, roteiro)
    eid = cliente.post("/api/tarefas", json={"tarefa": "Reembolse o pedido 1002"}).json()["execucao_id"]
    ex = cliente.get(f"/api/execucoes/{eid}").json()
    assert "bloqueio" in [e["tipo"] for e in ex["eventos"]]
    assert enviados[1]["corpo"]["messages"][-1]["content"].startswith("ERRO:")
    assert app.state.loja.listar_reembolsos() == []


def test_interruptor_sem_chave_fica_no_simulado(monkeypatch):
    monkeypatch.setenv("MODO_AGENTE", "groq")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cfg = Config.do_ambiente()
    assert cfg.modo == "simulado"
    assert "GROQ_API_KEY" in cfg.aviso_modo
    monkeypatch.setenv("GROQ_API_KEY", "gsk_x")
    assert Config.do_ambiente().modo == "groq"


def test_teto_zero_nao_deixa_gastar_com_claude(cfg):
    """LIMITE_USD_DIA=0: com um provedor pago, nenhuma chamada sai."""

    class ClaudeQueNaoPodeSerChamado:
        def responder(self, *a, **k):
            raise AssertionError("não devia chamar um provedor pago")

    cfg.modo = "claude"
    cfg.anthropic_api_key = "sk-ant-teste"
    cfg.limite_usd_dia = 0
    app = criar_app(cfg, cerebro=ClaudeQueNaoPodeSerChamado())
    cliente = TestClient(app)
    eid = cliente.post("/api/tarefas", json={"tarefa": "Oi"}).json()["execucao_id"]
    ex = cliente.get(f"/api/execucoes/{eid}").json()
    assert ex["status"] == "interrompida"
    assert "teto de gasto" in ex["resposta_final"]


def test_conversao_de_historico():
    mensagens = [
        {"role": "user", "content": "tarefa"},
        {"role": "assistant", "content": [
            {"type": "thinking", "thinking": "pensando"},
            {"type": "text", "text": "ok"},
            {"type": "tool_use", "id": "a", "name": "buscar_produtos", "input": {"busca": ""}},
        ]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "[]", "is_error": True}]},
    ]
    saida = modulo_cerebro._para_formato_openai("sistema", mensagens)
    assert saida[0] == {"role": "system", "content": "sistema"}
    assert saida[2]["content"] == "ok" and "pensando" not in json.dumps(saida)
    assert saida[3] == {"role": "tool", "tool_call_id": "a", "content": "ERRO: []"}
