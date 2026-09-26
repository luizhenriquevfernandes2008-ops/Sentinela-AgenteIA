"""API REST da loja. É por aqui (e só por aqui) que o agente age na loja.

Toda chamada exige o cabeçalho X-API-Key, como numa integração real.
"""

from __future__ import annotations

import hmac

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .loja import ErroLoja, Loja


class ItemPedido(BaseModel):
    produto_id: int
    quantidade: int = Field(gt=0)


class NovoPedido(BaseModel):
    cliente_id: int
    itens: list[ItemPedido] = Field(min_length=1)


class Cancelamento(BaseModel):
    motivo: str = Field(min_length=3)


class Reembolso(BaseModel):
    valor: float = Field(gt=0)
    motivo: str = Field(min_length=3)


class Email(BaseModel):
    para: str
    assunto: str = Field(min_length=1)
    corpo: str = Field(min_length=1)


def criar_app_loja(loja: Loja, api_key: str, documentacao: bool = True) -> FastAPI:
    app = FastAPI(
        title="Loja Fictícia — API",
        version="1.0",
        docs_url="/docs" if documentacao else None,
        redoc_url="/redoc" if documentacao else None,
        openapi_url="/openapi.json" if documentacao else None,
    )

    def exigir_chave(x_api_key: str | None = Header(default=None)) -> None:
        # compare_digest leva o mesmo tempo acerte ou erre: não dá para descobrir
        # a chave caractere por caractere medindo o tempo de resposta.
        if x_api_key is None or not hmac.compare_digest(x_api_key.encode(), api_key.encode()):
            raise HTTPException(status_code=401, detail="X-API-Key ausente ou inválida.")

    @app.exception_handler(ErroLoja)
    async def _erro_loja(_: Request, erro: ErroLoja) -> JSONResponse:
        return JSONResponse(status_code=erro.status_http, content={"detail": str(erro)})

    deps = [Depends(exigir_chave)]

    @app.get("/produtos", dependencies=deps)
    def listar_produtos(busca: str = ""):
        return loja.listar_produtos(busca)

    @app.get("/produtos/{produto_id}", dependencies=deps)
    def obter_produto(produto_id: int):
        return loja.produto(produto_id)

    @app.get("/clientes", dependencies=deps)
    def listar_clientes(busca: str = ""):
        return loja.listar_clientes(busca)

    @app.get("/clientes/{cliente_id}", dependencies=deps)
    def obter_cliente(cliente_id: int):
        return loja.cliente(cliente_id)

    @app.get("/pedidos", dependencies=deps)
    def listar_pedidos(cliente_id: int | None = None, status: str | None = None):
        return loja.listar_pedidos(cliente_id, status)

    @app.get("/pedidos/{pedido_id}", dependencies=deps)
    def obter_pedido(pedido_id: int):
        return loja.pedido(pedido_id)

    @app.post("/pedidos", status_code=201, dependencies=deps)
    def criar_pedido(dados: NovoPedido):
        return loja.criar_pedido(dados.cliente_id, [i.model_dump() for i in dados.itens])

    @app.post("/pedidos/{pedido_id}/cancelar", dependencies=deps)
    def cancelar_pedido(pedido_id: int, dados: Cancelamento):
        return loja.cancelar_pedido(pedido_id, dados.motivo)

    @app.post("/pedidos/{pedido_id}/reembolso", dependencies=deps)
    def reembolsar(pedido_id: int, dados: Reembolso):
        return loja.reembolsar(pedido_id, dados.valor, dados.motivo)

    @app.get("/emails", dependencies=deps)
    def listar_emails():
        return loja.listar_emails()

    @app.post("/emails", status_code=201, dependencies=deps)
    def enviar_email(dados: Email):
        return loja.enviar_email(dados.para, dados.assunto, dados.corpo)

    @app.get("/reembolsos", dependencies=deps)
    def listar_reembolsos():
        return loja.listar_reembolsos()

    return app
