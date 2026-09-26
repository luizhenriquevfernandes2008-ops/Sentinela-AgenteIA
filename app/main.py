"""Servidor: API da loja (/loja), API do agente e do painel (/api) e o painel (/)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import hmac
import re
from typing import Literal

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from .agente import Agente, AgentePausado
from .cerebro import Cerebro, CerebroClaude, CerebroCompativel, CerebroSimulado, ErroProvedor
from .config import PROVEDORES, Config
from .contas import ADMIN, Contas, ErroConta
from .controle import Controle, ControleInvalido
from .ferramentas import FERRAMENTAS, ClienteLoja
from .loja import Loja
from .loja_api import criar_app_loja
from .seguranca import COOKIE_SESSAO, Limitador, Sessoes, conexao_https, instalar_filtro, ip_do_cliente

ESTATICOS = Path(__file__).resolve().parent.parent / "static"


class NovaTarefa(BaseModel):
    tarefa: str = Field(min_length=1, max_length=2000)


class Login(BaseModel):
    usuario: str = Field(default="", max_length=60)
    senha: str = Field(min_length=1, max_length=200)


class NovaConta(BaseModel):
    usuario: str = Field(min_length=1, max_length=60)
    senha: str = Field(min_length=1, max_length=200)


class ConfigurarAdmin(BaseModel):
    codigo: str = Field(min_length=1, max_length=40)
    senha: str = Field(min_length=1, max_length=200)


class AjusteIA(BaseModel):
    modo: Literal["simulado", "groq"]
    chave: str | None = Field(default=None, max_length=300)


CHAVE_GROQ = re.compile(r"gsk_[A-Za-z0-9_-]{20,200}")


class Decisao(BaseModel):
    aprovar: bool
    comentario: str = Field(default="", max_length=500)


def criar_app(
    cfg: Config | None = None,
    http_loja: httpx.Client | None = None,
    cerebro: Cerebro | None = None,
    http_ia: httpx.Client | None = None,
) -> FastAPI:
    cfg = cfg or Config.do_ambiente()
    problemas = cfg.problemas_de_publicacao()
    if problemas:
        # Melhor não subir do que subir aberto na internet.
        raise RuntimeError("Configuração insegura para publicar:\n- " + "\n- ".join(problemas))

    loja = Loja(cfg.banco_loja)
    controle = Controle(cfg.banco_controle, somente_mais_rigido=cfg.publico)
    app_loja = criar_app_loja(loja, cfg.loja_api_key, documentacao=not cfg.publico)

    if cfg.modo == "real":  # nome antigo do modo Claude
        cfg.modo = "claude"

    def montar_cerebro(modo_ia: str, chave: str = "") -> Cerebro:
        if modo_ia == "claude":
            return CerebroClaude(cfg)
        if modo_ia == "groq":
            return CerebroCompativel(cfg, cfg.groq_url, chave, cfg.groq_modelo, http=http_ia)
        return CerebroSimulado()

    # Estado da IA em uso. Pode mudar com o servidor ligado (aba Controles).
    ia: dict[str, Any] = {}

    def descrever_ia(modo_ia: str, origem: str) -> tuple[str, str, bool]:
        info = PROVEDORES.get(modo_ia, {"nome": "Simulado", "gratuito": True})
        real = modo_ia in ("claude", "groq")
        modelo = {"claude": cfg.modelo, "groq": cfg.groq_modelo}.get(modo_ia, "simulado")
        ia.update(
            modo_ia=modo_ia, modo="real" if real else "simulado", provedor=info["nome"],
            gratuito=info["gratuito"], modelo=modelo, origem=origem,
        )
        return ia["modo"], modelo, info["gratuito"]

    # Prioridade: um cérebro passado pelo código (testes) > o que o operador
    # salvou no painel > as variáveis de ambiente.
    salvo = controle.ler_ajuste("ia") or {}
    if cerebro is not None:
        modo_ia, origem = ("simulado" if isinstance(cerebro, CerebroSimulado) else cfg.modo), "servidor"
    elif salvo.get("modo") == "groq" and salvo.get("chave"):
        modo_ia, origem = "groq", "painel"
        cerebro = montar_cerebro("groq", salvo["chave"])
    elif salvo.get("modo") == "simulado":
        modo_ia, origem = "simulado", "painel"
        cerebro = CerebroSimulado()
    else:
        modo_ia, origem = cfg.modo, "servidor"
        cerebro = montar_cerebro(cfg.modo, cfg.groq_api_key)
    modo, modelo, gratuito = descrever_ia(modo_ia, origem)

    # O agente fala com a loja por HTTP, como falaria com um sistema externo.
    cliente_loja = ClienteLoja(http_loja or httpx.Client(base_url=cfg.loja_url, timeout=15), cfg.loja_api_key)
    agente = Agente(cfg, controle, cliente_loja, cerebro, modo)
    agente.trocar_cerebro(cerebro, modo, modelo, gratuito)

    def aplicar_ia(modo_ia: str, novo: Cerebro, origem: str) -> None:
        agente.trocar_cerebro(novo, *descrever_ia(modo_ia, origem))

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

    contas = Contas(Path(cfg.banco_controle).with_name("contas.db"), senha_env=cfg.senha_operador)
    app.state.contas = contas
    sessoes = Sessoes(cfg.segredo_sessao, cfg.horas_sessao)
    instalar_filtro(app, cfg, sessoes, contas.existe)
    # Criação de contas: 3 por hora por IP e 30 por hora no total.
    contas_por_ip = Limitador(3, 3600)
    contas_no_total = Limitador(30, 3600)
    # Força bruta na senha: 5 erros por IP ou 30 no total a cada 15 minutos.
    erros_por_ip = Limitador(5, 15 * 60)
    erros_no_total = Limitador(30, 15 * 60)
    # Enxurrada de tarefas: 20 por minuto por pessoa e 60 no total.
    tarefas_por_pessoa = Limitador(20, 60)
    tarefas_no_total = Limitador(60, 60)
    # Cada validação de chave consulta o Groq: no máximo 10 por minuto.
    validacoes_de_chave = Limitador(10, 60)

    # ------------------------------------------------------------------ saúde
    @app.get("/api/saude")
    def saude() -> dict[str, bool]:
        """Usada pelo Render para saber se o servidor está no ar. Não expõe nenhum dado."""
        return {"ok": True}

    # ------------------------------------------------------------------ login
    def sessao_atual(request: Request):
        sessao = sessoes.ler(request.cookies.get(COOKIE_SESSAO))
        if sessao is None or not contas.existe(sessao.usuario, sessao.papel):
            return None
        return sessao

    def entrar(response: Response, request: Request, papel: str, usuario: str) -> dict[str, Any]:
        token, duracao = sessoes.criar(papel, usuario)
        response.set_cookie(
            COOKIE_SESSAO, token, max_age=duracao, httponly=True, samesite="strict",
            secure=conexao_https(request), path="/",
        )
        return {"autenticado": True, "papel": papel, "usuario": usuario}

    def contra_forca_bruta(request: Request) -> str:
        ip = ip_do_cliente(request, cfg)
        if erros_por_ip.bloqueado(ip) or erros_no_total.bloqueado("todos"):
            raise HTTPException(429, "Muitas tentativas erradas. Espere 15 minutos e tente de novo.")
        return ip

    def registrar_erro(ip: str) -> None:
        erros_por_ip.registrar(ip)
        erros_no_total.registrar("todos")

    @app.get("/api/sessao")
    def sessao(request: Request) -> dict[str, Any]:
        if not cfg.exige_login:
            return {"login_necessario": False, "autenticado": True, "papel": ADMIN, "usuario": "",
                    "admin_configurado": True}
        atual = sessao_atual(request)
        return {
            "login_necessario": True,
            "autenticado": atual is not None,
            "papel": atual.papel if atual else None,
            "usuario": atual.usuario if atual else None,
            "admin_configurado": contas.admin_configurado(),
        }

    @app.post("/api/login")
    def login(dados: Login, request: Request, response: Response) -> dict[str, Any]:
        if not cfg.exige_login:
            return {"autenticado": True, "papel": ADMIN, "usuario": ""}
        ip = contra_forca_bruta(request)
        usuario = (dados.usuario or "admin").strip().lower()
        papel = contas.verificar(usuario, dados.senha)
        if papel is None:
            registrar_erro(ip)
            raise HTTPException(401, "Usuário ou senha incorretos.")
        return entrar(response, request, papel, usuario)

    @app.post("/api/contas", status_code=201)
    def criar_conta(dados: NovaConta, request: Request, response: Response) -> dict[str, Any]:
        if not cfg.exige_login:
            raise HTTPException(400, "Rodando no seu computador não há contas: o painel já está liberado.")
        ip = ip_do_cliente(request, cfg)
        if not contas_no_total.tentar("todos") or not contas_por_ip.tentar(ip):
            raise HTTPException(429, "Muitas contas criadas agora. Tente mais tarde.")
        try:
            usuario = contas.criar_visitante(dados.usuario, dados.senha)
        except ErroConta as erro:
            raise HTTPException(400, str(erro))
        return entrar(response, request, "visitante", usuario)

    @app.post("/api/admin/configurar", status_code=201)
    def configurar_admin(dados: ConfigurarAdmin, request: Request, response: Response) -> dict[str, Any]:
        if not cfg.exige_login:
            raise HTTPException(400, "Rodando no seu computador não há login.")
        ip = contra_forca_bruta(request)
        try:
            usuario = contas.configurar_admin(dados.codigo, dados.senha)
        except ErroConta as erro:
            if "Código" in str(erro):
                registrar_erro(ip)
            raise HTTPException(400, str(erro))
        print("Administrador configurado pelo painel.")
        return entrar(response, request, ADMIN, usuario)

    def so_admin(request: Request) -> None:
        if cfg.exige_login and getattr(request.state, "papel", None) != ADMIN:
            raise HTTPException(403, "Só o administrador pode fazer isso.")

    @app.post("/api/logout")
    def logout(request: Request, response: Response) -> dict[str, bool]:
        sessoes.revogar(request.cookies.get(COOKIE_SESSAO))
        response.delete_cookie(COOKIE_SESSAO, path="/")
        return {"autenticado": False}

    # ----------------------------------------------------------------- status
    @app.get("/api/status")
    def status(request: Request) -> dict[str, Any]:
        # Sem login, só diz que está no ar (o Render usa isso para saber se o deploy subiu).
        if cfg.exige_login and sessao_atual(request) is None:
            return {"ok": True}
        return {
            "ok": True,
            "modo": ia["modo"],
            "provedor": ia["provedor"],
            "gratuito": ia["gratuito"],
            "modelo": ia["modelo"],
            "aviso": cfg.aviso_modo if ia["origem"] == "servidor" else "",
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
            eid = agente.iniciar(dados.tarefa, autor=getattr(request.state, "usuario", ""))
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
    def decidir(aprovacao_id: int, dados: Decisao, request: Request, background: BackgroundTasks) -> dict[str, Any]:
        try:
            eid, continuar = agente.decidir(
                aprovacao_id, dados.aprovar, dados.comentario, por=getattr(request.state, "usuario", "")
            )
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
    def atualizar_controles(mudancas: dict[str, Any], request: Request) -> dict[str, Any]:
        so_admin(request)
        try:
            return controle.atualizar_controles(mudancas)
        except (ControleInvalido, ValueError, TypeError) as erro:
            raise HTTPException(400, str(erro))

    @app.get("/api/metricas")
    def metricas() -> dict[str, Any]:
        return {**controle.metricas(), "modo": ia["modo"], "provedor": ia["provedor"], "gratuito": ia["gratuito"]}

    # ------------------------------------------------------ IA (aba Controles)
    def chave_salva() -> str:
        return (controle.ler_ajuste("ia") or {}).get("chave", "")

    def resumo_ia() -> dict[str, Any]:
        chave = chave_salva() or cfg.groq_api_key
        return {
            "modo": ia["modo_ia"],
            "provedor": ia["provedor"],
            "modelo": ia["modelo"],
            "gratuito": ia["gratuito"],
            # A chave nunca volta para o navegador: só os 4 últimos caracteres.
            "chave_configurada": bool(chave),
            "chave_final": chave[-4:] if chave else "",
            "chave_pelo_painel": bool(chave_salva()),
            # No Render grátis o disco é apagado quando o site hiberna ou reinicia.
            "guardada_temporariamente": cfg.publico,
        }

    @app.get("/api/ia")
    def obter_ia(request: Request) -> dict[str, Any]:
        so_admin(request)
        return resumo_ia()

    @app.put("/api/ia")
    def ajustar_ia(dados: AjusteIA, request: Request) -> dict[str, Any]:
        so_admin(request)
        if dados.modo == "simulado":
            controle.gravar_ajuste("ia", {"modo": "simulado", "chave": chave_salva()})
            aplicar_ia("simulado", CerebroSimulado(), "painel")
            return resumo_ia()

        chave = (dados.chave or "").strip() or chave_salva() or cfg.groq_api_key
        if not chave:
            raise HTTPException(400, "Cole a chave do Groq para ligar a IA.")
        if not CHAVE_GROQ.fullmatch(chave):
            raise HTTPException(400, "Isso não parece uma chave do Groq (ela começa com gsk_).")
        pessoa = getattr(request.state, "sessao", None) or ip_do_cliente(request, cfg)
        if not validacoes_de_chave.tentar(pessoa):
            raise HTTPException(429, "Muitas tentativas. Espere um minuto.")
        novo = montar_cerebro("groq", chave)
        try:
            novo.validar()
        except ErroProvedor as erro:
            raise HTTPException(400, str(erro))
        controle.gravar_ajuste("ia", {"modo": "groq", "chave": chave})
        aplicar_ia("groq", novo, "painel")
        print("IA ligada pelo painel: Groq ·", cfg.groq_modelo)  # sem imprimir a chave
        return resumo_ia()

    @app.delete("/api/ia/chave")
    def remover_chave(request: Request) -> dict[str, Any]:
        so_admin(request)
        controle.apagar_ajuste("ia")
        # Volta ao que as variáveis de ambiente dizem (simulado, se não houver chave lá).
        aplicar_ia(cfg.modo, montar_cerebro(cfg.modo, cfg.groq_api_key), "servidor")
        return resumo_ia()

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
    def resetar_demo(request: Request) -> dict[str, str]:
        so_admin(request)
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
