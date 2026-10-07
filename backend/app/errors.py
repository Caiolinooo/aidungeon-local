"""Erros de domínio da API.

`GameError` é convertido em resposta JSON no formato do schema `Error`
do openapi.yaml por um exception handler registrado em `main.py`:

    {"error": {"code": "<code>", "message": "<message>"}}
"""

from __future__ import annotations


class GameError(Exception):
    """Erro de domínio com status HTTP e código estável."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message

    # --- atalhos semânticos -------------------------------------------------
    @classmethod
    def not_found(cls, what: str) -> "GameError":
        return cls(404, "not_found", what)

    @classmethod
    def conflict(cls, code: str, message: str) -> "GameError":
        return cls(409, code, message)

    @classmethod
    def bad_request(cls, code: str, message: str) -> "GameError":
        return cls(400, code, message)
