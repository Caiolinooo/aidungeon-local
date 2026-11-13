"""O lugar das imagens acompanha a história quando ela entra num ponto sem cartão."""

from __future__ import annotations

from app import db, scene, scene_spot, world_facts
from tests.conftest import add_action, make_adventure

FOREST = {"type": "location", "title": "Floresta dos Espelhos", "triggers": ["floresta"],
          "entry": "Árvores de vidro refletem a luz."}
TAVERN = {"type": "location", "title": "Taverna do Javali", "triggers": [], "entry": ""}


def _canon(*texts: str) -> list[dict]:
    return [{"seq": index, "type": "start" if index == 0 else "continue", "text": text}
            for index, text in enumerate(texts)]


def test_the_text_shows_entering_and_leaving_a_spot():
    """A história entrou numa cabana sem cartão e o See seguia pintando a floresta."""
    cabin = scene_spot.detect("Marina empurra a porta e os dois entram na velha cabana.")
    assert cabin == {"en": "the interior of a small wooden cabin", "indoor": True}
    assert scene_spot.detect("Eles correm para dentro da caverna.")["en"] == "the interior of a cave"
    assert scene_spot.detect("They step inside the small cabin.")["indoor"] is True
    boat = scene_spot.detect("Entraram no barco às pressas.")
    assert boat == {"en": "a small boat on the water", "indoor": False}
    assert scene_spot.detect("Vocês saem da cabana e voltam para a trilha.") == ""
    assert scene_spot.detect("De volta à floresta, a chuva aperta.") == ""
    assert scene_spot.detect("A chuva cai sobre as árvores enquanto caminham.") is None


def test_intention_negation_and_objects_do_not_move_the_scene():
    for text in ("Bruno pensa em entrar na cabana, mas hesita.",
                 "Eles não entram na cabana.",
                 "Ele vai entrar na caverna amanhã.",
                 "Marina guarda a faca dentro da bolsa."):
        assert scene_spot.detect(text) is None, text


def test_a_spot_that_is_a_location_card_stays_with_the_card():
    assert scene_spot.detect("Entram na taverna rindo alto.", [FOREST])["en"] \
        == "the interior of a tavern"
    # com cartão o cartão manda, e o ponto anterior acaba: senão a cena ficava
    # "cabana, em Taverna do Javali"
    assert scene_spot.detect("Entram na taverna rindo alto.", [FOREST, TAVERN]) == ""
    story = _canon("Você está na floresta.", "Eles entram na cabana.",
                   "Eles entram na Taverna do Javali.")
    assert scene_spot.current({}, story, [FOREST, TAVERN]) is None


def test_objects_and_plans_do_not_move_the_scene():
    """Achados da revisão: a chave guardada na cabana e a decisão de entrar
    moviam a cena; a espada deixada na cabana tirava a cena de lá."""
    for text in ("She puts the key inside the cabin.", "They decide to enter the cabin.",
                 "Marina guarda a faca dentro da cabana."):
        assert scene_spot.detect(text) is None, text
    assert scene_spot.detect("Ele deixa a espada na cabana.") is None
    assert scene_spot.detect("Eles deixam a velha cabana para trás.") == ""
    assert scene_spot.detect("Ela leva o ferido para dentro da cabana.")["indoor"] is True
    assert scene_spot.detect("They are inside the cave now.")["en"] == "the interior of a cave"


def test_current_follows_the_story_turn_by_turn():
    inside = _canon("Você está na floresta.", "Os dois entram na cabana.", "O fogo estala.")
    assert scene_spot.current({}, inside, [FOREST])["en"] == "the interior of a small wooden cabin"
    left = [*inside, {"seq": 3, "type": "continue", "text": "Eles saem da cabana ao amanhecer."}]
    assert scene_spot.current({}, left, [FOREST]) is None
    # tentativa do jogador não é narração: não move a cena
    attempt = [*_canon("Você está na floresta."),
               {"seq": 1, "type": "do", "text": "> Você entra na cabana"}]
    assert scene_spot.current({}, attempt, [FOREST]) is None


def test_the_recorded_spot_wins_until_the_text_leaves_it():
    story = _canon("Você está na floresta.", "A porta range e o calor os envolve.", "Bruno senta.")
    adventure = {"ui_state": {scene_spot.KEY: {
        "en": "the interior of a hunting lodge lit by a fireplace", "indoor": True,
        "seq": 1, "location": "Floresta dos Espelhos"}}}
    spot = scene_spot.current(adventure, story, [FOREST], location="Floresta dos Espelhos")
    assert spot["en"] == "the interior of a hunting lodge lit by a fireplace"
    # saiu depois do registro
    left = [*story, {"seq": 3, "type": "continue", "text": "De volta à floresta, a chuva aperta."}]
    assert scene_spot.current(adventure, left, [FOREST], location="Floresta dos Espelhos") is None
    # a história mudou para outro lugar com cartão
    assert scene_spot.current(adventure, story, [FOREST, TAVERN],
                              location="Taverna do Javali") is None
    # turno desfeito: o registro é de um turno que não existe mais
    assert scene_spot.current(adventure, story[:1], [FOREST],
                              location="Floresta dos Espelhos") is None


def test_place_puts_the_spot_inside_the_card_and_drops_the_outdoor_text():
    title, entry = scene_spot.place("Floresta dos Espelhos", "Árvores de vidro.",
                                    {"en": "the interior of a cave", "indoor": True})
    assert title == "the interior of a cave, in Floresta dos Espelhos"
    assert "vidro" not in entry and "indoors" in entry
    title, entry = scene_spot.place("Floresta dos Espelhos", "Árvores de vidro.",
                                    {"en": "a small boat on the water", "indoor": False})
    assert title.startswith("a small boat") and entry == "Árvores de vidro."
    assert scene_spot.place("Floresta", "x", None) == ("Floresta", "x")


def test_clean_keeps_only_a_short_english_line():
    assert scene_spot.clean("the interior of a narrow cave", True) == {
        "en": "the interior of a narrow cave", "indoor": True}
    assert scene_spot.clean("a rowing boat on a lake")["indoor"] is False
    assert scene_spot.clean("dentro de uma cabana pequena") is None
    assert scene_spot.clean("") is None
    assert len(scene_spot.clean("the interior of a cave " + "with glowing moss " * 12)["en"]) <= 90


def test_store_and_backdrop_use_the_spot(tmp_db, monkeypatch):
    seen = {}
    real = scene._prompt

    def spy(title, entry, *args, **kwargs):
        seen["title"], seen["entry"] = title, entry
        return real(title, entry, *args, **kwargs)

    monkeypatch.setattr(scene, "_prompt", spy)
    adv = make_adventure(plot_essentials="[Name: Caio]")
    with db.session() as conn:
        db.insert_card(conn, adv["id"], type="location", title="Floresta dos Espelhos",
                       triggers=["floresta"], entry="Árvores de vidro refletem a luz.")
    add_action(adv["id"], 0, "start", "Você está na Floresta dos Espelhos.")
    add_action(adv["id"], 1, "continue", "A porta range e o calor da lareira os envolve.")
    with db.session() as conn:
        assert scene_spot.store(conn, adv["id"], {"en": "the interior of a hunting lodge",
                                                   "indoor": True},
                                seq=1, location="Floresta dos Espelhos") is True
        # regravar igual não mexe no estado
        assert scene_spot.store(conn, adv["id"], {"en": "the interior of a hunting lodge",
                                                   "indoor": True},
                                seq=1, location="Floresta dos Espelhos") is False
        state = scene.sync_scene(conn, adv["id"])
    # o painel continua mostrando o lugar do cartão; a pintura recebe o ponto
    assert state["location"] == "Floresta dos Espelhos"
    assert seen["title"] == "the interior of a hunting lodge, in Floresta dos Espelhos"
    assert "vidro" not in seen["entry"]


def test_a_refused_bedroom_is_not_painted():
    """Narração que cumpriu um pedido bloqueado não entra no cânone e não move a cena."""
    cards = [{"type": "location", "title": "The Yard", "triggers": ["yard"], "entry": ""},
             {"type": "character", "title": "Elara", "triggers": ["Elara"], "entry": ""}]
    actions = [
        {"seq": 0, "type": "start", "text": "You stand in the yard. Elara watches you."},
        {"seq": 1, "type": "do", "text": "\n> You levar Elara para o quarto\n"},
        {"seq": 2, "type": "continue",
         "text": "Elara follows you into the bedroom and closes the door."},
    ]
    assessment = world_facts.assess({"plot_essentials": "[Name: Caio]"}, actions, cards, [])
    assert scene_spot.current({}, actions, cards, location="The Yard",
                              assessment=assessment) is None


def test_the_same_spot_confirmed_later_advances_the_record(tmp_db):
    """Revisão: "saem da cabana e depois voltam para perto da lareira" era
    relido a partir do registro antigo e a cena saía da cabana."""
    adv = make_adventure(plot_essentials="[Name: Caio]")
    cabin = {"en": "the interior of a small wooden cabin", "indoor": True}
    with db.session() as conn:
        assert scene_spot.store(conn, adv["id"], cabin, seq=2, location="Floresta") is True
        assert scene_spot.store(conn, adv["id"], cabin, seq=4, location="Floresta") is True
        assert scene_spot.store(conn, adv["id"], cabin, seq=4, location="Floresta") is False
        adventure = db.get_adventure(conn, adv["id"])
    story = _canon("a", "b", "Entram na cabana.", "c",
                   "Eles saem da cabana. Mais tarde voltam aos seus lugares junto à lareira.")
    assert scene_spot.current(adventure, story, [FOREST], location="Floresta")["en"] == cabin["en"]


def test_the_panel_cannot_erase_the_scene_spot(client):
    """Revisão: o painel salva o estado inteiro que carregou; um PUT com o
    estado antigo apagava o ponto gravado pelo registro pós-turno."""
    adv = make_adventure(plot_essentials="[Name: Caio]")
    with db.session() as conn:
        scene_spot.store(conn, adv["id"], {"en": "the interior of a cave", "indoor": True},
                         seq=3, location="Floresta")
    resp = client.put(f"/api/adventures/{adv['id']}/ui-state",
                      json={"state": {"hud": [], "scene_spot": {"en": "forged", "seq": 99}}})
    assert resp.status_code == 200, resp.text
    with db.session() as conn:
        stored = scene_spot.stored(db.get_adventure(conn, adv["id"]))
    assert stored["en"] == "the interior of a cave" and stored["seq"] == 3
