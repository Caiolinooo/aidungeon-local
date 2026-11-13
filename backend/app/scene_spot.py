"""Ponto atual da cena dentro do lugar da história.

O lugar das imagens vinha só do cartão de lugar citado por último. A história
entrava numa cabana, numa caverna ou num barco sem cartão e o See e o fundo
continuavam pintando a floresta do cartão.

Duas fontes, as duas em inglês para o modelo de imagem:

- `detect`: lê o texto narrado na hora ("entram na cabana", "dentro da
  caverna", "saem do barco"). Não usa LLM, então vale já no See e no fundo
  pedidos logo depois do turno.
- o registro pós-turno (`npc_chronicle`) informa o ponto com as próprias
  palavras e `store` grava em `ui_state["scene_spot"]` com o seq do turno.

`current` junta as duas: parte do ponto gravado e aplica as entradas e saídas
que o texto mostrou depois dele. Um ponto gravado num turno desfeito (seq maior
que o último turno) ou em outro lugar com cartão é ignorado.
"""

from __future__ import annotations

import re
import unicodedata

from app import db

KEY = "scene_spot"
_LOOKBACK = 8
_MAX_CHARS = 90

# (frase em inglês, interno?, palavras em português, palavras em inglês)
_SPOTS: tuple[tuple[str, bool, str, str], ...] = (
    ("the interior of a small wooden cabin", True,
     r"cabanas?|choupanas?|casebres?|cabines?", r"cabins?|huts?|shacks?"),
    ("the interior of a cave", True, r"cavernas?|grutas?|covis?", r"caves?|caverns?|grottos?"),
    ("the interior of a tavern", True,
     r"tavernas?|estalagens?|hospedarias?|pousadas?", r"taverns?|inns?"),
    ("the interior of a bedroom", True, r"quartos?|aposentos?|dormit[oó]rios?", r"bedrooms?"),
    ("the interior of a kitchen", True, r"cozinhas?", r"kitchens?"),
    ("the interior of a cellar", True, r"por[aã]o|por[oõ]es|adegas?", r"cellars?|basements?"),
    ("the interior of a stone tower", True, r"torres?", r"towers?"),
    ("the interior of a tent", True, r"tendas?|barracas?", r"tents?"),
    ("the interior of a barn", True, r"celeiros?", r"barns?"),
    ("the interior of a temple", True, r"templos?|santu[aá]rios?", r"temples?|shrines?"),
    ("the interior of a chapel", True, r"capelas?|igrejas?", r"chapels?|churches?"),
    ("the interior of a shop", True, r"lojas?|armaz[eé]ns?|armaz[eé]m", r"shops?|stores?"),
    ("the interior of a carriage", True, r"carruagens?|carruagem|carro[cç]as?",
     r"carriages?|wagons?"),
    ("the interior of a tunnel", True, r"t[uú]neis|t[uú]nel|minas?|esgotos?",
     r"tunnels?|mines?|sewers?"),
    ("the interior of a dungeon cell", True, r"celas?|masmorras?|calabou[cç]os?",
     r"cells?|dungeons?"),
    ("the interior of a library", True, r"bibliotecas?", r"librar(?:y|ies)"),
    ("the interior of a bathroom", True, r"banheiros?", r"bathrooms?"),
    ("the interior of a stable", True, r"est[aá]bulos?", r"stables?"),
    ("the interior of a great hall", True, r"sal[aã]o|sal[oõ]es|castelos?|pal[aá]cios?",
     r"halls?|castles?|palaces?"),
    ("the interior of a crypt", True, r"criptas?|tumbas?|catacumbas?", r"crypts?|tombs?|catacombs?"),
    ("the interior of a forge workshop", True, r"forjas?|oficinas?|ferrarias?",
     r"forges?|workshops?|smith(?:y|ies)"),
    ("the interior of a mill", True, r"moinhos?", r"mills?"),
    ("the interior of a lighthouse", True, r"far[oó]is|farol", r"lighthouses?"),
    ("the interior of an infirmary", True, r"enfermarias?|hospitais|hospital",
     r"infirmar(?:y|ies)|hospitals?"),
    ("the interior of a laboratory", True, r"laborat[oó]rios?", r"laborator(?:y|ies)|labs?"),
    ("the interior of an office", True, r"escrit[oó]rios?", r"offices?"),
    ("the interior of a bar", True, r"bares|bar|boates?", r"bars?|nightclubs?"),
    ("the interior of a restaurant", True, r"restaurantes?", r"restaurants?"),
    ("the interior of a train car", True, r"trens?|trem|vag[aã]o|vag[oõ]es", r"trains?|train cars?"),
    ("the interior of a car", True, r"carros?", r"cars?"),
    ("the interior of a house", True, r"casas?", r"houses?|cottages?"),
    ("the interior of a room", True, r"salas?|c[oô]modos?", r"rooms?|chambers?"),
    ("a small boat on the water", False, r"barcos?|botes?|canoas?|jangadas?",
     r"boats?|canoes?|rafts?"),
    ("the deck of a ship", False, r"navios?|conv[eé]s|embarca[cç][aã]o|embarca[cç][oõ]es",
     r"ships?|decks?"),
    ("a bridge", False, r"pontes?", r"bridges?"),
    ("a beach", False, r"praias?", r"beach(?:es)?"),
    ("a forest clearing", False, r"clareiras?", r"clearings?"),
    ("a campsite around a fire", False, r"acampamentos?", r"camps?|campsites?"),
    ("a rooftop", False, r"telhados?", r"rooftops?|roofs?"),
    ("a garden", False, r"jardins?|jardim", r"gardens?"),
    ("a graveyard", False, r"cemit[eé]rios?", r"graveyards?|cemeter(?:y|ies)"),
    ("a market street", False, r"mercados?|feiras?", r"markets?"),
    ("a courtyard", False, r"p[aá]tios?", r"courtyards?"),
    ("a balcony", False, r"varandas?|sacadas?", r"balcon(?:y|ies)"),
    ("stone ruins", False, r"ru[ií]nas?", r"ruins?"),
)

_ADJ = r"(?:[\wÀ-ÿ'’-]+\s+){0,2}?"
_ENTER_PT = (
    # Infinitivo fica de fora: "pensa em entrar", "quer entrar" são intenção.
    r"(?:\b(?:entr|adentr|refugi|abrig|embarc|invad|instal)(?!ar\b)[\wÀ-ÿ-]*\b[^.!?\n]{0,40}?"
    r"\b(?:n[ao]s?|em|numa?|nuns|numas|à|ao|aos|às)\s+(?:(?:um|uma|uns|umas)\s+)?"
    r"|\b(?:para\s+)?dentro\s+d(?:[ao]s?|e(?:\s+uma?)?)\s+"
    r"|\bno\s+interior\s+d(?:[ao]s?|e(?:\s+uma?)?)\s+"
    r"|\ba\s+bordo\s+d(?:[ao]s?|e(?:\s+uma?)?)\s+"
    r"|\bsob\s+o\s+teto\s+d(?:[ao]s?|e(?:\s+uma?)?)\s+)"
)
_ENTER_EN = (
    r"(?:\b(?:enter(?:s|ed|ing)?|board(?:s|ed|ing)?)\s+(?:the|a|an|his|her|their)\s+"
    r"|\b(?:inside|into|aboard|within)\s+(?:the|a|an|his|her|their)\s+)"
)
_LEAVE_PT = (
    # "Saem da cabana" e "deixam a cabana". "Ele deixa a espada na cabana" não
    # é saída: quem fica na cabana é a espada.
    r"(?:\b(?:sai|saem|saiu|sa[ií]ram|sa[ií]mos|saindo|fog[\wÀ-ÿ]*|escap[\wÀ-ÿ]*|"
    r"desembarc[\wÀ-ÿ]*)\b[^.!?\n]{0,30}?\bd[ao]s?\s+"
    r"|\b(?:deix[\wÀ-ÿ]*|abandon[\wÀ-ÿ]*)\s+(?:[ao]s?)\s+"
    r"(?![\wÀ-ÿ-]+\s+(?:n[ao]s?|em|numa?|dentro)\b))"
)
_LEAVE_EN = (
    r"\b(?:leav(?:e|es|ing)|left|exit(?:s|ed|ing)?|step(?:s|ped)?\s+out\s+of|"
    r"walk(?:s|ed)?\s+out\s+of|out\s+of)\s+(?:the|a|an|his|her|their)\s+"
)
# "Saem para a trilha", "de volta à floresta": saída sem repetir o nome do ponto.
_OUTSIDE = re.compile(
    r"(?i)\b(?:de\s+volta\s+(?:à|ao|para\s+[ao])|sa(?:i|em|iu|[ií]ram|[ií]mos)\s+para\s+(?:a|o))\s+"
    r"(?:floresta|mata|trilha|estrada|rua|campo|bosque|praia|ar\s+livre)\b"
    r"|\bback\s+(?:out\s+)?(?:into|onto|on|to)\s+the\s+(?:forest|woods|trail|road|street|open)\b"
)
# Entrada negada, futura ou só desejada não muda o lugar.
_NOT_YET = re.compile(
    r"(?i)\b(?:n[aã]o|sem|nunca|antes\s+de|quer(?:em|ia|iam)?|pretend\w+|tent\w+|"
    r"poder\w*|dever\w*|se\s+voc[eê]|vai|v[aã]o|ir[aáã]o?|precis\w+|"
    r"not|never|without|before|wants?\s+to|tr(?:y|ies|ied)\s+to|about\s+to|"
    r"could|should|will|would)\b[^.!?\n]{0,18}$"
    r"|\bto\s*$"
)
# "She puts the key inside the cabin", "guarda a faca dentro da cabana": quem
# entra é o objeto, não a cena.
_PLACED = re.compile(
    r"(?i)\b(?:puts?|plac(?:e|es|ed)|leaves?|left|throws?|threw|toss(?:es|ed)?|hid(?:e|es)?|"
    r"drops?|dropped|sets?|stor(?:e|es|ed)|keeps?|kept|"
    r"coloc[\wÀ-ÿ]*|p[oõ]e[\wÀ-ÿ]*|deix[\wÀ-ÿ]*|jog[\wÀ-ÿ]*|escond[\wÀ-ÿ]*|guard[\wÀ-ÿ]*|"
    r"larg[\wÀ-ÿ]*)\b[^.!?\n]{0,40}$"
)


_BARE = frozenset({"inside", "into", "aboard", "within", "dentro", "para", "no", "a", "sob"})


def _compile(prefix_pt: str, prefix_en: str) -> list[tuple[re.Pattern[str], str, bool]]:
    out = []
    for phrase, indoor, pt, en in _SPOTS:
        pattern = re.compile(
            rf"(?i)(?:{prefix_pt}{_ADJ}(?:{pt})|{prefix_en}{_ADJ}(?:{en}))\b")
        out.append((pattern, phrase, indoor))
    return out


_ENTER = _compile(_ENTER_PT, _ENTER_EN)
_LEAVE = _compile(_LEAVE_PT, _LEAVE_EN)


def _fold(text: str) -> str:
    plain = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in plain if not unicodedata.combining(ch)).casefold()


def _card_words(cards: list[dict] | None) -> str:
    """Títulos e gatilhos dos cartões de lugar: um ponto que já é cartão fica
    com o cartão (ele tem descrição e imagem próprias)."""
    bits = []
    for card in cards or []:
        if (card.get("type") or "") != "location":
            continue
        bits.append(str(card.get("title") or ""))
        bits.extend(str(item) for item in (card.get("triggers") or []))
    return _fold(" ".join(bits))


def detect(text: str, cards: list[dict] | None = None) -> dict | str | None:
    """Última entrada ou saída de um ponto no texto.

    Devolve `{"en", "indoor"}` quando a cena entrou num ponto, `""` quando saiu
    e `None` quando o texto não mostra nem uma coisa nem outra.
    """
    text = text or ""
    if not text.strip():
        return None
    known = _card_words(cards)
    events: list[tuple[int, dict | str]] = []
    for pattern, phrase, indoor in _ENTER:
        for match in pattern.finditer(text):
            before = text[max(0, match.start() - 40):match.start()]
            if _NOT_YET.search(before):
                continue
            # A preposição sozinha ("inside the", "dentro da") também vale
            # para um objeto guardado ali.
            head = match.group(0).lstrip().split()[0].casefold()
            if head in _BARE and _PLACED.search(before):
                continue
            word = _fold(match.group(0).split()[-1])
            if word and re.search(rf"\b{re.escape(word)}\b", known):
                # Entraram num lugar que tem cartão: o cartão manda e o ponto
                # anterior acaba (senão ficava "cabana, em Taverna do Javali").
                events.append((match.end(), ""))
                continue
            events.append((match.end(), {"en": phrase, "indoor": indoor}))
    for pattern, _phrase, _indoor in _LEAVE:
        for match in pattern.finditer(text):
            if _NOT_YET.search(text[max(0, match.start() - 40):match.start()]):
                continue
            events.append((match.end(), ""))
    for match in _OUTSIDE.finditer(text):
        events.append((match.end(), ""))
    if not events:
        return None
    return max(events, key=lambda item: item[0])[1]


_PORTUGUESE = re.compile(
    r"(?i)\b(?:uma?|d[ao]s?|n[ao]s?|com|para|dentro|pequen[ao]|velh[ao]|cabana|caverna|"
    r"floresta|barco|quarto|sala|casa)\b|[ãõáéíóúâêôç]")
_UNSAFE = re.compile(r"(?i)\b(?:nude|naked|topless|sex|undress\w*)\b")
_INDOOR_WORDS = re.compile(
    r"(?i)\b(?:interior|inside|indoors?|room|cabin|hut|cave|cellar|hall|tent|tavern|inn|"
    r"house|cell|tunnel|chamber|library|kitchen|shop|chapel|temple|crypt|barn|stable)\b")


def clean(value, indoor=None) -> dict | None:
    """Frase do registro pós-turno: uma linha curta em inglês, ou nada."""
    text = " ".join(str(value or "").split()).strip(" .,;:\"'")
    if not text or len(text) < 4:
        return None
    if _PORTUGUESE.search(text) or _UNSAFE.search(text):
        return None
    if len(text) > _MAX_CHARS:
        cut = text[:_MAX_CHARS]
        space = cut.rfind(" ")
        text = (cut[:space] if space > 0 else cut).rstrip(" ,;:")
    inside = bool(indoor) if isinstance(indoor, bool) else bool(_INDOOR_WORDS.search(text))
    return {"en": text, "indoor": inside}


def stored(adventure: dict | None) -> dict | None:
    state = (adventure or {}).get("ui_state")
    raw = state.get(KEY) if isinstance(state, dict) else None
    if not isinstance(raw, dict):
        return None
    try:
        seq = int(raw.get("seq") or 0)
    except (TypeError, ValueError):
        return None
    return {
        "en": " ".join(str(raw.get("en") or "").split()),
        "indoor": bool(raw.get("indoor")),
        "seq": seq,
        "location": " ".join(str(raw.get("location") or "").split()),
    }


def store(conn, adventure_id: str, spot: dict | None, *, seq: int | None,
          location: str = "") -> bool:
    """Grava o ponto (ou a saída dele, `spot=None`) no estado da aventura."""
    adventure = db.get_adventure(conn, adventure_id) or {}
    state = adventure.get("ui_state") if isinstance(adventure.get("ui_state"), dict) else {}
    entry = {
        "en": (spot or {}).get("en") or "",
        "indoor": bool((spot or {}).get("indoor")),
        "seq": int(seq or 0),
        "location": " ".join(str(location or "").split()),
    }
    before = state.get(KEY) if isinstance(state.get(KEY), dict) else None
    if before is None and not entry["en"]:
        return False
    # O seq entra na comparação: o mesmo ponto confirmado num turno mais novo
    # avança o registro. Sem isso, "saem da cabana e depois voltam para perto
    # da lareira" era relido a partir do registro antigo e a cena saía de lá.
    if before and all(before.get(key) == entry[key]
                      for key in ("en", "indoor", "location", "seq")):
        return False
    db.update_adventure_ui_state(conn, adventure_id, {**state, KEY: entry})
    return True


_PRIVATE_ROOM = re.compile(r"(?i)\b(?:bedroom|bathroom|room|chamber|suite)\b")


def current(adventure: dict | None, actions: list[dict], cards: list[dict] | None = None,
            *, location: str = "", assessment=None) -> dict | None:
    """Ponto em que a cena está agora, ou None quando é o próprio lugar.

    Com `assessment`, só vale a narração aceita no cânone: uma continuação que
    cumpriu um pedido bloqueado ("levar Elara para o quarto") não muda o lugar,
    e um quarto que o mundo recusou não é pintado.
    """
    from app import world_facts
    canon = [
        action for action in actions or []
        if (action.get("type") or "") in world_facts.CANON_TYPES and (action.get("text") or "").strip()
    ]
    if assessment is not None:
        allowed = set(assessment.accepted or [])
        canon = [action for action in canon if (action.get("text") or "") in allowed]
    spot = _current(adventure, canon, cards, location)
    if spot and assessment is not None and assessment.ban_room \
            and _PRIVATE_ROOM.search(spot["en"]):
        return None
    return spot


def _current(adventure: dict | None, canon: list[dict], cards: list[dict] | None,
             location: str) -> dict | None:
    if not canon:
        return None
    latest = int(canon[-1].get("seq") or 0)
    spot: dict | None = None
    since = None
    saved = stored(adventure)
    if saved and saved["seq"] <= latest and (
            not saved["location"] or not location
            or _fold(saved["location"]) == _fold(location)):
        since = saved["seq"]
        if saved["en"]:
            spot = {"en": saved["en"], "indoor": saved["indoor"]}
    if since is None:
        tail = canon[-_LOOKBACK:]
    else:
        tail = [action for action in canon if int(action.get("seq") or 0) > since]
    for action in tail:
        event = detect(action.get("text") or "", cards)
        if event is None:
            continue
        spot = event or None
    return spot


_INDOOR_NOTE = ("indoors, enclosed by walls and a ceiling, the outdoors seen only "
                "through a door or a window")


def place(title: str, entry: str, spot: dict | None) -> tuple[str, str]:
    """(lugar, descrição) para o prompt: o ponto dentro do lugar do cartão.

    Num ponto interno a descrição do cartão sai: ela descreve o lado de fora e
    o modelo pintava as árvores do cartão dentro da cabana.
    """
    if not spot or not spot.get("en"):
        return title, entry
    known = title and title != "the current scene"
    name = f"{spot['en']}, in {title}" if known else spot["en"]
    if spot.get("indoor"):
        return name, _INDOOR_NOTE
    return name, entry
