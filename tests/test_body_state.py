"""Corpo/dor/visibilidade: normalização, deltas e snapshots sem decay."""

from __future__ import annotations

import json

from app import body_state


def _shape() -> dict:
    return {
        "pain": 4,
        "conditions": [{
            "id": "cut-cheek", "kind": "cut", "location": "left cheek",
            "severity": 3, "description": "corte no rosto",
            "visual_en": "small cut on the left cheek",
            "visibility": "portrait", "active": True,
        }],
    }


def test_normalize_clamps_and_defaults_visibility():
    state = body_state.normalize_body_state({
        "pain": 99,
        "conditions": [
            {"kind": "wound", "severity": 0, "location": "side"},
            {"kind": "bogus", "visibility": "hidden", "active": False},
        ],
    })
    assert state["v"] == body_state.VERSION
    assert state["pain"] == 10
    first, second = state["conditions"]
    assert first["severity"] == 1
    assert first["visibility"] == "scene"
    assert first["scene_visible"] is True
    assert first["portrait_visible"] is False
    assert second["kind"] == "other"
    assert second["active"] is False
    assert second["visibility"] == "hidden"
    assert second["scene_visible"] is False


def test_apply_adds_updates_and_heals_without_decay():
    first = body_state.apply_body({}, {
        "pain": 5,
        "conditions": [{
            "kind": "cut", "location": "left cheek", "severity": 3,
            "description": "corte no rosto", "visibility": "portrait",
        }],
    }, source_action_id=11, source_seq=3)
    assert first["pain"] == 5
    assert first["conditions"][0]["source_seq"] == 3

    unchanged = body_state.apply_body(first, {})
    assert unchanged["pain"] == 5
    assert unchanged["conditions"] == first["conditions"]

    updated = body_state.apply_body(first, {
        "pain": 2,
        "conditions": [{
            "kind": "cut", "location": "left cheek", "op": "update",
            "severity": 1, "description": "corte quase fechado",
        }],
    }, source_seq=4)
    assert updated["pain"] == 2
    assert updated["conditions"][0]["severity"] == 1
    assert updated["conditions"][0]["id"] == first["conditions"][0]["id"]
    assert updated["conditions"][0]["source_seq"] == 4

    healed = body_state.apply_body(updated, {
        "conditions": [{"id": first["conditions"][0]["id"], "op": "heal"}],
    }, source_seq=5)
    assert healed["conditions"][0]["active"] is False
    assert healed["conditions"][0]["scene_visible"] is False
    assert healed["conditions"][0]["healed_seq"] == 5


def test_visibility_drives_portrait_and_scene_prompts():
    state = body_state.normalize_body_state(_shape())
    state["conditions"].append(body_state.normalize_condition({
        "kind": "bruise", "location": "ribs", "visual_en": "bruise on the ribs",
        "visibility": "scene", "active": True,
    }))
    state["conditions"].append(body_state.normalize_condition({
        "kind": "illness", "location": "lungs", "visual_en": "sick",
        "visibility": "hidden", "active": True,
    }))
    assert body_state.visual_prompt(state, portrait=True) == "small cut on the left cheek"
    scene = body_state.visual_prompt(state, portrait=False)
    assert "small cut" in scene
    assert "bruise on the ribs" in scene
    assert "sick" not in scene
    assert [c["location"] for c in body_state.active_conditions(state, portrait=True)] == [
        "left cheek"]


def test_snapshot_and_restore_keep_unrelated_fields():
    sheet = {
        "appearance": "red hair", "current_outfit": "grey cloak",
        "expression": "wary", "body_state": _shape(),
        "portrait_state_key": "abc", "portrait_job_id": "job1",
        "portrait_prompt": "red hair, grey cloak",
    }
    snap = body_state.snapshot(sheet)
    sheet["body_state"] = {"pain": 0, "conditions": []}
    sheet["expression"] = "calm"
    sheet["current_outfit"] = "white gown"
    restored = body_state.restore_sheet(sheet, snap)
    assert restored["appearance"] == "red hair"
    assert restored["body_state"]["pain"] == 4
    assert restored["expression"] == "wary"
    assert restored["current_outfit"] == "grey cloak"
    assert restored["portrait_state_key"] == "abc"


def test_apply_to_sheet_updates_expression_and_outfit():
    sheet = {"name": "Elara"}
    updated = body_state.apply_to_sheet(sheet, {
        "pain": 3,
        "expression": "guarded",
        "current_outfit": "travelling cloak",
        "conditions": [{"kind": "bruise", "location": "arm"}],
    })
    assert updated["expression"] == "guarded"
    assert updated["current_outfit"] == "travelling cloak"
    assert updated["body_state"]["pain"] == 3
    assert json.loads(body_state.to_json(updated["body_state"]))["pain"] == 3
    assert sheet == {"name": "Elara"}
