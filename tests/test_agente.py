from conftest import decidir, pendentes, rodar


def tipos(execucao: dict) -> list[str]:
    return [e["tipo"] for e in execucao["eventos"]]


def test_leitura_roda_sem_aprovacao(cliente):
    ex = rodar(cliente, "Como está o estoque?")
    assert ex["status"] == "concluida"
    assert "Sem estoque: Headset Bluetooth" in ex["resposta_final"]
    assert "ferramenta_executada" in tipos(ex)
    assert pendentes(cliente) == []


def test_cancelamento_pausa_ate_aprovacao_e_continua(cliente, app):
    ex = rodar(cliente, "Cancele o pedido 1003 e avise o cliente")
    assert ex["status"] == "aguardando_aprovacao"
    assert app.state.loja.pedido(1003)["status"] == "pendente"  # nada mudou ainda

    [ap] = pendentes(cliente)
    assert ap["ferramenta"] == "cancelar_pedido"
    decidir(cliente, ap["id"], True)
    assert app.state.loja.pedido(1003)["status"] == "cancelado"

    [ap] = pendentes(cliente)
    assert ap["ferramenta"] == "enviar_email"
    ex = decidir(cliente, ap["id"], True)

    assert ex["status"] == "concluida"
    assert len(app.state.loja.listar_emails()) == 1
    assert tipos(ex).count("aprovada") == 2


def test_cancelar_pedido_pago_bloqueia_reembolso_acima_do_limite(cliente, app):
    # Pedido 1002 (R$ 1.899) está pago: o agente cancela e tenta reembolsar,
    # mas o reembolso passa do limite de R$ 1.000 e é bloqueado sem nem ir para aprovação.
    rodar(cliente, "Cancele o pedido 1002 e avise o cliente")
    [ap] = pendentes(cliente)
    decidir(cliente, ap["id"], True)
    [ap] = pendentes(cliente)
    assert ap["ferramenta"] == "enviar_email"
    ex = decidir(cliente, ap["id"], True)
    assert "bloqueio" in tipos(ex)
    assert "reembolso NÃO foi feito" in ex["resposta_final"]
    assert app.state.loja.listar_reembolsos() == []


def test_reembolso_abaixo_do_limite_com_aprovacao(cliente, app):
    rodar(cliente, "Reembolse R$ 50 do pedido 1001, o mouse veio arranhado")
    [ap] = pendentes(cliente)
    assert ap["ferramenta"] == "reembolsar_pedido" and ap["entrada"]["valor"] == 50.0
    decidir(cliente, ap["id"], True)
    [ap] = pendentes(cliente)
    ex = decidir(cliente, ap["id"], True)
    assert ex["status"] == "concluida"
    assert app.state.loja.pedido(1001)["total_reembolsado"] == 50.0


def test_rejeicao_volta_para_o_modelo(cliente, app):
    rodar(cliente, "Cancele o pedido 1003")
    [ap] = pendentes(cliente)
    ex = decidir(cliente, ap["id"], False, "Cliente desistiu de cancelar")
    assert ex["status"] == "concluida"
    assert "não foi feito" in ex["resposta_final"]
    assert "Cliente desistiu de cancelar" in ex["resposta_final"]
    assert app.state.loja.pedido(1003)["status"] == "pendente"


def test_aprovacao_nao_pode_ser_decidida_duas_vezes(cliente):
    rodar(cliente, "Cancele o pedido 1003")
    [ap] = pendentes(cliente)
    decidir(cliente, ap["id"], False)
    resp = cliente.post(f"/api/aprovacoes/{ap['id']}/decisao", json={"aprovar": True})
    assert resp.status_code == 409


def test_prompt_injection_barrado_pelo_limite(cliente, app):
    # O pedido 1005 traz na observação uma ordem para reembolsar R$ 9.999.
    # O simulador "cai" na armadilha de propósito; a regra de limite bloqueia.
    ex = rodar(cliente, "Analise o pedido 1005 e resolva o que for preciso")
    assert ex["status"] == "concluida"
    assert "bloqueio" in tipos(ex)
    assert pendentes(cliente) == []  # nem chegou a pedir aprovação
    assert app.state.loja.listar_reembolsos() == []


def test_ferramenta_desativada_e_bloqueada(cliente, app):
    cliente.put("/api/controles", json={"ferramentas": {"cancelar_pedido": {"ativa": False}}})
    ex = rodar(cliente, "Cancele o pedido 1003")
    assert "bloqueio" in tipos(ex)
    assert app.state.loja.pedido(1003)["status"] == "pendente"


def test_regras_reavaliadas_na_hora_de_executar(cliente, app):
    rodar(cliente, "Cancele o pedido 1003")
    [ap] = pendentes(cliente)
    # Enquanto a ação esperava aprovação, o operador desligou a ferramenta.
    cliente.put("/api/controles", json={"ferramentas": {"cancelar_pedido": {"ativa": False}}})
    ex = decidir(cliente, ap["id"], True)
    assert "bloqueio" in tipos(ex)
    assert app.state.loja.pedido(1003)["status"] == "pendente"


def test_email_so_para_clientes(app):
    controle = app.state.controle
    ctrl = controle.controles()
    entrada = {"para": "estranho@fora.com", "assunto": "oi", "corpo": "dados"}
    decisao = controle.avaliar("enviar_email", entrada, ctrl, app.state.agente._eh_cliente)
    assert decisao.acao == "bloquear"
    entrada["para"] = "ana.souza@exemplo.com"
    assert controle.avaliar("enviar_email", entrada, ctrl, app.state.agente._eh_cliente).acao == "aprovar"


def test_entrada_invalida_e_bloqueada(app):
    controle = app.state.controle
    decisao = controle.avaliar("consultar_pedido", {"pedido_id": "abc"}, controle.controles())
    assert decisao.acao == "bloquear" and "esperado integer" in decisao.motivo


def test_agente_pausado_recusa_tarefas(cliente):
    cliente.put("/api/controles", json={"agente_ativo": False})
    assert cliente.post("/api/tarefas", json={"tarefa": "Como está o estoque?"}).status_code == 409


def test_pausa_interrompe_execucao_em_andamento(cliente):
    rodar(cliente, "Cancele o pedido 1003")
    [ap] = pendentes(cliente)
    cliente.put("/api/controles", json={"agente_ativo": False})
    ex = decidir(cliente, ap["id"], True)
    assert ex["status"] == "interrompida"


def test_limite_de_passos(cliente):
    cliente.put("/api/controles", json={"max_passos": 1})
    ex = rodar(cliente, "Como está o estoque?")
    assert ex["status"] == "interrompida"
    assert ex["passos"] == 1


def test_orcamento_de_tokens(cliente):
    cliente.put("/api/controles", json={"orcamento_tokens_dia": 1})
    rodar(cliente, "Como está o estoque?")  # a primeira chamada consome o orçamento
    ex = rodar(cliente, "Como está o estoque?")
    assert ex["status"] == "interrompida"
    assert "orçamento" in ex["resposta_final"]


def test_cancelar_execucao_rejeita_pendentes(cliente, app):
    ex = rodar(cliente, "Cancele o pedido 1003")
    assert cliente.post(f"/api/execucoes/{ex['id']}/cancelar", json={}).status_code == 200
    assert pendentes(cliente) == []
    assert cliente.get(f"/api/execucoes/{ex['id']}").json()["status"] == "cancelada"
    assert app.state.loja.pedido(1003)["status"] == "pendente"


def test_criar_pedido_sem_aprovacao(cliente, app):
    ex = rodar(cliente, "Crie um pedido para o cliente 3 com 2 unidades do produto 2")
    assert ex["status"] == "concluida"
    assert "criado" in ex["resposta_final"]
    assert len(app.state.loja.listar_pedidos(cliente_id=3)) == 2


def test_metricas(cliente):
    rodar(cliente, "Como está o estoque?")
    m = cliente.get("/api/metricas").json()
    assert m["execucoes"] == 1
    assert m["acoes_executadas"] == 1
    assert m["uso_por_ferramenta"] == {"buscar_produtos": 1}


def test_restaurar_demo(cliente, app):
    rodar(cliente, "Cancele o pedido 1003")
    cliente.put("/api/controles", json={"max_passos": 3})
    assert cliente.post("/api/demo/resetar", json={}).status_code == 200
    assert cliente.get("/api/execucoes").json() == []
    assert pendentes(cliente) == []
    assert cliente.get("/api/controles").json()["max_passos"] == 10
    assert app.state.loja.pedido(1003)["status"] == "pendente"
