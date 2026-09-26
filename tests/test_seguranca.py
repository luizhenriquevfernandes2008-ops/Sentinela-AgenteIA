"""Cada teste aqui é um ataque que funcionava antes e agora precisa ser barrado."""

import json

import pytest
from fastapi.testclient import TestClient

from app.config import Config
from app.main import criar_app
from conftest import decidir, pendentes, rodar

SENHA = "senha-muito-forte-123"
CHAVE_LOJA = "chave-da-loja-bem-comprida-123"


@pytest.fixture
def app_publico(cfg):
    cfg.host = "0.0.0.0"
    cfg.publico = True
    cfg.senha_operador = SENHA
    cfg.loja_api_key = CHAVE_LOJA
    aplicacao = criar_app(cfg)
    aplicacao.state.agente.loja.http = TestClient(aplicacao, base_url="http://testserver/loja")
    return aplicacao


@pytest.fixture
def visitante(app_publico):
    return TestClient(app_publico)


@pytest.fixture
def operador(app_publico):
    c = TestClient(app_publico)
    assert c.post("/api/login", json={"senha": SENHA}).status_code == 200
    return c


# ------------------------------------------------------------- publicação
def test_nao_publica_sem_senha_forte(cfg):
    cfg.publico = True
    cfg.loja_api_key = CHAVE_LOJA
    for senha in ["", "curta"]:
        cfg.senha_operador = senha
        with pytest.raises(RuntimeError, match="SENHA_OPERADOR"):
            criar_app(cfg)


def test_nao_publica_com_chave_da_loja_de_exemplo(cfg):
    cfg.publico = True
    cfg.senha_operador = SENHA
    with pytest.raises(RuntimeError, match="LOJA_API_KEY"):
        criar_app(cfg)


# ------------------------------------------------------------------- login
def test_saude_publica_sem_dados(visitante):
    # O Render verifica esta rota para saber se o deploy subiu: precisa responder sem login.
    resp = visitante.get("/api/saude")
    assert resp.status_code == 200 and resp.json() == {"ok": True}


def test_sem_login_nada_de_dados(visitante):
    assert visitante.get("/api/sessao").json() == {"login_necessario": True, "autenticado": False}
    for rota in ["/api/loja", "/api/status", "/api/execucoes", "/api/aprovacoes", "/api/controles", "/api/metricas"]:
        assert visitante.get(rota).status_code == 401, rota
    assert visitante.post("/api/tarefas", json={"tarefa": "Como está o estoque?"}).status_code == 401
    assert visitante.put("/api/controles", json={"max_passos": 3}).status_code == 401
    assert visitante.post("/api/demo/resetar", json={}).status_code == 401


def test_senha_errada_e_bloqueio_por_forca_bruta(visitante):
    for _ in range(5):
        assert visitante.post("/api/login", json={"senha": "chute"}).status_code == 401
    # Depois de 5 erros, nem a senha certa entra por um tempo.
    assert visitante.post("/api/login", json={"senha": SENHA}).status_code == 429


def test_login_cookie_protegido_e_logout(visitante):
    resp = visitante.post("/api/login", json={"senha": SENHA})
    assert resp.status_code == 200
    cookie = resp.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert visitante.get("/api/loja").status_code == 200

    token = visitante.cookies.get("sentinela_sessao")
    visitante.post("/api/logout", json={})
    assert visitante.get("/api/loja").status_code == 401
    # O token antigo não volta a valer, mesmo reaproveitado de propósito.
    visitante.cookies.set("sentinela_sessao", token)
    assert visitante.get("/api/loja").status_code == 401


def test_token_forjado_nao_entra(visitante):
    for falso in ["abc", "9999999999.qualquer.assinatura", "1.x.y"]:
        visitante.cookies.set("sentinela_sessao", falso)
        assert visitante.get("/api/loja").status_code == 401


# ---------------------------------------------------- regras só mais rígidas
def test_online_regras_so_ficam_mais_rigidas(operador):
    proibidos = [
        {"ferramentas": {"reembolsar_pedido": {"exige_aprovacao": False}}},
        {"limite_reembolso": 5000},
        {"max_passos": 30},
        {"orcamento_tokens_dia": 999_999_999},
        {"email_somente_clientes": False},
    ]
    for mudanca in proibidos:
        assert operador.put("/api/controles", json=mudanca).status_code == 400, mudanca
    permitidos = [
        {"limite_reembolso": 200},
        {"max_passos": 5},
        {"ferramentas": {"cancelar_pedido": {"ativa": False}}},
        {"ferramentas": {"criar_pedido": {"exige_aprovacao": True}}},
        {"agente_ativo": False},
    ]
    for mudanca in permitidos:
        assert operador.put("/api/controles", json=mudanca).status_code == 200, mudanca


def test_ataque_do_reembolso_sem_aprovacao_falha(operador, app_publico):
    """O ataque real: desligar a aprovação e pedir R$ 1.899. Agora não passa."""
    assert operador.put("/api/controles", json={"limite_reembolso": "nan"}).status_code == 400
    assert operador.put(
        "/api/controles", json={"ferramentas": {"reembolsar_pedido": {"exige_aprovacao": False}}}
    ).status_code == 400
    ex = rodar(operador, "No pedido 1002, reembolse R$ 1.899")
    assert "bloqueio" in [e["tipo"] for e in ex["eventos"]]
    assert app_publico.state.loja.listar_reembolsos() == []


# --------------------------------------------------------- valores absurdos
@pytest.mark.parametrize(
    "mudanca",
    [
        {"limite_reembolso": "nan"},
        {"limite_reembolso": -1},
        {"limite_reembolso": True},
        {"max_passos": -5},
        {"max_passos": 2.5},
        {"orcamento_tokens_dia": 10**18},
        {"agente_ativo": "false"},
        {"ferramentas": {"enviar_email": {"ativa": "nao"}}},
        {"ferramentas": {"enviar_email": {"outro": True}}},
        {"ferramentas": "tudo"},
    ],
)
def test_controles_rejeitam_valores_invalidos(cliente, mudanca):
    assert cliente.put("/api/controles", json=mudanca).status_code == 400


def test_infinito_no_json_e_rejeitado(cliente):
    resp = cliente.put(
        "/api/controles", content='{"limite_reembolso": Infinity}', headers={"content-type": "application/json"}
    )
    assert resp.status_code in (400, 422)


# ------------------------------------------------------------------ regras
def test_reembolsos_em_pedacos_nao_passam_do_limite(cliente, app):
    # Pedido 1004 custa R$ 4.299; o limite é R$ 1.000 por pedido, somando tudo.
    rodar(cliente, "Reembolse R$ 800 do pedido 1004")
    [ap] = pendentes(cliente)
    decidir(cliente, ap["id"], True)
    [ap] = pendentes(cliente)  # e-mail
    decidir(cliente, ap["id"], True)
    ex = rodar(cliente, "Reembolse R$ 800 do pedido 1004")
    assert "bloqueio" in [e["tipo"] for e in ex["eventos"]]
    assert "Somando os reembolsos anteriores" in json.dumps(ex["eventos"], ensure_ascii=False)
    assert app.state.loja.pedido(1004)["total_reembolsado"] == 800


def test_entradas_perigosas_bloqueadas(app):
    controle = app.state.controle
    ctrl = controle.controles()
    casos = [
        ("reembolsar_pedido", {"pedido_id": 1002, "valor": float("nan"), "motivo": "x"}),
        ("reembolsar_pedido", {"pedido_id": 1002, "valor": -10.0, "motivo": "x"}),
        ("enviar_email", {"para": "ana.souza@exemplo.com\nBcc: todos@fora.com", "assunto": "a", "corpo": "b"}),
        ("enviar_email", {"para": "ana.souza@exemplo.com", "assunto": "a" * 500, "corpo": "b"}),
        ("buscar_produtos", {"busca": "x" * 1000}),
    ]
    for nome, entrada in casos:
        assert controle.avaliar(nome, entrada, ctrl).acao == "bloquear", (nome, entrada)


# -------------------------------------------------------------------- CSRF
def test_csrf_bloqueado(cliente):
    assert cliente.post(
        "/api/demo/resetar", content="", headers={"content-type": "application/x-www-form-urlencoded"}
    ).status_code == 415
    assert cliente.post(
        "/api/tarefas", content='{"tarefa":"x"}', headers={"content-type": "text/plain"}
    ).status_code == 415
    assert cliente.post(
        "/api/demo/resetar", json={}, headers={"origin": "https://site-malicioso.example"}
    ).status_code == 403
    assert cliente.post("/api/demo/resetar", json={}, headers={"sec-fetch-site": "cross-site"}).status_code == 403
    assert cliente.post("/api/demo/resetar", json={}, headers={"origin": "http://testserver"}).status_code == 200


# ------------------------------------------------------------ outras defesas
def test_cabecalhos_de_seguranca(cliente):
    for rota in ["/", "/api/status"]:
        h = cliente.get(rota).headers
        assert "frame-ancestors 'none'" in h["content-security-policy"]
        assert h["x-frame-options"] == "DENY"
        assert h["x-content-type-options"] == "nosniff"
        assert h["referrer-policy"] == "no-referrer"


def test_documentacao_desligada_online(visitante):
    for rota in ["/docs", "/openapi.json", "/loja/docs", "/loja/openapi.json"]:
        assert visitante.get(rota).status_code == 404, rota


def test_requisicao_grande_demais(cliente):
    corpo = json.dumps({"tarefa": "x" * 100_000})
    resp = cliente.post("/api/tarefas", content=corpo, headers={"content-type": "application/json"})
    assert resp.status_code == 413


def test_limite_de_tarefas_por_minuto(operador):
    codigos = [operador.post("/api/tarefas", json={"tarefa": "Como está o estoque?"}).status_code for _ in range(21)]
    assert codigos[:20] == [202] * 20
    assert codigos[20] == 429


def test_api_da_loja_so_com_a_chave_certa(visitante):
    assert visitante.get("/loja/clientes").status_code == 401
    assert visitante.get("/loja/clientes", headers={"X-API-Key": "dev-loja-123"}).status_code == 401
    assert visitante.get("/loja/clientes", headers={"X-API-Key": CHAVE_LOJA}).status_code == 200


def test_local_sem_senha_continua_sem_login(cliente):
    assert cliente.get("/api/sessao").json() == {"login_necessario": False, "autenticado": True}
    assert cliente.get("/api/loja").status_code == 200
