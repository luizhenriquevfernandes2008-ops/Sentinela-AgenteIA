"""Plano de controle do agente: auditoria, aprovações, limites e regras.

Tudo o que o agente faz passa por aqui e fica registrado. As regras são
código determinístico: elas valem mesmo que o modelo seja enganado (por
exemplo, por uma instrução maliciosa escondida num pedido).
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable

from .ferramentas import FERRAMENTAS, SENSIVEL, validar_entrada

ESQUEMA = """
CREATE TABLE IF NOT EXISTS execucoes (
    id INTEGER PRIMARY KEY,
    tarefa TEXT NOT NULL,
    status TEXT NOT NULL,
    modo TEXT NOT NULL,
    modelo TEXT NOT NULL,
    passos INTEGER NOT NULL DEFAULT 0,
    tokens_entrada INTEGER NOT NULL DEFAULT 0,
    tokens_saida INTEGER NOT NULL DEFAULT 0,
    custo_usd REAL NOT NULL DEFAULT 0,
    mensagens_json TEXT NOT NULL DEFAULT '[]',
    pendentes_json TEXT NOT NULL DEFAULT '[]',
    resposta_final TEXT NOT NULL DEFAULT '',
    criado_em TEXT NOT NULL,
    atualizado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS eventos (
    id INTEGER PRIMARY KEY,
    execucao_id INTEGER NOT NULL REFERENCES execucoes(id),
    tipo TEXT NOT NULL,
    titulo TEXT NOT NULL,
    dados_json TEXT NOT NULL DEFAULT '{}',
    criado_em TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS aprovacoes (
    id INTEGER PRIMARY KEY,
    execucao_id INTEGER NOT NULL REFERENCES execucoes(id),
    tool_use_id TEXT NOT NULL,
    ferramenta TEXT NOT NULL,
    entrada_json TEXT NOT NULL,
    motivo TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pendente',
    comentario TEXT NOT NULL DEFAULT '',
    criado_em TEXT NOT NULL,
    decidido_em TEXT
);
CREATE TABLE IF NOT EXISTS controles (
    chave TEXT PRIMARY KEY,
    valor_json TEXT NOT NULL
);
"""


def controles_padrao() -> dict[str, Any]:
    return {
        # Botão de emergência: com o agente pausado nada novo roda e execuções
        # em andamento param antes da próxima chamada ao modelo.
        "agente_ativo": True,
        "ferramentas": {
            nome: {"ativa": True, "exige_aprovacao": f.risco == SENSIVEL}
            for nome, f in FERRAMENTAS.items()
        },
        # Reembolsos acima deste valor são bloqueados mesmo com aprovação:
        # precisam ser feitos por uma pessoa, fora do agente.
        "limite_reembolso": 1000.0,
        # E-mails só podem ir para clientes cadastrados (evita vazamento de dados).
        "email_somente_clientes": True,
        # Máximo de chamadas ao modelo por execução (evita loops infinitos).
        "max_passos": 10,
        # Orçamento diário de tokens (entrada + saída) somando todas as execuções.
        "orcamento_tokens_dia": 300_000,
    }


# Faixas aceitas para cada limite (valem sempre, local ou online).
FAIXAS = {
    "limite_reembolso": (0.0, 100_000.0),
    "max_passos": (1, 50),
    "orcamento_tokens_dia": (0, 10_000_000),
}
BOOLEANOS = ("agente_ativo", "email_somente_clientes")

# Tamanho máximo dos textos que o agente pode mandar para a loja.
LIMITES_TEXTO = {"busca": 100, "motivo": 500, "para": 254, "assunto": 200, "corpo": 5000}
EMAIL_VALIDO = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,185}\.[A-Za-z]{2,24}$")


class ControleInvalido(ValueError):
    pass


def _agora() -> str:
    return datetime.now().isoformat(timespec="seconds")


@dataclass
class Decisao:
    acao: str  # "permitir" | "aprovar" | "bloquear"
    motivo: str


class Controle:
    def __init__(self, caminho: Path | str, somente_mais_rigido: bool = False):
        """somente_mais_rigido: online, nenhuma regra pode ficar mais frouxa que o padrão."""
        self.caminho = str(caminho)
        self.somente_mais_rigido = somente_mais_rigido
        if self.caminho != ":memory:":
            Path(self.caminho).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.caminho, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._trava = threading.RLock()
        with self._trava:
            self._conn.executescript(ESQUEMA)
            self._conn.commit()

    def resetar(self) -> None:
        """Apaga execuções, eventos e aprovações e volta os controles ao padrão."""
        with self._trava:
            for tabela in ("eventos", "aprovacoes", "execucoes", "controles"):
                self._conn.execute(f"DELETE FROM {tabela}")
            self._conn.commit()

    # ------------------------------------------------------------- controles
    def controles(self) -> dict[str, Any]:
        atuais = controles_padrao()
        with self._trava:
            for linha in self._conn.execute("SELECT chave, valor_json FROM controles"):
                valor = json.loads(linha["valor_json"])
                if linha["chave"] == "ferramentas":
                    for nome, conf in valor.items():
                        if nome in atuais["ferramentas"]:
                            atuais["ferramentas"][nome].update(conf)
                elif linha["chave"] in atuais:
                    atuais[linha["chave"]] = valor
        return atuais

    def atualizar_controles(self, mudancas: Any) -> dict[str, Any]:
        """Valida tudo antes de gravar: tipo, faixa e (online) se ficaria mais frouxo."""
        if not isinstance(mudancas, dict):
            raise ControleInvalido("Envie um objeto JSON com os controles a mudar.")
        atuais = self.controles()
        padrao = controles_padrao()
        for chave, valor in mudancas.items():
            if chave not in atuais:
                raise ControleInvalido(f"Controle desconhecido: {chave}")
            if chave == "ferramentas":
                if not isinstance(valor, dict) or len(valor) > len(FERRAMENTAS):
                    raise ControleInvalido("'ferramentas' deve ser um objeto por nome de ferramenta.")
                for nome, conf in valor.items():
                    if nome not in atuais["ferramentas"] or not isinstance(conf, dict):
                        raise ControleInvalido(f"Ferramenta desconhecida: {nome}")
                    for campo, v in conf.items():
                        if campo not in ("ativa", "exige_aprovacao") or not isinstance(v, bool):
                            raise ControleInvalido(f"{nome}.{campo}: use true ou false.")
                        if (self.somente_mais_rigido and campo == "exige_aprovacao"
                                and padrao["ferramentas"][nome]["exige_aprovacao"] and not v):
                            raise ControleInvalido(
                                f"Na versão online não é possível tirar a aprovação humana de '{nome}'."
                            )
                        atuais["ferramentas"][nome][campo] = v
            elif chave in BOOLEANOS:
                if not isinstance(valor, bool):
                    raise ControleInvalido(f"{chave}: use true ou false.")
                if self.somente_mais_rigido and chave == "email_somente_clientes" and not valor:
                    raise ControleInvalido("Na versão online os e-mails só podem ir para clientes cadastrados.")
                atuais[chave] = valor
            else:
                minimo, maximo = FAIXAS[chave]
                tipo_ok = isinstance(valor, (int, float)) and not isinstance(valor, bool)
                if isinstance(minimo, int) and tipo_ok and float(valor).is_integer():
                    valor = int(valor)
                elif isinstance(minimo, int):
                    tipo_ok = False
                if not tipo_ok or not math.isfinite(valor) or not minimo <= valor <= maximo:
                    raise ControleInvalido(f"{chave}: use um número entre {minimo} e {maximo}.")
                if self.somente_mais_rigido and valor > padrao[chave]:
                    raise ControleInvalido(
                        f"Na versão online {chave} não pode passar de {padrao[chave]} (só dá para deixar mais rígido)."
                    )
                atuais[chave] = valor
        with self._trava:
            for chave, valor in atuais.items():
                self._conn.execute(
                    "INSERT OR REPLACE INTO controles VALUES (?, ?)", (chave, json.dumps(valor))
                )
            self._conn.commit()
        return atuais

    # ----------------------------------------------------------------- regras
    def avaliar(
        self,
        nome: str,
        entrada: Any,
        controles: dict[str, Any],
        eh_cliente: Callable[[str], bool] | None = None,
        consultar_pedido: Callable[[int], dict | None] | None = None,
    ) -> Decisao:
        """Decide se uma chamada de ferramenta roda, espera aprovação ou é bloqueada."""
        ferramenta = FERRAMENTAS.get(nome)
        if ferramenta is None:
            return Decisao("bloquear", f"Ferramenta desconhecida: {nome}.")
        conf = controles["ferramentas"][nome]
        if not conf["ativa"]:
            return Decisao("bloquear", f"A ferramenta '{nome}' está desativada pelo operador.")
        erros = validar_entrada(ferramenta.schema, entrada)
        if erros:
            return Decisao("bloquear", "Entrada inválida: " + "; ".join(erros))
        for campo, limite in LIMITES_TEXTO.items():
            if isinstance(entrada.get(campo), str) and len(entrada[campo]) > limite:
                return Decisao("bloquear", f"'{campo}' passa do tamanho máximo de {limite} caracteres.")
        if nome == "reembolsar_pedido":
            valor, limite = entrada["valor"], controles["limite_reembolso"]
            if valor <= 0:
                return Decisao("bloquear", "O valor do reembolso precisa ser positivo.")
            if valor > limite:
                return Decisao(
                    "bloquear",
                    f"Reembolso de R$ {valor:.2f} passa do limite do agente "
                    f"(R$ {limite:.2f}). Precisa ser feito por uma pessoa.",
                )
            if consultar_pedido is not None:
                # O limite vale para a soma dos reembolsos do pedido: não dá para
                # contornar pedindo vários reembolsos pequenos.
                pedido = consultar_pedido(entrada["pedido_id"])
                if pedido is None:
                    return Decisao("bloquear", "Não foi possível conferir os reembolsos anteriores deste pedido.")
                total = pedido["total_reembolsado"] + valor
                if total > limite:
                    return Decisao(
                        "bloquear",
                        f"Somando os reembolsos anteriores, o pedido chegaria a R$ {total:.2f}, acima do "
                        f"limite do agente (R$ {limite:.2f}). Precisa ser feito por uma pessoa.",
                    )
        if nome == "enviar_email" and not EMAIL_VALIDO.match(entrada["para"]):
            return Decisao("bloquear", f"Endereço de e-mail inválido: {entrada['para']!r}.")
        if nome == "enviar_email" and controles["email_somente_clientes"] and eh_cliente is not None:
            if not eh_cliente(entrada["para"]):
                return Decisao("bloquear", f"'{entrada['para']}' não é e-mail de um cliente cadastrado.")
        if conf["exige_aprovacao"]:
            return Decisao("aprovar", f"'{nome}' é uma ação sensível e exige aprovação humana.")
        return Decisao("permitir", "Permitido pelas regras.")

    # --------------------------------------------------------------- execuções
    def criar_execucao(self, tarefa: str, modo: str, modelo: str) -> int:
        with self._trava:
            cur = self._conn.execute(
                "INSERT INTO execucoes (tarefa, status, modo, modelo, criado_em, atualizado_em) "
                "VALUES (?, 'executando', ?, ?, ?, ?)",
                (tarefa, modo, modelo, _agora(), _agora()),
            )
            self._conn.commit()
            return cur.lastrowid

    def execucao(self, execucao_id: int) -> dict[str, Any]:
        with self._trava:
            linha = self._conn.execute("SELECT * FROM execucoes WHERE id = ?", (execucao_id,)).fetchone()
        if not linha:
            raise KeyError(execucao_id)
        dados = dict(linha)
        dados["mensagens"] = json.loads(dados.pop("mensagens_json"))
        dados["pendentes"] = json.loads(dados.pop("pendentes_json"))
        return dados

    def atualizar_execucao(self, execucao_id: int, **campos: Any) -> None:
        if "mensagens" in campos:
            campos["mensagens_json"] = json.dumps(campos.pop("mensagens"), ensure_ascii=False)
        if "pendentes" in campos:
            campos["pendentes_json"] = json.dumps(campos.pop("pendentes"), ensure_ascii=False)
        campos["atualizado_em"] = _agora()
        colunas = ", ".join(f"{c} = ?" for c in campos)
        with self._trava:
            self._conn.execute(
                f"UPDATE execucoes SET {colunas} WHERE id = ?", [*campos.values(), execucao_id]
            )
            self._conn.commit()

    def somar_uso(self, execucao_id: int, entrada: int, saida: int, custo: float) -> None:
        with self._trava:
            self._conn.execute(
                "UPDATE execucoes SET tokens_entrada = tokens_entrada + ?, tokens_saida = tokens_saida + ?, "
                "custo_usd = custo_usd + ?, passos = passos + 1, atualizado_em = ? WHERE id = ?",
                (entrada, saida, custo, _agora(), execucao_id),
            )
            self._conn.commit()

    def listar_execucoes(self, limite: int = 50) -> list[dict[str, Any]]:
        with self._trava:
            linhas = self._conn.execute(
                "SELECT id, tarefa, status, modo, modelo, passos, tokens_entrada, tokens_saida, custo_usd, "
                "resposta_final, criado_em, atualizado_em FROM execucoes ORDER BY id DESC LIMIT ?",
                (limite,),
            ).fetchall()
        return [dict(l) for l in linhas]

    def tokens_hoje(self) -> int:
        with self._trava:
            linha = self._conn.execute(
                "SELECT COALESCE(SUM(tokens_entrada + tokens_saida), 0) FROM execucoes WHERE criado_em >= ?",
                (date.today().isoformat(),),
            ).fetchone()
        return int(linha[0])

    def custo_hoje(self) -> float:
        with self._trava:
            linha = self._conn.execute(
                "SELECT COALESCE(SUM(custo_usd), 0) FROM execucoes WHERE criado_em >= ?",
                (date.today().isoformat(),),
            ).fetchone()
        return float(linha[0])

    # ----------------------------------------------------------------- eventos
    def registrar(self, execucao_id: int, tipo: str, titulo: str, dados: Any = None) -> None:
        with self._trava:
            self._conn.execute(
                "INSERT INTO eventos (execucao_id, tipo, titulo, dados_json, criado_em) VALUES (?, ?, ?, ?, ?)",
                (execucao_id, tipo, titulo, json.dumps(dados or {}, ensure_ascii=False), _agora()),
            )
            self._conn.commit()

    def eventos(self, execucao_id: int) -> list[dict[str, Any]]:
        with self._trava:
            linhas = self._conn.execute(
                "SELECT * FROM eventos WHERE execucao_id = ? ORDER BY id", (execucao_id,)
            ).fetchall()
        return [{**dict(l), "dados": json.loads(l["dados_json"])} for l in linhas]

    # -------------------------------------------------------------- aprovações
    def criar_aprovacao(self, execucao_id: int, tool_use_id: str, ferramenta: str, entrada: Any, motivo: str) -> int:
        with self._trava:
            cur = self._conn.execute(
                "INSERT INTO aprovacoes (execucao_id, tool_use_id, ferramenta, entrada_json, motivo, criado_em) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (execucao_id, tool_use_id, ferramenta, json.dumps(entrada, ensure_ascii=False), motivo, _agora()),
            )
            self._conn.commit()
            return cur.lastrowid

    def aprovacao(self, aprovacao_id: int) -> dict[str, Any]:
        with self._trava:
            linha = self._conn.execute("SELECT * FROM aprovacoes WHERE id = ?", (aprovacao_id,)).fetchone()
        if not linha:
            raise KeyError(aprovacao_id)
        return {**dict(linha), "entrada": json.loads(linha["entrada_json"])}

    def listar_aprovacoes(self, status: str | None = None, execucao_id: int | None = None) -> list[dict[str, Any]]:
        sql, args = "SELECT * FROM aprovacoes WHERE 1=1", []
        if status:
            sql += " AND status = ?"
            args.append(status)
        if execucao_id is not None:
            sql += " AND execucao_id = ?"
            args.append(execucao_id)
        with self._trava:
            linhas = self._conn.execute(sql + " ORDER BY id", args).fetchall()
        return [{**dict(l), "entrada": json.loads(l["entrada_json"])} for l in linhas]

    def decidir_aprovacao(self, aprovacao_id: int, aprovado: bool, comentario: str = "") -> bool:
        """Grava a decisão. Retorna False se a aprovação já tinha sido decidida."""
        with self._trava:
            cur = self._conn.execute(
                "UPDATE aprovacoes SET status = ?, comentario = ?, decidido_em = ? "
                "WHERE id = ? AND status = 'pendente'",
                ("aprovada" if aprovado else "rejeitada", comentario, _agora(), aprovacao_id),
            )
            self._conn.commit()
            return cur.rowcount == 1

    # ---------------------------------------------------------------- métricas
    def metricas(self) -> dict[str, Any]:
        with self._trava:
            c = self._conn
            por_status = dict(c.execute("SELECT status, COUNT(*) FROM execucoes GROUP BY status").fetchall())
            por_tipo = dict(c.execute("SELECT tipo, COUNT(*) FROM eventos GROUP BY tipo").fetchall())
            totais = c.execute(
                "SELECT COUNT(*), COALESCE(SUM(tokens_entrada), 0), COALESCE(SUM(tokens_saida), 0), "
                "COALESCE(SUM(custo_usd), 0) FROM execucoes"
            ).fetchone()
            pendentes = c.execute("SELECT COUNT(*) FROM aprovacoes WHERE status = 'pendente'").fetchone()[0]
            uso_ferramentas = dict(
                c.execute(
                    "SELECT json_extract(dados_json, '$.ferramenta'), COUNT(*) FROM eventos "
                    "WHERE tipo = 'ferramenta_executada' GROUP BY 1"
                ).fetchall()
            )
        return {
            "execucoes": totais[0],
            "execucoes_por_status": por_status,
            "acoes_executadas": por_tipo.get("ferramenta_executada", 0),
            "acoes_bloqueadas": por_tipo.get("bloqueio", 0),
            "aprovacoes_pendentes": pendentes,
            "aprovadas": por_tipo.get("aprovada", 0),
            "rejeitadas": por_tipo.get("rejeitada", 0),
            "tokens_entrada": totais[1],
            "tokens_saida": totais[2],
            "custo_usd": round(totais[3], 4),
            "tokens_hoje": self.tokens_hoje(),
            "uso_por_ferramenta": uso_ferramentas,
        }
