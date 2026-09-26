"""Sobe o servidor: python -m app  (abre o painel em http://127.0.0.1:8000).

Variáveis úteis:
- PORTA (ou PORT, usada por serviços de hospedagem como o Render): porta do servidor.
- HOST: 127.0.0.1 (padrão, só este computador) ou 0.0.0.0 (hospedagem, liga o modo público).
- SENHA_OPERADOR: senha extra de administrador (opcional; com menos de 12 caracteres é ignorada).
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
    endereco = f"http://127.0.0.1:{porta}"
    if cfg.aviso_modo:
        print(f"AVISO: {cfg.aviso_modo}")
    descricao = {"claude": "API do Claude, paga", "groq": "Groq, plano gratuito"}.get(cfg.modo, "sem IA, sem custo")
    print(f"Modo do agente: {cfg.modo} · {cfg.modelo_em_uso} ({descricao})")
    print(f"Painel: {endereco}")
    if os.environ.get("ABRIR_NAVEGADOR") == "1":
        threading.Timer(1.5, webbrowser.open, args=[endereco]).start()
    if cfg.exige_login:
        print("Login do operador: ativado")
    app = criar_app(cfg)
    codigo = app.state.contas.codigo_configuracao
    if cfg.exige_login and codigo:
        print("\n" + "=" * 62)
        print("  CÓDIGO PARA CRIAR A SENHA DE ADMINISTRADOR:  " + codigo)
        print("  Abra o site, toque em 'Configurar administrador' e use-o.")
        print("  Ele vale uma vez e muda a cada reinício do servidor.")
        print("=" * 62 + "\n", flush=True)
    uvicorn.run(app, host=cfg.host, port=porta, server_header=False)


if __name__ == "__main__":
    principal()
