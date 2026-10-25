"""Contratos estritos do smoke pós-deploy."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from app import db

TOOLS_DIR = Path(__file__).resolve().parent.parent / "tools"
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

import smoke_check  # noqa: E402


@pytest.mark.parametrize("health", [
    {"status": "degraded", "model_loaded": True},
    {"status": "ok", "model_loaded": False},
    {"status": "unreachable", "model_loaded": False},
])
def test_smoke_fails_for_unhealthy_llm(monkeypatch, health):
    replies = iter([(200, b"[]"), (200, json.dumps(health).encode())])
    monkeypatch.setattr(smoke_check, "get", lambda *args, **kwargs: next(replies))

    assert smoke_check.check_api("http://app.local", "token") is False


def test_smoke_accepts_only_healthy_loaded_llm(monkeypatch):
    health = {"status": "ok", "model_loaded": True, "model_discovered": "model"}
    replies = iter([(200, b"[]"), (200, json.dumps(health).encode())])
    monkeypatch.setattr(smoke_check, "get", lambda *args, **kwargs: next(replies))

    assert smoke_check.check_api("http://app.local", "token") is True


def test_smoke_db_accepts_expected_migrations_and_integrity(tmp_db):
    assert smoke_check.check_db(str(tmp_db)) is True


def test_smoke_db_rejects_missing_expected_migration(tmp_db):
    expected = db._migration_files()[0][0]
    with db.session() as conn:
        conn.execute("DELETE FROM schema_migrations WHERE number = ?", (expected,))

    assert smoke_check.check_db(str(tmp_db)) is False


def test_smoke_db_rejects_stale_user_version(tmp_db):
    with db.session() as conn:
        conn.execute("PRAGMA user_version = 1")

    assert smoke_check.check_db(str(tmp_db)) is False


def test_smoke_db_rejects_failed_integrity_check(monkeypatch):
    expected = [number for number, _ in db._migration_files()]

    class Rows(list):
        def fetchone(self):
            return self[0]

    class Connection:
        def execute(self, sql):
            if sql == "PRAGMA user_version":
                return Rows([(expected[-1],)])
            if sql.startswith("SELECT number FROM schema_migrations"):
                return Rows([(number,) for number in expected])
            if sql == "PRAGMA integrity_check":
                return Rows([("invalid page",)])
            raise AssertionError(sql)

        def close(self):
            pass

    monkeypatch.setattr(smoke_check.sqlite3, "connect", lambda *args, **kwargs: Connection())

    assert smoke_check.check_db("app.db") is False
