import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import Config  # noqa: E402
from app.main import criar_app  # noqa: E402


@pytest.fixture
def cfg(tmp_path) -> Config:
    c = Config()
    c.modo = "simulado"
    c.banco_loja = tmp_path / "loja.db"
    c.banco_controle = tmp_path / "controle.db"
    return c


@pytest.fixture
def app(cfg):
    aplicacao = criar_app(cfg)
    # Em teste não há servidor rodando: o agente chama a API da loja pelo
    # TestClient, que percorre exatamente as mesmas rotas HTTP.
    aplicacao.state.agente.loja.http = TestClient(aplicacao, base_url="http://testserver/loja")
    return aplicacao


@pytest.fixture
def cliente(app) -> TestClient:
    return TestClient(app)


def pendentes(cliente: TestClient) -> list[dict]:
    return cliente.get("/api/aprovacoes").json()


def rodar(cliente: TestClient, tarefa: str) -> dict:
    resp = cliente.post("/api/tarefas", json={"tarefa": tarefa})
    assert resp.status_code == 202, resp.text
    return cliente.get(f"/api/execucoes/{resp.json()['execucao_id']}").json()


def decidir(cliente: TestClient, aprovacao_id: int, aprovar: bool, comentario: str = "") -> dict:
    resp = cliente.post(f"/api/aprovacoes/{aprovacao_id}/decisao", json={"aprovar": aprovar, "comentario": comentario})
    assert resp.status_code == 200, resp.text
    return cliente.get(f"/api/execucoes/{resp.json()['execucao_id']}").json()
