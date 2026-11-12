"""O See obedece o pedido e as imagens acompanham o que a história mudou."""

from __future__ import annotations

import json

from app import llm_client, look_lock, npc_assets, scene
from app.routes import images


def _card(title: str, **extra) -> dict:
    sheet = extra.pop("sheet", None)
    return {"id": title.lower().replace(" ", "-"), "title": title, "type": "character",
            "entry": extra.pop("entry", ""), "triggers": [],
            "sheet": json.dumps(sheet) if sheet else "", **extra}


CAST = [_card("Dick Vigarista", use_for_character_creation=1), _card("Lobo de Fogo"),
        _card("Fada dos Espelhos"), _card("Marina Teste"), _card("Bruno Teste")]
PLOT = "[Name: Dick Vigarista; Role: Protagonista]"


def test_the_order_decides_who_is_painted():
    """Pedido "Marina sentada, Bruno de joelhos" com cinco na cena: pintava o
    protagonista, o lobo, a fada e a Marina, e deixava o Bruno de fora."""
    asked = images._painted_cast(CAST, "Marina sentada num banco, Bruno de joelhos", PLOT)
    assert [card["title"] for card in asked] == ["Marina Teste", "Bruno Teste"]
    with_me = images._painted_cast(CAST, "Marina sentada comigo no banco", PLOT)
    assert [card["title"] for card in with_me] == ["Dick Vigarista", "Marina Teste"]
    # sem nomes no pedido: protagonista e os três citados por último
    plain = images._painted_cast(CAST, "todos olhando para o céu", PLOT)
    assert [card["title"] for card in plain] == [
        "Dick Vigarista", "Fada dos Espelhos", "Marina Teste", "Bruno Teste"]
    assert images._painted_cast(CAST[:3], "", PLOT) == CAST[:3]


def test_creatures_are_told_apart_from_people_and_from_verbs():
    fada = _card("Fada dos Espelhos", entry="Uma criatura mágica. Ajuda os visitantes, mas "
                 "cobra um preço por isso.", portrait_prompt="A delicate fairy with silver hair")
    lobo = _card("Lobo de Fogo", entry="O Lobo de Fogo é uma criatura mágica.",
                 portrait_prompt="A majestic fiery wolf with thick reddish fur")
    capitao = _card("Lobo do Mar", entry="Capitão velho e ranzinza.",
                    portrait_prompt="An old sea captain with a grey beard")
    ladrao = _card("Ladrão de Jibóia", entry="Um pirata gordo que rouba cobras.",
                   portrait_prompt="A stout pirate with a large belly")
    naja = _card("Naja", entry="Uma cobra enorme que guarda o templo.")
    assert npc_assets.creature_kind(fada) == ""            # "cobra um preço" é verbo
    assert npc_assets.creature_kind(lobo) == "wolf"
    assert npc_assets.creature_kind(capitao) == ""         # apelido não é bicho
    assert npc_assets.creature_kind(ladrao) == ""
    assert npc_assets.creature_kind(naja) == "snake"
    # o texto da fada cita o lobo: ela continua não sendo lobo
    citada = _card("Fada dos Espelhos", entry="Uma criatura mágica. Teme o Lobo de Fogo.",
                   portrait_prompt="A delicate fairy with silver hair")
    assert npc_assets.creature_kind(citada) == ""
    marina = _card("Marina Teste")
    mixed = npc_assets.coverage_lock(marina, lobo)
    assert mixed == ("Marina Teste: fully clothed, wearing clothes, covered torso. "
                     "Lobo de Fogo: natural hide covering the body, not bare human skin")
    assert npc_assets.coverage_lock(marina) == npc_assets.CLOTHED_LOCK
    assert npc_assets.coverage_lock(lobo) == npc_assets.CREATURE_LOCK
    assert npc_assets.coverage_lock(marina, lobo, explicit=True) == ""


def test_story_state_goes_on_each_persons_own_line():
    """Roupa do momento e marcas visíveis ficam coladas ao nome de quem as
    tem. Numa frase à parte, com quatro em cena, o vestido da Marina foi
    pintado na fada e no lobo."""
    marina = _card("Marina Teste", sheet={
        "current_outfit": "long yellow hooded raincoat over her clothes",
        "body_state": {"v": 1, "pain": 2, "conditions": [
            {"id": "c1", "active": True, "scene_visible": True, "portrait_visible": False,
             "visual_en": "bandaged right forearm"}]}})
    bruno = _card("Bruno Teste", sheet={"current_outfit": "regata branca e jeans"})
    lobo = _card("Lobo de Fogo", entry="criatura", portrait_prompt="A fiery wolf",
                 sheet={"current_outfit": "red cape"})
    assert npc_assets.story_state_bits(marina) == [
        "now wearing long yellow hooded raincoat over her clothes", "bandaged right forearm"]
    assert npc_assets.story_state_bits(bruno) == []     # português não vai ao pintor
    assert npc_assets.story_state_bits(lobo) == []      # bicho não leva roupa
    assert npc_assets.story_state_note([marina, bruno]) == npc_assets.STORY_STATE_NOTE
    assert npc_assets.story_state_note([bruno]) == ""


def test_backdrop_paints_the_outfit_the_story_gave(tmp_db):
    from app import look_lock as locks

    def looked(title, **sheet):
        look = {"hair_colour": "black", "hair_length": "chin-length", "glasses": "none",
                "version": locks.LOOK_VERSION, "basis_hash": "x", "source": "photo"}
        return _card(title, portrait_image_id=f"{title}-img",
                     sheet={"reference": {"turnaround": {}}, "look_lock": look, **sheet})

    marina = looked("Marina Teste", current_outfit="long red dress")
    bruno = looked("Bruno Teste")
    prompt, people = scene._prompt("the cabin", "A wooden cabin.", "cinematic", "night",
                                   [marina, bruno], "Marina and Bruno wait by the fire.")
    assert people is True
    assert "Marina Teste: no glasses; now wearing long red dress; chin-length" in prompt
    assert "Bruno Teste: no glasses" in prompt and "Bruno Teste: now wearing" not in prompt
    assert npc_assets.STORY_STATE_NOTE in prompt
    assert "each with that exact face, hair and age." in prompt
    plain, _ = scene._prompt("the cabin", "A wooden cabin.", "cinematic", "night",
                             [looked("Marina Teste"), bruno], "Marina and Bruno wait.")
    assert npc_assets.STORY_STATE_NOTE not in plain
    assert "each with that exact face, hair and age and clothing." in plain
    # cena explícita não recebe a roupa do momento por este caminho
    from app import likeness
    assert "now wearing" not in likeness.opening_clause([marina, bruno], explicit=True)
    assert "now wearing long red dress" in likeness.opening_clause([marina, bruno])


def test_see_reads_the_clothing_of_the_moment_only_when_the_text_talks_about_it(monkeypatch):
    calls: list[str] = []

    def fake(cfg, messages, **kwargs):
        calls.append(messages[1]["content"])
        return json.dumps({"Marina": "long yellow hooded raincoat over her clothes",
                           "Bruno Teste": "white tank top and jeans",
                           "Dick Vigarista": "completely naked",
                           "Fada dos Espelhos": "vestido de pétalas"})

    monkeypatch.setattr(llm_client, "chat_completion", fake)
    names = ["Marina Teste", "Bruno Teste", "Dick Vigarista", "Fada dos Espelhos"]
    quiet = images._story_outfits(names, "Eles caminham pela floresta em silêncio.", "",
                                  llm_cfg={}, transport=None)
    assert quiet == {} and calls == []         # sem palavra de roupa: nenhuma chamada
    worn = images._story_outfits(
        names, "Começa a chover. Marina veste uma capa amarela; Bruno tira a camisa.", "",
        llm_cfg={}, transport=None)
    assert worn == {"Marina Teste": "long yellow hooded raincoat over her clothes",
                    "Bruno Teste": "white tank top and jeans"}
    assert len(calls) == 1 and "People: Marina Teste; Bruno Teste" in calls[0]
    dressed = images._with_story_outfits([_card("Marina Teste"), _card("Bruno Teste")], worn)
    assert json.loads(dressed[0]["sheet"])["current_outfit"].startswith("long yellow")

    def broken(cfg, messages, **kwargs):
        raise llm_client.LlmError("timeout")
    monkeypatch.setattr(llm_client, "chat_completion", broken)
    assert images._story_outfits(names, "Marina veste a capa.", "", llm_cfg={}, transport=None) == {}


def test_look_reads_stay_off_the_turn_slot(monkeypatch):
    """Na role `assistant` a leitura ocupava o slot do juiz do turno, que tem
    12 s de prazo: o turno seguia sem adjudicação."""
    roles: list[str] = []

    def fake(cfg, messages, **kwargs):
        roles.append(kwargs.get("role"))
        return "{}"

    monkeypatch.setattr(llm_client, "chat_completion", fake)
    look_lock._read_clothing(b"\x89PNG\r\n\x1a\n" + b"0" * 8, llm_cfg={}) if False else None
    look_lock._read_body_text("Tall and slender.", llm_cfg={})
    assert roles and set(roles) == {"assets"}


def test_360_turn_waits_until_the_game_is_idle(tmp_db, monkeypatch):
    """O giro 360° derruba o narrador para 8 tok/s. Só é pego da fila com o
    jogo parado há cinco minutos: sem ação e sem imagem pedida."""
    from datetime import datetime, timedelta, timezone

    from app import db, jobs
    from conftest import add_action, make_adventure

    monkeypatch.setenv("AIDUNGEON_TURNAROUND_IDLE_S", "300")
    adv = make_adventure()
    old = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")
    with db.session() as conn:
        turn = jobs.enqueue(conn, "turnaround_clip", adv["id"], "npc", {"prompt": "turn"},
                            priority=jobs.PRIORITY_PORTRAIT_CLIP)
        conn.execute("UPDATE actions SET created_at=?", (old,))
        conn.execute("UPDATE jobs SET created_at=?", (old,))
        assert db.claim_next_job(conn)["id"] == turn["id"]     # jogo parado: roda
        conn.execute("UPDATE jobs SET status='pending', started_at=NULL WHERE id=?", (turn["id"],))
    add_action(adv["id"], 5, "continue", "A chuva cai.")       # alguém jogou agora
    with db.session() as conn:
        assert db.claim_next_job(conn) is None
        conn.execute("UPDATE actions SET created_at=?", (old,))
        still = jobs.enqueue(conn, "scene_backdrop", adv["id"], "room", {"prompt": "hall"})
        assert db.claim_next_job(conn)["id"] == still["id"]     # a imagem roda
        assert db.claim_next_job(conn) is None                  # e o giro continua esperando
        conn.execute("UPDATE jobs SET created_at=? WHERE id=?", (old, still["id"]))
        assert db.claim_next_job(conn)["id"] == turn["id"]


def test_chronicle_gets_a_second_chance_after_a_timeout(monkeypatch):
    import threading

    from app import game_engine, npc_chronicle

    calls: list[int] = []

    def flaky(adventure_id, transport):
        calls.append(1)
        if len(calls) == 1:
            raise llm_client.LlmTimeout("llm call timed out")
        return {"created": 0, "updated": 1}

    monkeypatch.setattr(npc_chronicle, "update_from_turn", flaky)
    monkeypatch.setattr(game_engine, "_CHRONICLE_RETRY_S", 0.01)
    game_engine._chronicle_with_retry("adv", None, threading.Event())
    assert len(calls) == 2
    calls.clear()
    stopped = threading.Event(); stopped.set()               # outro turno começou
    game_engine._chronicle_with_retry("adv", None, stopped)
    assert len(calls) == 1


def test_a_cancelled_clip_is_interrupted_again_if_the_first_interrupt_came_too_early(
        tmp_db, monkeypatch):
    """O /interrupt chegou antes de o ComfyUI começar o prompt e o giro
    cancelado rodou 3 minutos. Enquanto o job parado seguir no poll, o aviso
    é repetido a cada `_REINTERRUPT_S`."""
    from app import image_client, jobs

    sent: list[int] = []
    monkeypatch.setattr(image_client, "interrupt_comfy", lambda cfg, transport=None: sent.append(1))
    job = {"id": "clip-1", "type": "turnaround_clip", "adventure_id": "a"}
    jobs._forget_job_runtime("clip-1")
    assert jobs.interrupt_image(job) is True
    assert jobs.interrupt_image(job) is False          # logo em seguida: não repete
    jobs._clip_interrupted["clip-1"] -= jobs._REINTERRUPT_S + 1
    assert jobs.interrupt_image(job) is True           # o prompt ainda roda: manda de novo
    assert len(sent) == 2
    jobs._forget_job_runtime("clip-1")


def test_truncated_consequence_reply_keeps_what_came_complete():
    """Com o teto de tokens a resposta vinha cortada no meio de um item e a
    avaliação do turno inteiro era descartada."""
    from app import consequences

    cut = ('{"effects": [{"target": "Bruno", "visual_en": "white bandage"}, '
           '{"target": "Marina", "visual_en": "wet ha')
    assert consequences._extract_json(cut) == {
        "effects": [{"target": "Bruno", "visual_en": "white bandage"}]}
    whole = '```json\n{"effects": [], "facts": ["a }, tricky"]}\n```'
    assert consequences._extract_json(whole) == {"effects": [], "facts": ["a }, tricky"]}
    import pytest
    with pytest.raises(ValueError):
        consequences._extract_json("no json here")


def test_trait_cut_keeps_the_outfit_of_the_moment_before_other_traits():
    """No segundo corte a pessoa ficava só com os óculos; a roupa do momento
    vem antes do cabelo e do corpo."""
    line = ("Marina Teste: glasses: round tortoiseshell frames; now wearing yellow raincoat "
            "over blue mechanic jumpsuit; chin-length, straight, reddish-brown hair; "
            "body: slim, long frame")
    cut = scene._drop_traits(line, 110)
    assert cut == ("Marina Teste: glasses: round tortoiseshell frames; now wearing yellow "
                   "raincoat over blue mechanic jumpsuit")
    hair_first = ("Fada: no glasses; shoulder-length, loose waves, near-black hair; "
                  "now wearing white dress")
    assert scene._drop_traits(hair_first, 45) == "Fada: no glasses; now wearing white dress"
    assert scene._drop_traits(line, 400) == line
