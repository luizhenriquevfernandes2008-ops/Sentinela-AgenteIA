CHAVE = {"X-API-Key": "dev-loja-123"}


def test_exige_api_key(cliente):
    assert cliente.get("/loja/produtos").status_code == 401
    assert cliente.get("/loja/produtos", headers={"X-API-Key": "errada"}).status_code == 401
    assert cliente.get("/loja/produtos", headers=CHAVE).status_code == 200


def test_criar_pedido_baixa_estoque(cliente):
    antes = cliente.get("/loja/produtos/2", headers=CHAVE).json()["estoque"]
    resp = cliente.post("/loja/pedidos", headers=CHAVE, json={"cliente_id": 1, "itens": [{"produto_id": 2, "quantidade": 3}]})
    assert resp.status_code == 201
    assert resp.json()["total"] == round(89.90 * 3, 2)
    assert cliente.get("/loja/produtos/2", headers=CHAVE).json()["estoque"] == antes - 3


def test_estoque_insuficiente(cliente):
    resp = cliente.post("/loja/pedidos", headers=CHAVE, json={"cliente_id": 1, "itens": [{"produto_id": 5, "quantidade": 1}]})
    assert resp.status_code == 409
    assert "Estoque insuficiente" in resp.json()["detail"]


def test_cancelar_devolve_estoque_e_nao_cancela_entregue(cliente):
    antes = cliente.get("/loja/produtos/4", headers=CHAVE).json()["estoque"]
    resp = cliente.post("/loja/pedidos/1002/cancelar", headers=CHAVE, json={"motivo": "teste"})
    assert resp.status_code == 200 and resp.json()["status"] == "cancelado"
    assert cliente.get("/loja/produtos/4", headers=CHAVE).json()["estoque"] == antes + 1
    assert cliente.post("/loja/pedidos/1001/cancelar", headers=CHAVE, json={"motivo": "teste"}).status_code == 409


def test_reembolso_nao_passa_do_total(cliente):
    total = cliente.get("/loja/pedidos/1002", headers=CHAVE).json()["total"]
    assert cliente.post("/loja/pedidos/1002/reembolso", headers=CHAVE, json={"valor": total + 1, "motivo": "teste"}).status_code == 409
    resp = cliente.post("/loja/pedidos/1002/reembolso", headers=CHAVE, json={"valor": total, "motivo": "teste"})
    assert resp.json()["status"] == "reembolsado"
