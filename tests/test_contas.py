"""Administrador criado no primeiro acesso e visitantes com acesso limitado."""

import pytest
from fastapi.testclient import TestClient

from app.main import criar_app
from conftest import rodar

CHAVE_LOJA = "chave-da-loja-bem-comprida-123"
SENHA_ADM = "minha-senha-de-adm-123"


@pytest.fixture
def app_online(cfg):
    cfg.host = "0.0.0.0"
    cfg.publico = True
    cfg.senha_operador = ""  # nenhuma senha no servidor: o ADM é criado pelo site
    cfg.loja_api_key = CHAVE_LOJA
    app = criar_app(cfg)
    app.state.agente.loja.http = TestClient(app, base_url="http://testserver/loja")
    return app


def novo(app):
    return TestClient(app)


def configurar_admin(app):
    c = novo(app)
    codigo = app.state.contas.codigo_configuracao
    resp = c.post("/api/admin/configurar", json={"codigo": codigo, "senha": SENHA_ADM})
    assert resp.status_code == 201, resp.text
    return c


def criar_visitante(app, nome="recrutador", senha="senha-visita"):
    c = novo(app)
    resp = c.post("/api/contas", json={"usuario": nome, "senha": senha})
    assert resp.status_code == 201, resp.text
    return c


# ---------------------------------------------------- configurar o administrador
def test_primeiro_acesso_cria_o_administrador(app_online):
    c = novo(app_online)
    assert c.get("/api/sessao").json()["admin_configurado"] is False
    assert c.post("/api/admin/configurar", json={"codigo": "ERRADO", "senha": SENHA_ADM}).status_code == 400
    adm = configurar_admin(app_online)
    s = adm.get("/api/sessao").json()
    assert s["autenticado"] and s["papel"] == "admin" and s["admin_configurado"]
    assert adm.put("/api/controles", json={"max_passos": 5}).status_code == 200


def test_codigo_vale_uma_vez(app_online):
    codigo = app_online.state.contas.codigo_configuracao
    configurar_admin(app_online)
    resp = novo(app_online).post("/api/admin/configurar", json={"codigo": codigo, "senha": "outra-senha-qualquer"})
    assert resp.status_code == 400
    assert app_online.state.contas.codigo_configuracao is None


def test_senha_do_admin_precisa_ser_forte(app_online):
    codigo = app_online.state.contas.codigo_configuracao
    assert novo(app_online).post("/api/admin/configurar", json={"codigo": codigo, "senha": "curta"}).status_code == 400


def test_adivinhar_o_codigo_e_bloqueado(app_online):
    c = novo(app_online)
    for i in range(5):
        assert c.post("/api/admin/configurar", json={"codigo": f"CHUTE-{i}", "senha": SENHA_ADM}).status_code == 400
    codigo = app_online.state.contas.codigo_configuracao
    assert c.post("/api/admin/configurar", json={"codigo": codigo, "senha": SENHA_ADM}).status_code == 429


def test_admin_entra_depois_com_usuario_e_senha(app_online):
    configurar_admin(app_online)
    c = novo(app_online)
    assert c.post("/api/login", json={"usuario": "admin", "senha": "errada-errada"}).status_code == 401
    assert c.post("/api/login", json={"usuario": "admin", "senha": SENHA_ADM}).json()["papel"] == "admin"


# -------------------------------------------------------------------- visitantes
def test_visitante_usa_a_demo(app_online):
    configurar_admin(app_online)
    v = criar_visitante(app_online)
    assert v.get("/api/sessao").json()["papel"] == "visitante"
    ex = rodar(v, "Cancele o pedido 1003")
    [ap] = v.get("/api/aprovacoes").json()
    assert v.post(f"/api/aprovacoes/{ap['id']}/decisao", json={"aprovar": True}).status_code == 200
    ex = v.get(f"/api/execucoes/{ex['id']}").json()
    assert ex["autor"] == "recrutador"
    aprovada = next(e for e in ex["eventos"] if e["tipo"] == "aprovada")
    assert aprovada["dados"]["por"] == "recrutador"
    assert v.get("/api/loja").status_code == 200
    assert v.get("/api/controles").status_code == 200  # pode ver as regras


def test_visitante_nao_mexe_no_que_e_de_admin(app_online):
    v = criar_visitante(app_online)
    proibidos = [
        v.put("/api/controles", json={"max_passos": 5}),
        v.put("/api/controles", json={"agente_ativo": False}),
        v.get("/api/ia"),
        v.put("/api/ia", json={"modo": "groq", "chave": "gsk_" + "a" * 40}),
        v.delete("/api/ia/chave", headers={"content-type": "application/json"}),
        v.post("/api/demo/resetar", json={}),
    ]
    assert [r.status_code for r in proibidos] == [403] * len(proibidos)


def test_visitante_entra_de_novo_com_a_propria_senha(app_online):
    criar_visitante(app_online, "maria", "senha-da-maria")
    c = novo(app_online)
    assert c.post("/api/login", json={"usuario": "maria", "senha": "errada-errada"}).status_code == 401
    assert c.post("/api/login", json={"usuario": "Maria", "senha": "senha-da-maria"}).json()["papel"] == "visitante"
    # A senha de um visitante não vale como senha de administrador.
    assert novo(app_online).post("/api/login", json={"senha": "senha-da-maria"}).status_code == 401


@pytest.mark.parametrize("nome,senha", [
    ("admin", "senha-qualquer"), ("ab", "senha-qualquer"), ("nome com espaço", "senha-qualquer"),
    ("<script>", "senha-qualquer"), ("pedro", "curta"),
])
def test_nomes_e_senhas_invalidos(app_online, nome, senha):
    assert novo(app_online).post("/api/contas", json={"usuario": nome, "senha": senha}).status_code == 400


def test_nome_repetido(app_online):
    criar_visitante(app_online, "joao")
    assert novo(app_online).post("/api/contas", json={"usuario": "joao", "senha": "outra-senha"}).status_code == 400


def test_limite_de_contas_por_ip(app_online):
    c = novo(app_online)
    codigos = [c.post("/api/contas", json={"usuario": f"pessoa{i}", "senha": "senha-boa-123"}).status_code for i in range(4)]
    assert codigos == [201, 201, 201, 429]


def test_papel_nao_pode_ser_trocado_no_cookie(app_online):
    v = criar_visitante(app_online)
    token = v.cookies.get("sentinela_sessao")
    partes = token.split(".")
    partes[1] = "admin"
    v.cookies.set("sentinela_sessao", ".".join(partes))
    assert v.put("/api/controles", json={"max_passos": 5}).status_code == 401


def test_conta_apagada_perde_o_acesso(app_online):
    v = criar_visitante(app_online, "temporario")
    app_online.state.contas._conn.execute("DELETE FROM contas WHERE nome = 'temporario'")
    assert v.get("/api/loja").status_code == 401


def test_senha_do_servidor_continua_valendo_para_admin(cfg):
    cfg.publico = True
    cfg.loja_api_key = CHAVE_LOJA
    cfg.senha_operador = "senha-do-servidor-123"
    c = TestClient(criar_app(cfg))
    assert c.post("/api/login", json={"senha": "senha-do-servidor-123"}).json()["papel"] == "admin"
