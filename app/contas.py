"""Contas do painel: um administrador e quantos visitantes quiserem.

- O administrador é criado no primeiro acesso, com um código de uso único que
  o servidor imprime no log (só quem tem acesso ao servidor vê).
- Visitantes criam a própria conta e podem usar a demo, sem mexer nas regras.
- Senhas nunca são guardadas: só um hash scrypt com sal aleatório.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
import threading
from datetime import datetime
from pathlib import Path

ADMIN = "admin"
VISITANTE = "visitante"
NOME_ADMIN = "admin"
NOME_VALIDO = re.compile(r"[a-z0-9_-]{3,30}")
MINIMO_SENHA_ADMIN = 12
MINIMO_SENHA_VISITANTE = 8
MAXIMO_CONTAS = 500

ESQUEMA = """
CREATE TABLE IF NOT EXISTS contas (
    nome TEXT PRIMARY KEY,
    papel TEXT NOT NULL,
    sal BLOB NOT NULL,
    hash BLOB NOT NULL,
    criado_em TEXT NOT NULL
);
"""


class ErroConta(ValueError):
    pass


def _hash(senha: str, sal: bytes) -> bytes:
    # scrypt é lento de propósito: dificulta adivinhar senhas mesmo se o banco vazar.
    return hashlib.scrypt(senha.encode(), salt=sal, n=2**14, r=8, p=1, dklen=32)


def _gerar_codigo() -> str:
    alfabeto = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # sem 0/O e 1/I, para não confundir
    texto = "".join(secrets.choice(alfabeto) for _ in range(12))
    return f"{texto[:4]}-{texto[4:8]}-{texto[8:]}"


class Contas:
    def __init__(self, caminho: Path | str, senha_env: str = ""):
        self.caminho = str(caminho)
        if self.caminho != ":memory:":
            Path(self.caminho).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.caminho, check_same_thread=False)
        self._trava = threading.RLock()
        with self._trava:
            self._conn.executescript(ESQUEMA)
            self._conn.commit()
        # SENHA_OPERADOR (variável de ambiente) continua valendo como senha de administrador.
        self.senha_env = senha_env
        self.codigo_configuracao: str | None = None if self.admin_configurado() else _gerar_codigo()

    # ------------------------------------------------------------ consultas
    def _linha(self, nome: str) -> tuple | None:
        with self._trava:
            return self._conn.execute(
                "SELECT nome, papel, sal, hash FROM contas WHERE nome = ?", (nome,)
            ).fetchone()

    def admin_configurado(self) -> bool:
        with self._trava:
            return self._conn.execute("SELECT 1 FROM contas WHERE papel = ?", (ADMIN,)).fetchone() is not None

    def existe(self, nome: str, papel: str) -> bool:
        if papel == ADMIN and nome == NOME_ADMIN and self.senha_env:
            return True
        linha = self._linha(nome)
        return linha is not None and linha[1] == papel

    def total(self) -> int:
        with self._trava:
            return self._conn.execute("SELECT COUNT(*) FROM contas").fetchone()[0]

    # ------------------------------------------------------------- criação
    def _inserir(self, nome: str, papel: str, senha: str) -> None:
        sal = secrets.token_bytes(16)
        with self._trava:
            try:
                self._conn.execute(
                    "INSERT INTO contas VALUES (?, ?, ?, ?, ?)",
                    (nome, papel, sal, _hash(senha, sal), datetime.now().isoformat(timespec="seconds")),
                )
            except sqlite3.IntegrityError:
                raise ErroConta("Esse nome de usuário já existe. Escolha outro.")
            self._conn.commit()

    def configurar_admin(self, codigo: str, senha: str) -> str:
        if self.codigo_configuracao is None or self.admin_configurado():
            raise ErroConta("O administrador já foi configurado. Entre com a senha dele.")
        if not hmac.compare_digest(codigo.strip().upper().encode(), self.codigo_configuracao.encode()):
            raise ErroConta("Código incorreto. Ele aparece no log do servidor (no Render: Logs).")
        if len(senha) < MINIMO_SENHA_ADMIN:
            raise ErroConta(f"A senha do administrador precisa ter pelo menos {MINIMO_SENHA_ADMIN} caracteres.")
        self._inserir(NOME_ADMIN, ADMIN, senha)
        self.codigo_configuracao = None  # uso único
        return NOME_ADMIN

    def criar_visitante(self, nome: str, senha: str) -> str:
        nome = nome.strip().lower()
        if not NOME_VALIDO.fullmatch(nome):
            raise ErroConta("Use de 3 a 30 caracteres: letras minúsculas, números, _ ou -.")
        if nome == NOME_ADMIN:
            raise ErroConta("Esse nome é reservado. Escolha outro.")
        if len(senha) < MINIMO_SENHA_VISITANTE:
            raise ErroConta(f"A senha precisa ter pelo menos {MINIMO_SENHA_VISITANTE} caracteres.")
        if len(senha) > 200:
            raise ErroConta("Senha longa demais.")
        if self.total() >= MAXIMO_CONTAS:
            raise ErroConta("Limite de contas atingido. Tente mais tarde.")
        self._inserir(nome, VISITANTE, senha)
        return nome

    # ---------------------------------------------------------------- login
    def verificar(self, nome: str, senha: str) -> str | None:
        """Devolve o papel da conta se a senha estiver certa, senão None."""
        nome = (nome or NOME_ADMIN).strip().lower()
        if nome == NOME_ADMIN and self.senha_env and hmac.compare_digest(senha.encode(), self.senha_env.encode()):
            return ADMIN
        linha = self._linha(nome)
        if linha is None:
            _hash(senha, b"\0" * 16)  # mesmo tempo de resposta com ou sem conta: não revela quem existe
            return None
        _, papel, sal, esperado = linha
        return papel if hmac.compare_digest(_hash(senha, sal), esperado) else None
