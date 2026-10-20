"""Runner de migrations: cada arquivo roda uma vez, registrado pelo número, e a
faixa 010–019 entra mesmo num banco que já passou da 020."""

from __future__ import annotations

import shutil
import sqlite3

import pytest

from app import db


@pytest.fixture()
def migrations_dir(tmp_path, monkeypatch):
    target = tmp_path / "migrations"
    shutil.copytree(db.MIGRATIONS_DIR, target)
    monkeypatch.setattr(db, "MIGRATIONS_DIR", target)
    return target


def _numbers(conn) -> list[int]:
    return [row[0] for row in conn.execute("SELECT number FROM schema_migrations ORDER BY number")]


def _files() -> list[int]:
    return [number for number, _ in db._migration_files()]


def test_fresh_database_records_every_migration(tmp_db):
    with db.session() as conn:
        assert _numbers(conn) == _files()
        assert conn.execute("PRAGMA user_version").fetchone()[0] == _files()[-1]


def test_kimi_range_applies_after_grok_range(tmp_path, monkeypatch, migrations_dir):
    path = tmp_path / "v21.db"
    conn = sqlite3.connect(path)
    conn.executescript(db.SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.execute("PRAGMA user_version = 21")
    conn.commit()
    conn.close()
    (migrations_dir / "010_scene_state.sql").write_text(
        "CREATE TABLE scene_state(adventure_id TEXT PRIMARY KEY, data TEXT);\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AIDUNGEON_DB_PATH", str(path))

    db.init_db()
    db.init_db()

    with db.session() as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "scene_state" in tables
        assert 10 in _numbers(conn)
        assert _numbers(conn) == _files()
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 24


def test_backfill_keeps_old_migrations_from_running_again(tmp_path, monkeypatch,
                                                          migrations_dir):
    path = tmp_path / "v20.db"
    before = db.SCHEMA_PATH.read_text(encoding="utf-8").split("-- --- Grok (G5)")[0]
    conn = sqlite3.connect(path)
    conn.executescript(before)
    conn.execute("PRAGMA user_version = 20")
    conn.commit()
    conn.close()
    monkeypatch.setenv("AIDUNGEON_DB_PATH", str(path))

    db.init_db()

    with db.session() as conn:
        assert _numbers(conn) == _files()
        columns = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
        assert {"priority", "supersede_key"} <= columns
        assert conn.execute("PRAGMA user_version").fetchone()[0] == _files()[-1]
