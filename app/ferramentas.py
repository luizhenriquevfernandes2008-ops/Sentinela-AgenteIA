"""Ferramentas que o agente pode usar e o cliente HTTP que as executa na loja.

Cada ferramenta tem:
- um schema JSON (o que o modelo vê e preenche);
- um nível de risco (leitura, escrita ou sensível), usado pelos controles;
- uma função que chama a API REST da loja.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

import httpx

LEITURA = "leitura"
ESCRITA = "escrita"
SENSIVEL = "sensivel"


def _schema(propriedades: dict, obrigatorios: list[str] | None = None) -> dict:
    return {
        "type": "object",
        "properties": propriedades,
        "required": obrigatorios if obrigatorios is not None else list(propriedades),
        "additionalProperties": False,
    }


@dataclass(frozen=True)
class Ferramenta:
    nome: str
    descricao: str
    schema: dict
    risco: str

    def definicao_api(self) -> dict:
        return {
            "name": self.nome,
            "description": self.descricao,
            "input_schema": self.schema,
            "strict": True,
        }


FERRAMENTAS: dict[str, Ferramenta] = {
    f.nome: f
    for f in [
        Ferramenta(
            "buscar_produtos",
            "Lista produtos da loja com preço e estoque atual. Use busca vazia para listar todos.",
            _schema({"busca": {"type": "string", "description": "Trecho do nome ou SKU. Pode ser vazio."}}),
            LEITURA,
        ),
        Ferramenta(
            "buscar_clientes",
            "Procura clientes por nome ou e-mail. Use busca vazia para listar todos.",
            _schema({"busca": {"type": "string", "description": "Trecho do nome ou e-mail. Pode ser vazio."}}),
            LEITURA,
        ),
        Ferramenta(
            "consultar_pedido",
            "Retorna um pedido completo: status, itens, total, valor já reembolsado, observações e dados do cliente.",
            _schema({"pedido_id": {"type": "integer"}}),
            LEITURA,
        ),
        Ferramenta(
            "listar_pedidos_cliente",
            "Lista todos os pedidos de um cliente.",
            _schema({"cliente_id": {"type": "integer"}}),
            LEITURA,
        ),
        Ferramenta(
            "criar_pedido",
            "Cria um pedido novo (status 'pendente') e baixa o estoque. Falha se não houver estoque.",
            _schema(
                {
                    "cliente_id": {"type": "integer"},
                    "itens": {
                        "type": "array",
                        "items": _schema(
                            {"produto_id": {"type": "integer"}, "quantidade": {"type": "integer"}}
                        ),
                    },
                }
            ),
            ESCRITA,
        ),
        Ferramenta(
            "cancelar_pedido",
            "Cancela um pedido 'pendente' ou 'pago' e devolve os itens ao estoque. "
            "Não faz reembolso: se o pedido estava pago, reembolse separadamente.",
            _schema({"pedido_id": {"type": "integer"}, "motivo": {"type": "string"}}),
            SENSIVEL,
        ),
        Ferramenta(
            "reembolsar_pedido",
            "Registra um reembolso (total ou parcial) para um pedido. O valor não pode passar do saldo reembolsável.",
            _schema(
                {
                    "pedido_id": {"type": "integer"},
                    "valor": {"type": "number", "description": "Valor em reais."},
                    "motivo": {"type": "string"},
                }
            ),
            SENSIVEL,
        ),
        Ferramenta(
            "enviar_email",
            "Envia um e-mail a um cliente. Use para confirmar ao cliente o que foi feito.",
            _schema({"para": {"type": "string"}, "assunto": {"type": "string"}, "corpo": {"type": "string"}}),
            SENSIVEL,
        ),
    ]
}


def definicoes_api(nomes: list[str] | None = None) -> list[dict]:
    """Definições no formato da API do Claude, em ordem estável (bom para cache de prompt)."""
    nomes = nomes if nomes is not None else list(FERRAMENTAS)
    return [FERRAMENTAS[n].definicao_api() for n in FERRAMENTAS if n in nomes]


_TIPOS = {"string": str, "integer": int, "number": (int, float), "array": list, "object": dict}


def validar_entrada(schema: dict, valor: Any, caminho: str = "entrada") -> list[str]:
    """Validação simples do JSON schema. O modo real já usa strict=True, mas
    validar de novo no servidor é barato e protege contra qualquer origem."""
    erros: list[str] = []
    tipo = schema.get("type")
    esperado = _TIPOS.get(tipo)
    if esperado and (not isinstance(valor, esperado) or (tipo in ("integer", "number") and isinstance(valor, bool))):
        return [f"{caminho}: esperado {tipo}, recebido {type(valor).__name__}"]
    if tipo == "object":
        for campo in schema.get("required", []):
            if campo not in valor:
                erros.append(f"{caminho}.{campo}: obrigatório")
        if schema.get("additionalProperties") is False:
            for campo in valor:
                if campo not in schema.get("properties", {}):
                    erros.append(f"{caminho}.{campo}: campo não permitido")
        for campo, sub in schema.get("properties", {}).items():
            if campo in valor:
                erros += validar_entrada(sub, valor[campo], f"{caminho}.{campo}")
    if tipo == "array" and "items" in schema:
        for i, item in enumerate(valor):
            erros += validar_entrada(schema["items"], item, f"{caminho}[{i}]")
    return erros


class ErroFerramenta(Exception):
    pass


class ClienteLoja:
    """Cliente HTTP da API da loja. Recebe um httpx.Client pronto para permitir testes."""

    def __init__(self, http: httpx.Client, api_key: str):
        self.http = http
        self.api_key = api_key

    def _chamar(self, metodo: str, caminho: str, **kwargs) -> Any:
        try:
            resp = self.http.request(metodo, caminho, headers={"X-API-Key": self.api_key}, **kwargs)
        except httpx.HTTPError as erro:
            raise ErroFerramenta(f"Falha de comunicação com a loja: {erro}") from erro
        if resp.status_code >= 400:
            try:
                detalhe = resp.json().get("detail", resp.text)
            except ValueError:
                detalhe = resp.text
            raise ErroFerramenta(f"Loja respondeu {resp.status_code}: {detalhe}")
        return resp.json()

    # Cada método abaixo corresponde a uma ferramenta.
    def buscar_produtos(self, busca: str) -> Any:
        return self._chamar("GET", "/produtos", params={"busca": busca})

    def buscar_clientes(self, busca: str) -> Any:
        return self._chamar("GET", "/clientes", params={"busca": busca})

    def consultar_pedido(self, pedido_id: int) -> Any:
        return self._chamar("GET", f"/pedidos/{pedido_id}")

    def listar_pedidos_cliente(self, cliente_id: int) -> Any:
        return self._chamar("GET", "/pedidos", params={"cliente_id": cliente_id})

    def criar_pedido(self, cliente_id: int, itens: list[dict]) -> Any:
        return self._chamar("POST", "/pedidos", json={"cliente_id": cliente_id, "itens": itens})

    def cancelar_pedido(self, pedido_id: int, motivo: str) -> Any:
        return self._chamar("POST", f"/pedidos/{pedido_id}/cancelar", json={"motivo": motivo})

    def reembolsar_pedido(self, pedido_id: int, valor: float, motivo: str) -> Any:
        return self._chamar("POST", f"/pedidos/{pedido_id}/reembolso", json={"valor": valor, "motivo": motivo})

    def enviar_email(self, para: str, assunto: str, corpo: str) -> Any:
        return self._chamar("POST", "/emails", json={"para": para, "assunto": assunto, "corpo": corpo})

    def executar(self, nome: str, entrada: dict) -> str:
        """Executa a ferramenta e devolve o resultado como texto JSON para o modelo."""
        funcao: Callable[..., Any] | None = getattr(self, nome, None) if nome in FERRAMENTAS else None
        if funcao is None:
            raise ErroFerramenta(f"Ferramenta desconhecida: {nome}")
        return json.dumps(funcao(**entrada), ensure_ascii=False)
