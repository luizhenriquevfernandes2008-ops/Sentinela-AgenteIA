"""Servidor: API da loja (/loja), API do agente e do painel (/api) e o painel (/)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agente import Agente, AgentePausado
from .cerebro import Cerebro, CerebroClaude, CerebroSimulado
from .config import Config
from .controle import Controle
from .ferramentas import FERRAMENTAS, ClienteLoja
from .loja import Loja
from .loja_api import criar_app_loja

ESTATICOS = Path(__file__).resolve().parent.parent / "static"


class NovaTarefa(BaseModel):
    tarefa: str = Field(min_length=1, max_length=2000)


class Decisao(BaseModel):
    aprovar: bool
    comentario: str = Field(default="", max_length=500)


def criar_app(
    cfg: Config | None = None,
    http_loja: httpx.Client | None = None,
    cerebro: Cerebro | None = None,
) -> FastAPI:
    cfg = cfg or Config.do_ambiente()
    loja = Loja(cfg.banco_loja)
    controle = Controle(cfg.banco_controle)
    app_loja = criar_app_loja(loja, cfg.loja_api_key)

    if cerebro is None:
        cerebro = CerebroClaude(cfg) if cfg.modo == "real" else CerebroSimulado()
    modo = "simulado" if isinstance(cerebro, CerebroSimulado) else "real"
    # O agente fala com a loja por HTTP, como falaria com um sistema externo.
    cliente_loja = ClienteLoja(http_loja or httpx.Client(base_url=cfg.loja_url, timeout=15), cfg.loja_api_key)
    agente = Agente(cfg, controle, cliente_loja, cerebro, modo)

    app = FastAPI(title="Sentinela — agente de IA com controle humano", version="1.0")
    app.state.agente = agente
    app.state.loja = loja
    app.state.controle = controle
    app.mount("/loja", app_loja)

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
    def nova_tarefa(dados: NovaTarefa, background: BackgroundTasks) -> dict[str, int]:
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
        except (ValueError, TypeError) as erro:
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

    @app.post("/api/loja/resetar")
    def resetar_loja() -> dict[str, str]:
        loja.resetar()
        return {"status": "ok"}

    # ------------------------------------------------------------------ painel
    @app.get("/", include_in_schema=False)
    def painel() -> FileResponse:
        return FileResponse(ESTATICOS / "index.html")

    app.mount("/static", StaticFiles(directory=ESTATICOS), name="static")
    return app
