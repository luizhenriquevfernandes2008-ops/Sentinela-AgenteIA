"""Ligar a IA pela aba Controles: colar a chave do Groq no painel, sem mexer no servidor."""

import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app.main import criar_app

CHAVE_BOA = "gsk_" + "A1b2C3d4" * 6
CHAVE_RUIM = "gsk_" + "X" * 48


@pytest.fixture(autouse=True)
def sem_espera(monkeypatch):
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)


class GroqFalso:
    """Responde /models (validação da chave) e /chat/completions (tarefas)."""

    def __init__(self):
        self.pedidos = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        chave = request.headers.get("authorization", "").removeprefix("Bearer ")
        self.pedidos.append(request.url.path)
        if chave != CHAVE_BOA:
            return httpx.Response(401, json={"error": {"message": "Invalid API Key"}})
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "openai/gpt-oss-120b"}]})
        return httpx.Response(200, json={
            "model": "openai/gpt-oss-120b",
            "choices": [{"message": {"role": "assistant", "content": "Olá! Estoque conferido."}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        })


@pytest.fixture
def groq(monkeypatch):
    falso = GroqFalso()
    original = httpx.Client.__init__

    def init(self, *args, **kwargs):
        # Só o cliente do Groq (base_url do Groq) passa pelo transporte falso.
        if "groq.com" in str(kwargs.get("base_url", "")):
            kwargs["transport"] = httpx.MockTransport(falso)
        original(self, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "__init__", init)
    return falso


@pytest.fixture
def painel(cfg, groq):
    app = criar_app(cfg)
    app.state.agente.loja.http = TestClient(app, base_url="http://testserver/loja")
    return app, TestClient(app)


def test_ligar_ia_colando_a_chave(painel, groq):
    app, c = painel
    assert c.get("/api/ia").json()["modo"] == "simulado"

    resp = c.put("/api/ia", json={"modo": "groq", "chave": CHAVE_BOA})
    assert resp.status_code == 200, resp.text
    dados = resp.json()
    assert dados["modo"] == "groq" and dados["provedor"] == "Groq"
    assert dados["chave_final"] == CHAVE_BOA[-4:]
    assert CHAVE_BOA not in json.dumps(dados)  # a chave inteira nunca volta
    assert c.get("/api/status").json()["provedor"] == "Groq"

    eid = c.post("/api/tarefas", json={"tarefa": "Oi"}).json()["execucao_id"]
    ex = c.get(f"/api/execucoes/{eid}").json()
    assert ex["status"] == "concluida" and ex["resposta_final"] == "Olá! Estoque conferido."
    assert ex["modelo"] == "openai/gpt-oss-120b"


def test_chave_recusada_nao_liga(painel):
    _, c = painel
    resp = c.put("/api/ia", json={"modo": "groq", "chave": CHAVE_RUIM})
    assert resp.status_code == 400 and "recusou" in resp.json()["detail"]
    assert c.get("/api/ia").json()["modo"] == "simulado"


@pytest.mark.parametrize("chave", ["", "sk-ant-123", "gsk_curta", "gsk_" + "a" * 40 + " ; rm -rf"])
def test_formato_de_chave_invalido(painel, chave):
    _, c = painel
    assert c.put("/api/ia", json={"modo": "groq", "chave": chave}).status_code == 400


def test_desligar_e_religar_sem_colar_de_novo(painel):
    _, c = painel
    c.put("/api/ia", json={"modo": "groq", "chave": CHAVE_BOA})
    assert c.put("/api/ia", json={"modo": "simulado"}).json()["modo"] == "simulado"
    assert c.get("/api/ia").json()["chave_configurada"] is True
    assert c.put("/api/ia", json={"modo": "groq"}).json()["modo"] == "groq"


def test_remover_chave_volta_ao_simulado(painel):
    _, c = painel
    c.put("/api/ia", json={"modo": "groq", "chave": CHAVE_BOA})
    dados = c.delete("/api/ia/chave", headers={"content-type": "application/json"}).json()
    assert dados["modo"] == "simulado" and dados["chave_configurada"] is False


def test_escolha_sobrevive_a_reinicio_e_ao_restaurar_demo(cfg, groq):
    app = criar_app(cfg)
    c = TestClient(app)
    c.put("/api/ia", json={"modo": "groq", "chave": CHAVE_BOA})
    c.post("/api/demo/resetar", json={})
    assert c.get("/api/ia").json()["modo"] == "groq"
    # "Reinicia" o servidor com o mesmo banco.
    assert TestClient(criar_app(cfg)).get("/api/ia").json()["modo"] == "groq"
