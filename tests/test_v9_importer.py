"""Import de backup grava image_style também no cenário implícito."""

from __future__ import annotations

import json

from app import db

import importer


def _backup(root, image_style: str) -> None:
    root.mkdir()
    (root / "metadata.json").write_text(json.dumps({
        "adventure": {
            "title": "Ink Tale",
            "description": "A painted door.",
            "tags": ["art"],
            "thirdPerson": False,
            "memory": "The door is red.",
            "authorsNote": "Keep it visual.",
        },
        "state": {
            "imageStyle": image_style,
            "instructions": {"scenario": "Describe light."},
            "storySummary": "",
            "storyCards": [],
            "memories": [],
        },
    }), encoding="utf-8")
    (root / "actions-001.json").write_text(json.dumps({
        "partNumber": 1,
        "actions": [{"id": "0", "type": "start", "text": "The door opens."}],
    }), encoding="utf-8")


def test_imported_scenario_keeps_image_style(tmp_db, tmp_path):
    root = tmp_path / "backup"
    _backup(root, "oil painting, dramatic light")
    result = importer.import_dir(root)
    with db.session() as conn:
        adv = db.get_adventure(conn, result["adventure_id"])
        scenario = db.get_scenario(conn, adv["scenario_id"])
    assert adv["image_style"] == "oil painting, dramatic light"
    assert scenario["image_style"] == "oil painting, dramatic light"
    assert scenario["title"] == "Ink Tale"
