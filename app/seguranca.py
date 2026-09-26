"""Segurança do painel: login, sessão, limites de taxa e o filtro de requisições.

O painel é o "operador humano" do agente: quem o controla aprova reembolsos
e mexe nas regras. Por isso, quando o site está na internet, tudo em /api
exige login, e cada requisição que muda algo passa por checagens contra
CSRF (outro site agindo em nome de quem está logado).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import Config

COOKIE_SESSAO = "sentinela_sessao"
TAMANHO_MAXIMO_CORPO = 64 * 1024  # 64 KB: nenhuma requisição legítima do painel chega perto
# /api/status responde sem login, mas só com {"ok": true} (ver main.py).
ROTAS_SEM_LOGIN = {"/api/saude", "/api/status", "/api/sessao", "/api/login"}
METODOS_QUE_ALTERAM = {"POST", "PUT", "PATCH", "DELETE"}

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; connect-src 'self'; font-src 'self'; object-src 'none'; "
    "base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
)


# ================================================================== sessão
class Sessoes:
    """Tokens de sessão assinados com HMAC: prazo.identificador.assinatura."""

    def __init__(self, segredo: str, horas: int):
        self._segredo = segredo.encode()
        self._duracao = horas * 3600
        self._revogados: dict[str, float] = {}
        self._trava = threading.Lock()

    def _assinar(self, conteudo: str) -> str:
        mac = hmac.new(self._segredo, conteudo.encode(), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(mac).decode().rstrip("=")

    def criar(self) -> tuple[str, int]:
        prazo = int(time.time()) + self._duracao
        conteudo = f"{prazo}.{secrets.token_urlsafe(18)}"
        return f"{conteudo}.{self._assinar(conteudo)}", self._duracao

    def identificador(self, token: str | None) -> str | None:
        """Devolve o identificador da sessão se o token for válido, senão None."""
        if not token or token.count(".") != 2:
            return None
        prazo, ident, assinatura = token.split(".")
        if not hmac.compare_digest(assinatura, self._assinar(f"{prazo}.{ident}")):
            return None
        if not prazo.isdigit() or int(prazo) < time.time():
            return None
        with self._trava:
            if ident in self._revogados:
                return None
        return ident

    def revogar(self, token: str | None) -> None:
        ident = self.identificador(token)
        if ident:
            with self._trava:
                agora = time.time()
                self._revogados = {k: v for k, v in self._revogados.items() if v > agora}
                self._revogados[ident] = agora + self._duracao


# ============================================================ limites de taxa
class Limitador:
    """Janela deslizante: no máximo `limite` eventos por `janela` segundos por chave."""

    def __init__(self, limite: int, janela: float):
        self.limite = limite
        self.janela = janela
        self._eventos: dict[str, deque[float]] = defaultdict(deque)
        self._trava = threading.Lock()

    def _limpar(self, chave: str, agora: float) -> deque[float]:
        fila = self._eventos[chave]
        while fila and fila[0] <= agora - self.janela:
            fila.popleft()
        return fila

    def bloqueado(self, chave: str) -> bool:
        with self._trava:
            return len(self._limpar(chave, time.monotonic())) >= self.limite

    def registrar(self, chave: str) -> None:
        with self._trava:
            agora = time.monotonic()
            self._limpar(chave, agora).append(agora)
            if len(self._eventos) > 10_000:  # não deixa a memória crescer sem fim
                for k in [k for k, v in self._eventos.items() if not v]:
                    del self._eventos[k]

    def tentar(self, chave: str) -> bool:
        """Registra o evento se couber no limite. Retorna False se estourou."""
        with self._trava:
            agora = time.monotonic()
            fila = self._limpar(chave, agora)
            if len(fila) >= self.limite:
                return False
            fila.append(agora)
            return True


def ip_do_cliente(request: Request, cfg: Config) -> str:
    """No Render, o IP real vem no fim do X-Forwarded-For (o proxy acrescenta por último)."""
    if cfg.publico:
        encaminhado = request.headers.get("x-forwarded-for", "")
        if encaminhado:
            return encaminhado.split(",")[-1].strip()
    return request.client.host if request.client else "desconhecido"


def conexao_https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"


# ======================================================= filtro de requisições
def _origem_confere(request: Request) -> bool:
    """Requisições que alteram algo precisam vir do próprio site."""
    if request.headers.get("sec-fetch-site") == "cross-site":
        return False
    origem = request.headers.get("origin") or request.headers.get("referer")
    if not origem:
        return True  # ferramentas como curl não mandam Origin; o cookie SameSite cobre o navegador
    host = request.headers.get("x-forwarded-host") or request.headers.get("host", "")
    return urlsplit(origem).netloc == host


def instalar_filtro(app: FastAPI, cfg: Config, sessoes: Sessoes) -> None:
    @app.middleware("http")
    async def filtro(request: Request, chamar_proximo):
        caminho = request.url.path
        eh_api = caminho.startswith("/api/")

        if eh_api:
            tamanho = request.headers.get("content-length", "0")
            if not tamanho.isdigit() or int(tamanho) > TAMANHO_MAXIMO_CORPO:
                return JSONResponse({"detail": "Requisição grande demais."}, status_code=413)

            if request.method in METODOS_QUE_ALTERAM:
                # Formulários de outros sites não conseguem mandar application/json
                # sem uma checagem prévia do navegador (CORS), que este servidor não libera.
                tipo = request.headers.get("content-type", "")
                if not tipo.startswith("application/json"):
                    return JSONResponse({"detail": "Use Content-Type: application/json."}, status_code=415)
                if not _origem_confere(request):
                    return JSONResponse({"detail": "Origem não permitida."}, status_code=403)

            if cfg.exige_login and caminho not in ROTAS_SEM_LOGIN:
                ident = sessoes.identificador(request.cookies.get(COOKIE_SESSAO))
                if ident is None:
                    return JSONResponse({"detail": "Faça login para continuar."}, status_code=401)
                request.state.sessao = ident

        resposta = await chamar_proximo(request)

        cabecalhos = resposta.headers
        cabecalhos["X-Content-Type-Options"] = "nosniff"
        cabecalhos["X-Frame-Options"] = "DENY"
        cabecalhos["Referrer-Policy"] = "no-referrer"
        cabecalhos["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        cabecalhos["Cross-Origin-Opener-Policy"] = "same-origin"
        if not caminho.startswith(("/docs", "/redoc", "/loja/docs", "/loja/redoc")):
            cabecalhos["Content-Security-Policy"] = CSP
        if eh_api:
            cabecalhos["Cache-Control"] = "no-store"
        if conexao_https(request):
            cabecalhos["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return resposta
