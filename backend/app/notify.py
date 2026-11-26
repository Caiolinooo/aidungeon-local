"""Avisos no celular pelo ntfy (https://ntfy.sh).

Quem envia é este servidor, então o aviso chega com o jogo fechado no
telefone. O tópico funciona como senha: quem o conhece lê e escreve nele.
Por isso ele é sorteado, só o admin o vê, e as mensagens não levam nada da
aventura: só o estado do Colab.
"""

from __future__ import annotations

import re
import secrets
import sqlite3
from typing import Any

import httpx

from app import db, settings_store

SERVER = "https://ntfy.sh"
_TOPIC = re.compile(r"[A-Za-z0-9_-]{8,64}")


def topic(conn: sqlite3.Connection) -> str:
    value = settings_store._get_override(conn, "notify.ntfy_topic")
    return value if isinstance(value, str) and _TOPIC.fullmatch(value) else ""


def new_topic() -> str:
    return "aidungeon-" + secrets.token_urlsafe(12).replace("_", "-")


def set_topic(conn: sqlite3.Connection, value: str) -> str:
    """Grava o tópico. Vazio desliga os avisos."""
    value = (value or "").strip()
    if not value:
        db.delete_setting(conn, "notify.ntfy_topic")
        return ""
    if not _TOPIC.fullmatch(value):
        from app.errors import GameError
        raise GameError.bad_request(
            "validation_error", "o tópico precisa ter de 8 a 64 letras, números, - ou _")
    settings_store._set_override(conn, "notify.ntfy_topic", value)
    return value


def send(message: str, *, title: str = "AI Dungeon", tags: str = "", transport: Any = None,
         conn: sqlite3.Connection | None = None) -> bool:
    """Manda o aviso. Nunca levanta erro: aviso que falha não pode derrubar quem avisou."""
    try:
        if conn is None:
            with db.session() as own:
                target = topic(own)
        else:
            target = topic(conn)
        if not target:
            return False
        headers = {"Title": title.encode("utf-8").decode("latin-1", "ignore") or "AI Dungeon"}
        if tags:
            headers["Tags"] = tags
        with httpx.Client(transport=transport, timeout=8.0) as client:
            return client.post(f"{SERVER}/{target}", content=message.encode("utf-8"),
                               headers=headers).status_code == 200
    except Exception:
        return False
