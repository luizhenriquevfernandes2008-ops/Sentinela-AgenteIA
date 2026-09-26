"""Sobe o servidor: python -m app  (abre o painel em http://127.0.0.1:8000).

Variáveis úteis:
- PORTA (ou PORT, usada por serviços de hospedagem como o Render): porta do servidor.
- HOST: 127.0.0.1 (padrão, só este computador) ou 0.0.0.0 (hospedagem).
- ABRIR_NAVEGADOR=1: abre o painel no navegador assim que o servidor subir.
"""

import os
import threading
import webbrowser

import uvicorn

from .config import Config
from .main import criar_app


def principal() -> None:
    cfg = Config.do_ambiente()  # carrega o .env antes de ler as outras variáveis
    porta = int(os.environ.get("PORTA") or os.environ.get("PORT") or "8000")
    host = os.environ.get("HOST", "127.0.0.1")
    if "LOJA_URL" not in os.environ:
        cfg.loja_url = f"http://127.0.0.1:{porta}/loja"

    endereco = f"http://127.0.0.1:{porta}"
    print(f"Modo do agente: {cfg.modo}" + (f" ({cfg.modelo})" if cfg.modo == "real" else " (sem chave, sem custo)"))
    print(f"Painel: {endereco}")
    if os.environ.get("ABRIR_NAVEGADOR") == "1":
        threading.Timer(1.5, webbrowser.open, args=[endereco]).start()
    uvicorn.run(criar_app(cfg), host=host, port=porta)


if __name__ == "__main__":
    principal()
