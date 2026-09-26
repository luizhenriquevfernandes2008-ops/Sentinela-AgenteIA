"""Configuração lida de variáveis de ambiente (ou de um arquivo .env)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent


def _carregar_dotenv(caminho: Path) -> None:
    """Leitor mínimo de .env: CHAVE=valor por linha, sem sobrescrever o ambiente."""
    if not caminho.exists():
        return
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or linha.startswith("#") or "=" not in linha:
            continue
        chave, valor = linha.split("=", 1)
        os.environ.setdefault(chave.strip(), valor.strip().strip('"').strip("'"))


# Preço por milhão de tokens (USD). Fonte: tabela pública da Anthropic.
PRECOS_POR_MILHAO = {
    "claude-opus-5": {"entrada": 5.00, "saida": 25.00},
    "claude-sonnet-5": {"entrada": 2.00, "saida": 10.00},
    "claude-haiku-4-5": {"entrada": 1.00, "saida": 5.00},
}


@dataclass
class Config:
    # "simulado" roda sem internet e sem custo; "real" usa a API do Claude.
    modo: str = "simulado"
    anthropic_api_key: str = ""
    modelo: str = "claude-opus-5"
    esforco: str = "high"  # low | medium | high | xhigh | max

    # Onde fica a API REST da loja (o "sistema da empresa" que o agente opera).
    loja_url: str = "http://127.0.0.1:8000/loja"
    loja_api_key: str = "dev-loja-123"

    banco_loja: Path = field(default_factory=lambda: RAIZ / "dados" / "loja.db")
    banco_controle: Path = field(default_factory=lambda: RAIZ / "dados" / "controle.db")

    @classmethod
    def do_ambiente(cls) -> "Config":
        _carregar_dotenv(RAIZ / ".env")
        cfg = cls()
        cfg.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        modo = os.environ.get("MODO_AGENTE", "").strip().lower()
        if modo in ("simulado", "real"):
            cfg.modo = modo
        elif cfg.anthropic_api_key:
            cfg.modo = "real"
        cfg.modelo = os.environ.get("MODELO_CLAUDE", cfg.modelo)
        cfg.esforco = os.environ.get("ESFORCO_CLAUDE", cfg.esforco)
        cfg.loja_url = os.environ.get("LOJA_URL", cfg.loja_url).rstrip("/")
        cfg.loja_api_key = os.environ.get("LOJA_API_KEY", cfg.loja_api_key)
        if os.environ.get("BANCO_LOJA"):
            cfg.banco_loja = Path(os.environ["BANCO_LOJA"])
        if os.environ.get("BANCO_CONTROLE"):
            cfg.banco_controle = Path(os.environ["BANCO_CONTROLE"])
        return cfg

    def custo_usd(self, tokens_entrada: int, tokens_saida: int) -> float:
        preco = PRECOS_POR_MILHAO.get(self.modelo, PRECOS_POR_MILHAO["claude-opus-5"])
        return (tokens_entrada * preco["entrada"] + tokens_saida * preco["saida"]) / 1_000_000
