"""Modo real sem gastar nada: a API do Claude é substituída por um transporte
HTTP falso. O SDK oficial monta a requisição e interpreta a resposta como faria
de verdade, então isso testa a integração ponta a ponta."""

import json

import httpx2
import pytest
from anthropic import DefaultHttpxClient
from fastapi.testclient import TestClient

from app.cerebro import CerebroClaude
from app.main import criar_app


def _mensagem(content, stop_reason):
    return {
        "id": "msg_teste",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-5",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 1000, "output_tokens": 200, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    }


ROTEIRO = [
    _mensagem(
        [
            {"type": "thinking", "thinking": "Preciso conferir o pedido antes.", "signature": "assinatura-1"},
            {"type": "text", "text": "Vou consultar o pedido 1003."},
            {"type": "tool_use", "id": "toolu_1", "name": "consultar_pedido", "input": {"pedido_id": 1003}},
        ],
        "tool_use",
    ),
    _mensagem(
        [{"type": "tool_use", "id": "toolu_2", "name": "cancelar_pedido", "input": {"pedido_id": 1003, "motivo": "Pedido do operador"}}],
        "tool_use",
    ),
    _mensagem([{"type": "text", "text": "Pedido 1003 cancelado."}], "end_turn"),
]


@pytest.fixture
def api_falsa():
    requisicoes = []

    def responder(request: httpx2.Request) -> httpx2.Response:
        corpo = json.loads(request.content)
        requisicoes.append({"corpo": corpo, "headers": dict(request.headers)})
        return httpx2.Response(200, json=ROTEIRO[len(requisicoes) - 1])

    return requisicoes, DefaultHttpxClient(transport=httpx2.MockTransport(responder))


def test_modo_real_com_api_falsa(cfg, api_falsa):
    requisicoes, http = api_falsa
    cfg.modo = "real"
    cfg.anthropic_api_key = "sk-ant-teste"
    cerebro = CerebroClaude(cfg)
    cerebro.client = cerebro.client.with_options(http_client=http, max_retries=0)

    app = criar_app(cfg, cerebro=cerebro)
    app.state.agente.loja.http = TestClient(app, base_url="http://testserver/loja")
    cliente = TestClient(app)

    eid = cliente.post("/api/tarefas", json={"tarefa": "Cancele o pedido 1003"}).json()["execucao_id"]
    assert cliente.get(f"/api/execucoes/{eid}").json()["status"] == "aguardando_aprovacao"
    [ap] = cliente.get("/api/aprovacoes").json()
    cliente.post(f"/api/aprovacoes/{ap['id']}/decisao", json={"aprovar": True})

    ex = cliente.get(f"/api/execucoes/{eid}").json()
    assert ex["status"] == "concluida"
    assert ex["resposta_final"] == "Pedido 1003 cancelado."
    assert ex["modelo"] == "claude-opus-5"
    assert ex["custo_usd"] == pytest.approx(3 * (1000 * 5 + 200 * 25) / 1_000_000)
    assert app.state.loja.pedido(1003)["status"] == "cancelado"
    assert any(e["tipo"] == "pensamento" for e in ex["eventos"])

    primeira, segunda = requisicoes[0]["corpo"], requisicoes[1]["corpo"]
    assert primeira["model"] == "claude-opus-5"
    assert primeira["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in requisicoes[0]["headers"]["anthropic-beta"]
    assert primeira["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert all(t["strict"] for t in primeira["tools"])
    # O bloco de raciocínio volta intacto (com assinatura) na chamada seguinte.
    assert segunda["messages"][1]["content"][0] == ROTEIRO[0]["content"][0]
    resultado = segunda["messages"][2]["content"][0]
    assert resultado["tool_use_id"] == "toolu_1" and '"status": "pendente"' in resultado["content"]
