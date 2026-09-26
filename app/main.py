"""Servidor: API da loja (/loja), API do agente e do painel (/api) e o painel (/)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import hmac

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agente import Agente, AgentePausado
from .cerebro import Cerebro, CerebroClaude, CerebroSimulado
from .config import Config
from .controle import Controle, ControleInvalido
from .ferramentas import FERRAMENTAS, ClienteLoja
from .loja import Loja
from .loja_api import criar_app_loja
from .seguranca import COOKIE_SESSAO, Limitador, Sessoes, conexao_https, instalar_filtro, ip_do_cliente

ESTATICOS = Path(__file__).resolve().parent.parent / "static"


class NovaTarefa(BaseModel):
    tarefa: str = Field(min_length=1, max_length=2000)


class Login(BaseModel):
    senha: str = Field(min_length=1, max_length=200)


class Decisao(BaseModel):
    aprovar: bool
    comentario: str = Field(default="", max_length=500)


def criar_app(
    cfg: Config | None = None,
    http_loja: httpx.Client | None = None,
    cerebro: Cerebro | None = None,
) -> FastAPI:
    cfg = cfg or Config.do_ambiente()
    problemas = cfg.problemas_de_publicacao()
    if problemas:
        # Melhor não subir do que subir aberto na internet.
        raise RuntimeError("Configuração insegura para publicar:\n- " + "\n- ".join(problemas))

    loja = Loja(cfg.banco_loja)
    controle = Controle(cfg.banco_controle, somente_mais_rigido=cfg.publico)
    app_loja = criar_app_loja(loja, cfg.loja_api_key, documentacao=not cfg.publico)

    if cerebro is None:
        cerebro = CerebroClaude(cfg) if cfg.modo == "real" else CerebroSimulado()
    modo = "simulado" if isinstance(cerebro, CerebroSimulado) else "real"
    # O agente fala com a loja por HTTP, como falaria com um sistema externo.
    cliente_loja = ClienteLoja(http_loja or httpx.Client(base_url=cfg.loja_url, timeout=15), cfg.loja_api_key)
    agente = Agente(cfg, controle, cliente_loja, cerebro, modo)

    app = FastAPI(
        title="Sentinela — agente de IA com controle humano",
        version="1.0",
        # Online, a documentação interativa não fica exposta.
        docs_url=None if cfg.publico else "/docs",
        redoc_url=None if cfg.publico else "/redoc",
        openapi_url=None if cfg.publico else "/openapi.json",
    )
    app.state.agente = agente
    app.state.loja = loja
    app.state.controle = controle
    app.mount("/loja", app_loja)

    sessoes = Sessoes(cfg.segredo_sessao, cfg.horas_sessao)
    instalar_filtro(app, cfg, sessoes)
    # Força bruta na senha: 5 erros por IP ou 30 no total a cada 15 minutos.
    erros_por_ip = Limitador(5, 15 * 60)
    erros_no_total = Limitador(30, 15 * 60)
    # Enxurrada de tarefas: 20 por minuto por pessoa e 60 no total.
    tarefas_por_pessoa = Limitador(20, 60)
    tarefas_no_total = Limitador(60, 60)

    # ------------------------------------------------------------------ login
    @app.get("/api/sessao")
    def sessao(request: Request) -> dict[str, bool]:
        return {
            "login_necessario": cfg.exige_login,
            "autenticado": not cfg.exige_login or sessoes.identificador(request.cookies.get(COOKIE_SESSAO)) is not None,
        }

    @app.post("/api/login")
    def login(dados: Login, request: Request, response: Response) -> dict[str, bool]:
        if not cfg.exige_login:
            return {"autenticado": True}
        ip = ip_do_cliente(request, cfg)
        if erros_por_ip.bloqueado(ip) or erros_no_total.bloqueado("todos"):
            raise HTTPException(429, "Muitas tentativas erradas. Espere 15 minutos e tente de novo.")
        if not cfg.senha_operador or not hmac.compare_digest(dados.senha.encode(), cfg.senha_operador.encode()):
            erros_por_ip.registrar(ip)
            erros_no_total.registrar("todos")
            raise HTTPException(401, "Senha incorreta.")
        token, duracao = sessoes.criar()
        response.set_cookie(
            COOKIE_SESSAO, token, max_age=duracao, httponly=True, samesite="strict",
            secure=conexao_https(request), path="/",
        )
        return {"autenticado": True}

    @app.post("/api/logout")
    def logout(request: Request, response: Response) -> dict[str, bool]:
        sessoes.revogar(request.cookies.get(COOKIE_SESSAO))
        response.delete_cookie(COOKIE_SESSAO, path="/")
        return {"autenticado": False}

    # ----------------------------------------------------------------- status
    @app.get("/api/status")
    def status() -> dict[str, Any]:
        return {
            "modo": modo,
            "modelo": agente.modelo,
            "agente_ativo": controle.controles()["agente_ativo"],
            "ferramentas": {n: {"risco": f.risco, "descricao": f.descricao} for n, f in FERRAMENTAS.items()},
        }

    # ----------------------------------------------------------------- tarefas
    @app.post("/api/tarefas", status_code=202)
    def nova_tarefa(dados: NovaTarefa, request: Request, background: BackgroundTasks) -> dict[str, int]:
        pessoa = getattr(request.state, "sessao", None) or ip_do_cliente(request, cfg)
        if not tarefas_no_total.tentar("todos") or not tarefas_por_pessoa.tentar(pessoa):
            raise HTTPException(429, "Muitas tarefas em pouco tempo. Espere um minuto.")
        try:
            eid = agente.iniciar(dados.tarefa)
        except AgentePausado as erro:
            raise HTTPException(409, str(erro))
        except ValueError as erro:
            raise HTTPException(400, str(erro))
        background.add_task(agente.rodar, eid)
        return {"execucao_id": eid}

    @app.get("/api/execucoes")
    def listar_execucoes() -> list[dict[str, Any]]:
        return controle.listar_execucoes()

    @app.get("/api/execucoes/{execucao_id}")
    def obter_execucao(execucao_id: int) -> dict[str, Any]:
        try:
            return agente.resumo(execucao_id)
        except KeyError:
            raise HTTPException(404, "Execução não encontrada.")

    @app.post("/api/execucoes/{execucao_id}/cancelar")
    def cancelar_execucao(execucao_id: int) -> dict[str, str]:
        try:
            agente.cancelar(execucao_id)
        except KeyError:
            raise HTTPException(404, "Execução não encontrada.")
        except ValueError as erro:
            raise HTTPException(409, str(erro))
        return {"status": "cancelada"}

    # -------------------------------------------------------------- aprovações
    @app.get("/api/aprovacoes")
    def listar_aprovacoes(status: str | None = "pendente") -> list[dict[str, Any]]:
        aprovacoes = controle.listar_aprovacoes(status or None)
        tarefas = {e["id"]: e["tarefa"] for e in controle.listar_execucoes(500)}
        return [{**a, "tarefa": tarefas.get(a["execucao_id"], "")} for a in aprovacoes]

    @app.post("/api/aprovacoes/{aprovacao_id}/decisao")
    def decidir(aprovacao_id: int, dados: Decisao, background: BackgroundTasks) -> dict[str, Any]:
        try:
            eid, continuar = agente.decidir(aprovacao_id, dados.aprovar, dados.comentario)
        except KeyError:
            raise HTTPException(404, "Aprovação não encontrada.")
        except ValueError as erro:
            raise HTTPException(409, str(erro))
        if continuar:
            background.add_task(agente.rodar, eid)
        return {"execucao_id": eid, "continuando": continuar}

    # --------------------------------------------------------------- controles
    @app.get("/api/controles")
    def obter_controles() -> dict[str, Any]:
        return controle.controles()

    @app.put("/api/controles")
    def atualizar_controles(mudancas: dict[str, Any]) -> dict[str, Any]:
        try:
            return controle.atualizar_controles(mudancas)
        except (ControleInvalido, ValueError, TypeError) as erro:
            raise HTTPException(400, str(erro))

    @app.get("/api/metricas")
    def metricas() -> dict[str, Any]:
        return {**controle.metricas(), "modo": modo}

    # ------------------------------------------------- visão da loja (painel)
    @app.get("/api/loja")
    def visao_loja() -> dict[str, Any]:
        return {
            "pedidos": loja.listar_pedidos(),
            "produtos": loja.listar_produtos(),
            "emails": loja.listar_emails(),
            "reembolsos": loja.listar_reembolsos(),
        }

    @app.post("/api/demo/resetar")
    def resetar_demo() -> dict[str, str]:
        """Volta tudo ao estado inicial: loja, histórico e controles."""
        loja.resetar()
        controle.resetar()
        return {"status": "ok"}

    # ------------------------------------------------------------------ painel
    @app.get("/", include_in_schema=False)
    def painel() -> FileResponse:
        return FileResponse(ESTATICOS / "index.html")

    app.mount("/static", StaticFiles(directory=ESTATICOS), name="static")
    return app
