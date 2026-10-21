"""Packs de regras em JSON. Cache em memória; pack vazio não é erro."""

from __future__ import annotations

import json
from pathlib import Path

_DIR = Path(__file__).resolve().parent / "packs"
_CACHE: dict[str, dict] = {}


def load_pack(pack_id: str) -> dict | None:
    if not pack_id:
        return None
    cached = _CACHE.get(pack_id)
    if cached is not None:
        return cached
    path = _DIR / f"{pack_id}.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return None
    _CACHE[pack_id] = data
    return data


def list_packs() -> list[dict]:
    found: list[dict] = []
    if not _DIR.is_dir():
        return found
    for path in sorted(_DIR.glob("*.json")):
        pack = load_pack(path.stem)
        if not pack:
            continue
        found.append({
            "id": pack.get("id") or path.stem,
            "name": pack.get("name") or path.stem,
            "resolution": pack.get("resolution") or "",
            "rulesets": list(pack.get("rulesets") or []),
        })
    return found


def pack_for_ruleset(ruleset_id: str) -> str:
    for pack in list_packs():
        if ruleset_id in pack["rulesets"]:
            return str(pack["id"])
    return ""
