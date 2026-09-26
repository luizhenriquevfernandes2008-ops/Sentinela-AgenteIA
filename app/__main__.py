"""Sobe o servidor: python -m app  (abre o painel em http://127.0.0.1:8000)."""

import os

import uvicorn

from .config import Config
from .main import criar_app


def principal() -> None:
    porta = int(os.environ.get("PORTA", "8000"))
    cfg = Config.do_ambiente()
    if "LOJA_URL" not in os.environ:
        cfg.loja_url = f"http://127.0.0.1:{porta}/loja"
    print(f"Modo do agente: {cfg.modo}" + (f" ({cfg.modelo})" if cfg.modo == "real" else " (sem chave, sem custo)"))
    print(f"Painel: http://127.0.0.1:{porta}")
    uvicorn.run(criar_app(cfg), host="127.0.0.1", port=porta)


if __name__ == "__main__":
    principal()
