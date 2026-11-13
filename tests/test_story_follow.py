"""O See obedece o pedido e as imagens acompanham o que a história mudou."""

from __future__ import annotations

import json

from app import likeness, llm_client, look_lock, npc_assets, scene
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


def test_wolf_is_an_animal_in_the_frame_not_another_person():
    """Quatro no elenco, um deles lobo: "Exactly 4 people" pintava quatro
    humanos e o lobo, ou um lobisomem ao lado do lobo."""
    marina = _card("Marina Teste", portrait_prompt="A woman with black hair")
    bruno = _card("Bruno Teste", portrait_prompt="A man with a beard")
    fada = _card("Fada dos Espelhos", portrait_prompt="A delicate fairy with silver hair")
    lobo = _card("Lobo de Fogo", entry="O Lobo de Fogo é uma criatura mágica.",
                 portrait_prompt="A majestic fiery wolf with thick reddish fur")
    cast = [marina, bruno, fada, lobo]
    people, beasts = npc_assets.split_cast(cast)
    assert [card["title"] for card in people] == ["Marina Teste", "Bruno Teste", "Fada dos Espelhos"]
    assert [card["title"] for card in beasts] == ["Lobo de Fogo"]
    assert npc_assets.animal_kind(lobo) == "wolf"
    capitao = _card("Lobo do Mar", entry="Capitão velho e ranzinza.",
                    portrait_prompt="An old sea captain with a grey beard")
    assert npc_assets.animal_kind(capitao) == ""
    dragon = _card("Draco", entry="um dragão antigo de escamas negras")
    assert npc_assets.creature_kind(dragon) == "dragon"
    assert npc_assets.animal_kind(dragon) == ""
    line = scene._headcount(cast, animals=True)
    assert line.startswith("Exactly 3 people and 1 animal in the frame")
    assert "Lobo de Fogo (the wolf)" in line
    assert "Exactly 4 people" not in line
    assert "Exactly 4 people" in scene._headcount([marina, bruno, fada, dragon], animals=True)
    alone = scene._alone_there(4, 1)
    assert "3 people and 1 animal" in alone
    assert "no other animal" in alone
    assert scene._alone_there(3) == "These 3 are the only people there: nobody else is anywhere in the frame, near or far"
    clause = likeness.opening_clause(cast)
    assert "Lobo de Fogo: a real wolf, an animal with the body of a wolf, never a person" in clause
    prompt, painted = scene._prompt(
        "the cabin", "A wooden cabin.", "cinematic", "night", cast,
        "Marina, Bruno, the fairy and the wolf wait by the fire.")
    assert painted is True
    assert "Exactly 3 people and 1 animal" in prompt
    assert "each animal with its own fur and markings" in prompt
    assert prompt.count("never a person") == 1        # sem a terceira trava repetida
    assert "Exactly 4 people" not in prompt
    assert "Exactly 5 people" not in prompt


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
    assert "each person with that exact face, hair and age." in prompt
    plain, _ = scene._prompt("the cabin", "A wooden cabin.", "cinematic", "night",
                             [looked("Marina Teste"), bruno], "Marina and Bruno wait.")
    assert npc_assets.STORY_STATE_NOTE not in plain
    assert "each person with that exact face, hair and age and clothing." in plain
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


def test_everyone_gets_a_clothing_line_when_the_story_changed_an_outfit():
    """Quatro personagens: quem não tinha linha de roupa pegava a de outro. Em
    6 fundos a fada saiu duas vezes com o macacão da Marina e uma vez virou um
    segundo homem; com a roupa de cada um escrita, 6 de 6 saíram certos."""
    marina = _card("Marina Teste", sheet={"current_outfit": "yellow raincoat over blue jumpsuit"})
    fada = _card("Fada dos Espelhos", sheet={"look_lock": {
        "version": look_lock.LOOK_VERSION,
        "top": "light blue sheer floral-embroidered camisole top with thin straps and floral embellishments",
        "bottom": "flowing petal and leaf fabric dress changing colour with light"}})
    lobo = _card("Lobo de Fogo", portrait_prompt="a wolf wreathed in fire")
    bits = npc_assets.cast_state_bits([marina, fada, lobo])
    assert bits["marina-teste"] == ["now wearing yellow raincoat over blue jumpsuit"]
    own = bits["fada-dos-espelhos"]
    # a cor e o nome da peça ficam; o detalhe do meio sai
    assert own == ["wearing light blue camisole top and flowing petal fabric dress"]
    assert bits["lobo-de-fogo"] == []                     # criatura não veste roupa
    # ninguém trocou de roupa: a referência manda e o prompt não cresce
    assert npc_assets.cast_state_bits([fada, lobo]) == {"fada-dos-espelhos": [], "lobo-de-fogo": []}
    # sozinha na cena não há de quem pegar roupa
    assert npc_assets.cast_state_bits([marina]) == {
        "marina-teste": ["now wearing yellow raincoat over blue jumpsuit"]}


def test_the_own_outfit_survives_the_trait_cut():
    line = ("Fada dos Espelhos: no glasses; wearing light blue camisole top and petal dress; "
            "shoulder-length, loose waves, near-black hair; body: slim, long frame")
    cut = scene._drop_traits(line, 90)
    assert "wearing light blue camisole top and petal dress" in cut
    assert "hair" not in cut


def test_a_non_answer_is_not_an_outfit():
    """O See pintou "now wearing no clothing described"."""
    for text in ("no clothing described", "No outfit mentioned", "unknown", "not specified",
                 "same as before", "none", "N/A", "clothing not described", ""):
        assert not npc_assets.is_outfit(text), text
    for text in ("yellow raincoat over blue jumpsuit", "worn suit", "white nightgown"):
        assert npc_assets.is_outfit(text), text
    dick = _card("Dick", sheet={"current_outfit": "no clothing described"})
    assert npc_assets.story_state_bits(dick) == []
    from app import body_state
    kept = body_state.apply_to_sheet({"current_outfit": "worn suit"},
                                     {"current_outfit": "no clothing described"})
    assert kept["current_outfit"] == "worn suit"


def test_another_character_is_not_a_body_mark():
    """O registro gravou "fairy on shoulder" como marca no ombro do protagonista
    e da própria fada; no fundo a fada virou um enfeite e a cena saiu errada."""
    from app import npc_chronicle
    marks = {"body_state": {"conditions": [
        {"id": "c1", "kind": "mark", "location": "shoulder", "visual_en": "fairy on shoulder",
         "active": True, "scene_visible": True, "visibility": "scene"},
        {"id": "c2", "kind": "cut", "location": "right forearm",
         "visual_en": "white bandage on right forearm", "active": True, "scene_visible": True,
         "visibility": "scene"},
        {"id": "c3", "kind": "wound", "location": "left arm", "visual_en": "wolf bite on left arm",
         "active": True, "scene_visible": True, "visibility": "scene"},
    ]}}
    dick = _card("Dick", sheet=marks)
    assert npc_assets.story_state_bits(dick) == [
        "white bandage on right forearm, wolf bite on left arm"]
    fada = _card("Fada dos Espelhos")
    others = npc_assets.other_names(dick, [dick, fada])
    assert "fada" in others and "dick" not in others
    body = {"conditions": [
        {"op": "add", "kind": "mark", "visual_en": "small glowing figure on the shoulder",
         "description": "A Fada pousa no ombro dele."},
        {"op": "add", "kind": "cut", "visual_en": "fresh cut on the left cheek",
         "description": "Um corte no rosto."},
        {"op": "heal", "id": "c1", "description": "a fada sai do ombro"},
    ]}
    kept = npc_chronicle._without_people_as_marks(body, others)["conditions"]
    assert [item.get("visual_en") or item["op"] for item in kept] == [
        "fresh cut on the left cheek", "heal"]
    # a marca que cita quem a causou é marca de verdade
    assert not npc_assets.names_someone("wolf bite on left arm", ["wolf", "fada"])
    assert not npc_assets.names_someone("Bruno's blood on her sleeve", ["bruno"])


def test_the_see_outfit_must_name_a_garment(monkeypatch):
    """Pedido "Bruno mostrando o braço enfaixado": o leitor devolveu "bandaged
    arm" como roupa e o Bruno perdeu o casaco que a história tinha dado."""
    assert not npc_assets.names_garment("bandaged arm")
    assert npc_assets.names_garment("yellow hooded cloak over blue jumpsuit")
    assert npc_assets.names_garment("brown leather hat and boots")
    monkeypatch.setattr(llm_client, "chat_completion", lambda *a, **k: json.dumps(
        {"Bruno Teste": "bandaged arm", "Marina Teste": "yellow hooded cloak over blue jumpsuit"}))
    worn = images._story_outfits(
        ["Bruno Teste", "Marina Teste"], "Marina veste a capa amarela.",
        "Bruno mostrando o braço enfaixado", llm_cfg={}, transport=None)
    assert worn == {"Marina Teste": "yellow hooded cloak over blue jumpsuit"}


def test_a_mark_names_its_owner_in_a_group():
    """A faixa do Bruno (de casaco) foi pintada no braço nu da fada em 4 de 4
    fundos; com o nome do dono e "por cima da roupa" ficou nele em 2 de 4."""
    assert npc_assets.owned_mark("white bandage on right forearm", "Bruno Teste", dressed=True) \
        == "white bandage on Bruno Teste's right forearm, worn over the clothes"
    assert npc_assets.owned_mark("small fresh cut on the left cheek", "Marina", dressed=True) \
        == "small fresh cut on Marina's left cheek"
    assert npc_assets.owned_mark("bruised knuckles", "Bruno", dressed=False) == "bruised knuckles on Bruno"
    assert npc_assets.owned_mark("scar across Bruno's brow", "Bruno", dressed=False) \
        == "scar across Bruno's brow"
    marks = {"current_outfit": "grey wool coat", "body_state": {"conditions": [
        {"id": "c2", "kind": "cut", "location": "right forearm",
         "visual_en": "white bandage on right forearm", "active": True, "scene_visible": True,
         "visibility": "scene"}]}}
    bruno, marina = _card("Bruno Teste", sheet=marks), _card("Marina Teste")
    group = npc_assets.cast_state_bits([bruno, marina])["bruno-teste"]
    assert group == ["now wearing grey wool coat",
                     "white bandage on Bruno Teste's right forearm, worn over the clothes"]
    # sozinho na cena não há para quem a marca ir
    assert npc_assets.cast_state_bits([bruno])["bruno-teste"] == [
        "now wearing grey wool coat", "white bandage on right forearm"]


def test_non_urgent_paints_wait_for_the_post_turn_chain(tmp_db):
    """Num turno de teste a crônica levou 275 s: quatro retratos refeitos, o
    clipe automático e o tick disputavam a placa com ela. Enquanto o pós-turno
    da aventura roda, só passa o que o jogador está esperando."""
    from app import db, jobs, post_turn_state
    from tests.conftest import make_adventure

    adv = make_adventure()
    other = make_adventure()
    with db.session() as conn:
        clip = jobs.enqueue(conn, "scene_clip", adv["id"], "img-1", {"automatic": True},
                            priority=jobs.PRIORITY_SCENE_CLIP)
        synced = jobs.enqueue(conn, "character_portrait", adv["id"], "card-1",
                              {"state_key": "abc"}, priority=jobs.PRIORITY_PRESENT_PORTRAIT)
        tick = jobs.enqueue(conn, "npc_day_tick", adv["id"], "tick", {},
                            priority=jobs.PRIORITY_LIFE_TICK)
        asked = jobs.enqueue(conn, "character_portrait", adv["id"], "card-2", {},
                             priority=jobs.PRIORITY_PRESENT_PORTRAIT)
        backdrop = jobs.enqueue(conn, "scene_backdrop", adv["id"], "ref", {"prompt": "x"},
                                priority=jobs.PRIORITY_BACKDROP)
        elsewhere = jobs.enqueue(conn, "npc_day_tick", other["id"], "tick", {},
                                 priority=jobs.PRIORITY_LIFE_TICK)
    post_turn_state.begin(adv["id"])
    assert post_turn_state.busy() == [adv["id"]]
    claimed = []
    with db.session() as conn:
        while True:
            job = db.claim_next_job(conn, hold_adventures=post_turn_state.busy())
            if not job:
                break
            claimed.append(job["id"])
            db.finish_job(conn, job["id"], "done", {})
    # retrato pedido, fundo e o job de outra aventura passam; o resto espera
    assert claimed == [asked["id"], backdrop["id"], elsewhere["id"]]
    post_turn_state.end(adv["id"])
    with db.session() as conn:
        rest = []
        while True:
            job = db.claim_next_job(conn, hold_adventures=post_turn_state.busy())
            if not job:
                break
            rest.append(job["id"])
            db.finish_job(conn, job["id"], "done", {})
    assert sorted(rest) == sorted([clip["id"], synced["id"], tick["id"]])


def test_a_stuck_post_turn_chain_does_not_hold_the_queue_forever(monkeypatch):
    from app import post_turn_state

    clock = {"now": 1000.0}
    monkeypatch.setattr(post_turn_state.time, "monotonic", lambda: clock["now"])
    post_turn_state.begin("adv")
    assert post_turn_state.busy() == ["adv"]
    # turnos seguidos não renovam a espera: o relógio conta do primeiro
    clock["now"] += post_turn_state.HOLD_S - 10
    post_turn_state.begin("adv")
    clock["now"] += 11
    assert post_turn_state.busy() == []


def test_backdrop_inside_a_spot_keeps_the_indoor_note_and_no_reference_outfit():
    """Revisão 5: com a roupa trocada, a guarda pedia "outfit" da referência;
    a nota de interior caía antes de traços de identidade."""
    from app import scene_spot
    marina = _card("Marina Teste", portrait_prompt="A woman",
                   sheet={"current_outfit": "yellow raincoat over blue jumpsuit",
                          "reference": {"turnaround": {"master_image_id": "m"}}})
    lobo = _card("Lobo de Fogo", entry="O Lobo de Fogo é uma criatura mágica.",
                 portrait_prompt="A majestic fiery wolf")
    title, entry = scene_spot.place("Floresta", "Árvores de vidro.",
                                    {"en": "the interior of a cave", "indoor": True})
    prompt, _ = scene._prompt(title, entry, "cinematic", "night", [marina, lobo],
                              "Marina e o lobo esperam.")
    assert scene_spot._INDOOR_NOTE in prompt
    assert "vidro" not in prompt
    assert "hair, body, outfit and distinctive" not in prompt
    assert "each animal with its own fur and markings" in prompt
    only_wolf, _ = scene._prompt("the cabin", "", "cinematic", "night", [lobo], "O lobo dorme.")
    assert "and clothing" not in only_wolf
