"""Dependências FastAPI injetáveis (ponto de extensão para testes)."""

from __future__ import annotations

from typing import Any


def get_http_transport() -> Any:
    """Transport httpx para chamadas ao LLM.

    Produção: None (httpx.Client padrão). Testes sobrescrevem via
    `app.dependency_overrides[get_http_transport]` com `httpx.MockTransport`.
    """
    return None
