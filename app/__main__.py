"""Sobe o servidor: python -m app  (abre o painel em http://127.0.0.1:8000).

Variáveis úteis:
- PORTA (ou PORT, usada por serviços de hospedagem como o Render): porta do servidor.
- HOST: 127.0.0.1 (padrão, só este computador) ou 0.0.0.0 (hospedagem, liga o modo público).
- SENHA_OPERADOR: senha do painel (obrigatória no modo público, mínimo de 12 caracteres).
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
    if "LOJA_URL" not in os.environ:
        cfg.loja_url = f"http://127.0.0.1:{porta}/loja"
    problemas = cfg.problemas_de_publicacao()
    if problemas:
        print("\nNão vou publicar o painel assim, porque ficaria inseguro:")
        for problema in problemas:
            print(f"  - {problema}")
        print("Defina as variáveis de ambiente acima (no Render: Environment) e tente de novo.\n")
        raise SystemExit(1)

    endereco = f"http://127.0.0.1:{porta}"
    print(f"Modo do agente: {cfg.modo}" + (f" ({cfg.modelo})" if cfg.modo == "real" else " (sem chave, sem custo)"))
    print(f"Painel: {endereco}")
    if os.environ.get("ABRIR_NAVEGADOR") == "1":
        threading.Timer(1.5, webbrowser.open, args=[endereco]).start()
    if cfg.exige_login:
        print("Login do operador: ativado")
    uvicorn.run(criar_app(cfg), host=cfg.host, port=porta, server_header=False)


if __name__ == "__main__":
    principal()
