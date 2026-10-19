#!/usr/bin/env python3
"""Marca um card como pessoa real e baixa a foto principal da Wikipedia.

Não imprime segredo. A foto vai para data/references (ou AIDUNGEON_REFERENCES_DIR).

    PYTHONPATH=backend python tools/resolve_real_likeness.py \
        --adventure dd5a6fa4c0394dd3832a1261778ef5a2 \
        --title "Neymar Jr." --query Neymar
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app import db, likeness, settings_store  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve o likeness de um card já existente.")
    parser.add_argument("--adventure", required=True)
    parser.add_argument("--title", required=True, help="título exato do card")
    parser.add_argument("--query", default="", help="título na Wikipedia, se for diferente do card")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    with db.session() as conn:
        cards = db.list_cards(conn, args.adventure)
        match = [card for card in cards if (card.get("title") or "") == args.title]
        if len(match) != 1:
            print(f"cards com esse título: {len(match)}", file=sys.stderr)
            return 2
        llm_cfg = settings_store.effective_llm_settings(conn)
        updated = likeness.resolve_card(
            conn, match[0], query=args.query or None, llm_cfg=llm_cfg, force=args.force,
        )
    sheet = json.loads(updated.get("sheet") or "{}")
    like = sheet.get("likeness") or {}
    print(json.dumps({
        "card_id": updated["id"],
        "title": updated.get("title"),
        "real_name": sheet.get("real_name"),
        "minor": bool(sheet.get("minor")),
        "license": like.get("license") or "",
        "reference": like.get("reference_image_path") or "",
    }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        code = getattr(exc, "code", "")
        message = getattr(exc, "message", "") or str(exc)
        print(f"{code}: {message}" if code else message, file=sys.stderr)
        raise SystemExit(1)
