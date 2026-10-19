"""See: a frase em inglês lidera; o beat cru em português fica de fora."""

from __future__ import annotations

import json

from app import db
from conftest import add_action, make_adventure
from test_v2 import make_v2_transport, override_transport


def test_see_drops_the_portuguese_beat(client, tmp_path):
    summary = "Princess Alusair steps into the rain."
    adv = make_adventure(image_style="ink sketch")
    with db.session() as conn:
        db.set_setting(conn, "image.storage_dir", json.dumps(str(tmp_path / "imgs")))
    add_action(adv["id"], 0, "continue", "Princesa Alusair entra na chuva.")
    override_transport(client, make_v2_transport(chat_content=summary))
    try:
        resp = client.post(f"/api/adventures/{adv['id']}/see", json={"action_seq": 0})
    finally:
        client.app.dependency_overrides.clear()
    assert resp.status_code == 200, resp.text
    data = resp.json()
    head = data["prompt"].split("BREAK", 1)[0]
    sentence = summary.rstrip(" ,.;")  # clip_words tira o ponto final no prompt
    assert "entra na chuva" not in data["prompt"]
    assert sentence in head
    assert data["action"]["text"].rstrip(" ,.;") == sentence
