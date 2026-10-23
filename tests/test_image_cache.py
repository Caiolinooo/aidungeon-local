"""Cache de imagens por aventura: chave estável, busca e deleção segura."""

from __future__ import annotations

from app import db, image_client


def _cfg(**extra):
    cfg = {
        "provider": "sdwebui",
        "sd_model_checkpoint": "epicrealism",
        "sdwebui_steps": 30,
        "sdwebui_width": 832,
        "sdwebui_height": 1216,
        "negative_prompt": "bad anatomy",
        "api_key": "secret-a",
    }
    cfg.update(extra)
    return cfg


def test_request_key_is_stable_and_ignores_the_api_key():
    first = image_client.request_key(_cfg(), "a knight", width=832, height=1216)
    same = image_client.request_key(_cfg(api_key="secret-b"), "a knight",
                                    width=832, height=1216)
    other_prompt = image_client.request_key(_cfg(), "a mage", width=832, height=1216)
    other_negative = image_client.request_key(
        _cfg(), "a knight", width=832, height=1216, negative_extra="extra limbs")
    other_denoise = image_client.request_key(
        _cfg(), "a knight", width=832, height=1216, denoising_strength=0.5)
    assert first == same
    assert first != other_prompt
    assert first != other_negative
    assert first != other_denoise
    assert "secret" not in first


def test_cache_lookup_is_scoped_to_one_adventure(tmp_db):
    with db.session() as conn:
        one = db.insert_adventure(conn, title="One")
        two = db.insert_adventure(conn, title="Two")
        image = db.insert_image(
            conn, adventure_id=one["id"], action_seq=0, file_path="outputs/a.png",
            kind="portrait", ref_id="card-1", cache_key="key-1",
        )
        assert db.find_cached_image(
            conn, one["id"], kind="portrait", ref_id="card-1",
            cache_key="key-1")["id"] == image["id"]
        assert db.find_cached_image(
            conn, one["id"], kind="portrait", ref_id="card-2",
            cache_key="key-1") is None
        assert db.find_cached_image(
            conn, two["id"], kind="portrait", ref_id="card-1",
            cache_key="key-1") is None
        assert db.find_cached_image(
            conn, one["id"], kind="portrait", ref_id="card-1",
            cache_key="") is None


def test_reused_image_keeps_the_shared_file_until_the_last_row(tmp_db, tmp_path,
                                                               monkeypatch):
    monkeypatch.setattr(db, "REPO_ROOT", tmp_path)
    shared = tmp_path / "outputs" / "shared.png"
    shared.parent.mkdir()
    shared.write_bytes(b"png")
    with db.session() as conn:
        adv = db.insert_adventure(conn, title="Reuse")
        original = db.insert_image(
            conn, adventure_id=adv["id"], action_seq=1, file_path=str(shared),
            kind="see", cache_key="k",
        )
        reused = db.insert_image(
            conn, adventure_id=adv["id"], action_seq=2, file_path=str(shared),
            kind="see", cache_key="k", reused_from=original["id"],
        )
        assert reused["reused_from"] == original["id"]
        assert db.delete_image(conn, original["id"]) is True
        assert shared.exists()
        assert db.delete_image(conn, reused["id"]) is True
        assert not shared.exists()
