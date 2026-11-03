"""Orçamento dos presets. Sem rede e sem descarregar o narrador."""

from __future__ import annotations

from app import image_budget


def test_h3_length_snaps_up_onto_the_grid():
    assert image_budget.h3_length(121) == 124
    assert image_budget.h3_length(56) == 56
    assert image_budget.h3_length(48) == 56


def test_zimage_budget_fits_the_hole_at_1024():
    budget = image_budget.preset_budget(
        "image", "z_image_turbo", width=1024, height=1024)
    assert budget["weights_vram_mib"] == 3992
    assert budget["activation_mib"] == 2048
    assert budget["ram_mib"] == 5372
    assert budget["vram_mib"] == 6040
    assert budget["weights_known"] is True
    assert budget["spill_mib"] == 0


def test_h3_budget_uses_the_snapped_length():
    budget = image_budget.preset_budget(
        "video", "h3", width=768, height=448, frames=121)
    assert budget["activation_mib"] == round(3072 * 124 / 56)
    assert budget["weights_vram_mib"] == 13615
    assert budget["ram_mib"] == 14961
    assert budget["weights_known"] is True


def test_h3_default_frame_spills_past_the_hole():
    budget = image_budget.preset_budget(
        "video", "h3", width=768, height=448, frames=56)
    assert budget["activation_mib"] == 3072
    assert budget["vram_mib"] == 16687
    assert budget["spill_mib"] == 9551


def test_unknown_engine_is_rejected():
    try:
        image_budget.preset_budget("image", "foo", width=64, height=64)
    except ValueError as exc:
        assert "desconhecido" in str(exc)
    else:
        raise AssertionError("unknown engine must raise")


def test_budget_route_compares_live_memory_and_stays_200(client, monkeypatch):
    monkeypatch.setattr("app.routes.health._memory", lambda: {
        "vram_total_mb": 23034,
        "vram_used_mb": 20000,
        "ram_available_mb": 4000,
    })
    response = client.get("/api/settings/image/budget", params={
        "kind": "image",
        "engine": "z_image_turbo",
        "width": 1024,
        "height": 1024,
    })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["vram_free_mib"] == 3034
    assert body["ram_available_mib"] == 4000
    assert body["fits_vram"] is False
    assert body["fits_ram"] is False
    assert body["spill_mib"] == 0

    monkeypatch.setattr("app.routes.health._memory", lambda: {})
    empty = client.get("/api/settings/image/budget", params={
        "kind": "video",
        "engine": "h3",
        "width": 768,
        "height": 448,
        "frames": 56,
    })
    assert empty.status_code == 200, empty.text
    unknown = empty.json()
    assert unknown["vram_free_mib"] is None
    assert unknown["ram_available_mib"] is None
    assert unknown["fits_vram"] is None
    assert unknown["fits_ram"] is None
    assert unknown["spill_mib"] == 9551

    missing = client.get("/api/settings/image/budget", params={
        "kind": "image", "engine": "qwen", "height": "wide",
    })
    assert missing.status_code == 422
    assert missing.json()["error"]["code"] == "validation_error"
    bad_kind = client.get("/api/settings/image/budget", params={
        "kind": "audio", "engine": "qwen", "width": 64, "height": 64,
    })
    assert bad_kind.status_code == 422
    assert bad_kind.json()["error"]["code"] == "validation_error"
