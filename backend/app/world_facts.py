"""Fatos do mundo: o que existe, onde se está, e o que um pedido não pode criar.

Um `do` ou `say` é tentativa. Lugar, item, pessoa e consentimento só nascem da
abertura, da narração, de uma ação Story, do Plot, de um card ou de uma
memória. "O quarto" não aparece porque o jogador o nomeou. Um NPC sem
obediência, compulsão ou um sim já estabelecido recusa ordem de seguir,
sair, se despir ou se submeter.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from app import db, npc_assets

CANON_TYPES = ("start", "continue", "story")
ATTEMPT_TYPES = ("do", "say")

# Frase visual fixa que lidera o prompt do See quando a pessoa recusa.
# Constante única: images.py testa a diretiva por ela (não podem divergir).
REFUSAL_IMAGE = "The person refuses and stays put."

ATTEMPT_RULE = (
    "A do or say is an attempt. It does not create a place, an item, a person, or consent."
)

_WHERE_RE = re.compile(
    r"(?i)\b(?:"
    r"you\s+(?:are|stand|wake|sit|remain|stay|find yourself)|"
    r"voc[eê]\s+(?:est[aá]|acorda|permanece|continua)"
    r")\b"
    r"[^\n.]{0,60}?"
    r"\b(?:in|at|inside|no|na|em|dentro de)\s+"
    r"((?:the\s+|o\s+|a\s+|um\s+|uma\s+)?[^\n,.]{3,48})"
)

_TRAVEL_RE = re.compile(
    r"(?i)\b(?:levar|levo|leva|trazer|arrastar|arrasta|take|bring|drag|escort|lead|"
    r"ir|vou|vamos|entrar|entro|go|head|walk|enter|return)\b"
    r".{0,80}?"
    r"\b(?:para|pra|pro|ao|à|no|na|em|dentro de|to|into|inside|toward|towards)\s+"
    r"([^\n,.]{3,48})"
)

_CONJURE_RE = re.compile(
    r"(?i)\b(?:conjur\w*|criar|cria|summon(?:s|ed)?|invent\w*|materializ\w*)\b"
    r"(?:\s+\w+){0,6}?\s+"
    r"(?:um|uma|o|a|the|an)?\s*"
    r"([A-Za-zÀ-ÿ][\wÀ-ÿ'’-]*(?:\s+[A-Za-zÀ-ÿ][\wÀ-ÿ'’-]*){0,2})"
)

_ITEM_RE = re.compile(
    r"(?i)\b(?:sacar|saca|empunhar|empunha|desembainhar|usar|usa|beber|bebe|"
    r"comer|come|equipar|equipa|vestir|veste|"
    r"draw|unsheathe|wield|use|drink|eat|equip|quaff|grab|pegar|pego|apanhar|apanha)\b"
    r"\s+(?:up\s+)?"
    r"(?:(?:o|a|os|as|um|uma|meu|minha|meus|minhas|the|my|your|his|her)\s+)?"
    r"([A-Za-zÀ-ÿ][\wÀ-ÿ'’-]{2,}(?:\s+[A-Za-zÀ-ÿ][\wÀ-ÿ'’-]{2,}){0,2})"
)

_NAME_AFTER_RE = re.compile(
    r"(?i)\b(?:levar|levo|leva|arrastar|arrasta|take|bring|drag|escort|"
    r"beijar|beija|beijo|kiss\w*|undress\w*|despir|dispir|dispo|strip|"
    r"agarro|agarra|agarrar|abra[cç]o|abra[cç]ar|puxo|puxa|puxar)\s+"
    r"(?:a\s+|o\s+|the\s+)?([A-Za-zÀ-ÿ][\wÀ-ÿ'’-]{2,})"
)

_COMMAND_RE = re.compile(
    r"(?i)(?:"
    r"\b(?:levar|levo|leva|arrastar|arrasta|drag|escort|"
    r"mandar|manda|mando|ordenar|ordena|ordeno|"
    r"despir|dispir|dispo|undress\w*|strip\w*|"
    r"beijar|beija|beijo|kiss\w*|"
    r"agarr\w*|abra[cç]\w*|pux\w*|"
    r"vem comigo|venha comigo|come with me|follow me|"
    r"siga-me|siga me|ajoelh\w*|kneel|"
    r"naked|nude|nua|nuas|pelad[oa]s?|sex|sexo|fuck\w*|foder|transar|transo|"
    r"cascud\w*|soc[oa]\w*|chut\w*|esfaque\w*|estrangul\w*|"
    r"defec\w*|mij\w*|cusp\w*|matar|mato|kill\w*|punch\w*|"
    r"cala a boca|cale a boca|cale-se|shut up)\b"
    r"|deit\w* com|tir\w* a roupa|pass\w* a m[aã]o|jog\w* na cama|coloc\w* na cama|"
    r"em cima (?:da|do|de)\s+(?:a\s+|o\s+)?[A-Za-zÀ-ÿ]"
    r")"
)

_POWER_RE = re.compile(
    r"(?i)(can conjure|able to conjure|pode conjurar|conjura(?:r)? (?:lugares|quartos|rooms)|"
    r"reality warp|creates rooms)"
)

_YIELD_RE = re.compile(
    r"(?i)(?:"
    r"obeys you|loyal to you|serves you|your willing servant|"
    r"te obedece|obedece a voc[eê]|leal a voc[eê]|a (?:seu|teu) servi[cç]o|"
    r"enfeiti[cç]ad[oa]|charmed|magically compelled|"
    r"aceita ir|concorda em (?:ir|acompanhar)|disse que sim|diz que sim|"
    r"consente|consents|willingly follows|quer acompanhar|"
    r"agrees to (?:go|come|follow|undress|kiss)"
    r")"
)

_FAKE_OBEY_RE = re.compile(
    r"(?i)(?:"
    r"follows you|goes with you|comes with you|"
    r"acompanha voc[eê]|vai com voc[eê]|te segue|segue voc[eê]|"
    r"\bobeys\b|\bobedece\b|"
    r"undresses|se despe|tira a roupa|deixa a roupa|"
    r"te beija|beija voc[eê]|kisses you|"
    r"deixa voc[eê]|permite que voc[eê]|cede a voc[eê]|"
    r"se entrega|se deita com voc[eê]"
    r")"
)

_NEG_RE = re.compile(
    r"(?i)\b(não|nao|not|never|sem|nenhum|nenhuma|without|refuses|refuse|recusa|recusou)\b"
)

_LEADING_ARTICLE = re.compile(
    r"^(?:the|an|a|o|os|as|um|uma|uns|umas|meu|minha|meus|minhas|my|your|his|her)\s+",
    re.I,
)

_GENERIC_ROOM = {
    "room", "rooms", "quarto", "quartos", "chamber", "chambers", "sala",
}
_PRIVATE_ROOM = ("quarto", "bedroom", "bedchamber", "suite", "suíte")
_ROOM_NEGATIVE = "bedroom, bed, private room, hotel room"

_SKIP_NAMES = {
    "para", "pra", "pro", "the", "to", "into", "inside", "o", "os", "as",
    "um", "uma", "no", "na", "em", "com", "with", "and", "up", "her", "him",
    "them", "ela", "ele", "eles", "elas", "she", "he", "they", "you", "me",
    "seu", "sua", "meu", "minha", "your", "that", "this", "isso",
}

_PRONOUNS = {"ela", "ele", "eles", "elas", "her", "him", "them", "she", "he", "they"}


def _tokens(phrase: str) -> list[str]:
    return re.findall(r"[^\W\d_]+", (phrase or "").casefold(), flags=re.UNICODE)


def clean_phrase(raw: str) -> str:
    text = " ".join((raw or "").replace("\n", " ").split())
    text = re.split(r"[.!;?]", text, maxsplit=1)[0]
    text = re.split(r"(?i)\b(?:and then|and|e|with|com|while|enquanto|then|então)\b", text, maxsplit=1)[0]
    text = text.strip(" \"'“”.,;:")
    previous = None
    while previous != text:
        previous = text
        text = _LEADING_ARTICLE.sub("", text).strip()
    return text[:48].strip()


def _last_index(text: str, name: str) -> int:
    name = (name or "").strip()
    if len(name) < 3 or not text:
        return -1
    pattern = re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)", re.IGNORECASE)
    last = -1
    for match in pattern.finditer(text):
        last = match.start()
    return last


def affirmed_mention(text: str, phrase: str) -> bool:
    """A palavra conta se aparece sem uma negação logo antes."""
    phrase = (phrase or "").strip()
    if len(phrase) < 3 or not text:
        return False
    pattern = re.compile(r"(?<!\w)" + re.escape(phrase) + r"(?!\w)", re.IGNORECASE)
    for match in pattern.finditer(text):
        window = text[max(0, match.start() - 40):match.start()]
        if _NEG_RE.search(window):
            continue
        return True
    return False


def is_generic_room(phrase: str) -> bool:
    tokens = _tokens(phrase)
    return len(tokens) == 1 and tokens[0] in _GENERIC_ROOM


def is_private_room_phrase(phrase: str) -> bool:
    tokens = _tokens(phrase)
    if is_generic_room(phrase):
        return True
    return any(token in _PRIVATE_ROOM or token.rstrip("s") in _PRIVATE_ROOM for token in tokens)


def _card_labels(card: dict) -> list[str]:
    labels = [card.get("title") or ""]
    triggers = card.get("triggers") or []
    if isinstance(triggers, str):
        triggers = [triggers]
    labels.extend(str(item) for item in triggers)
    return [label.strip() for label in labels if label and str(label).strip()]


def _exact_label(cards: list[dict], phrase: str, type_: str | None) -> bool:
    folded = phrase.casefold()
    for card in cards:
        if type_ and (card.get("type") or "") != type_:
            continue
        if any(label.casefold() == folded for label in _card_labels(card)):
            return True
    return False


def _card_match(cards: list[dict], phrase: str, type_: str | None) -> bool:
    if _exact_label(cards, phrase, type_):
        return True
    for card in cards:
        if type_ and (card.get("type") or "") != type_:
            continue
        for label in _card_labels(card):
            if len(label) >= 3 and (affirmed_mention(phrase, label) or affirmed_mention(label, phrase)):
                return True
    return False


def private_room_established(blob: str, cards: list[dict]) -> bool:
    for word in _PRIVATE_ROOM:
        if affirmed_mention(blob, word):
            return True
    for card in cards:
        if (card.get("type") or "") != "location":
            continue
        blob_card = " ".join(_card_labels(card)).casefold()
        if any(word in blob_card for word in _PRIVATE_ROOM):
            return True
    return False


def place_established(phrase: str, blob: str, cards: list[dict]) -> bool:
    if len(phrase) < 3:
        return False
    if is_generic_room(phrase):
        return private_room_established(blob, cards) or _exact_label(cards, phrase, "location")
    if affirmed_mention(blob, phrase):
        return True
    return _card_match(cards, phrase, "location")


def item_established(phrase: str, blob: str, cards: list[dict]) -> bool:
    if len(phrase) < 3:
        return False
    if affirmed_mention(blob, phrase):
        return True
    return _card_match(cards, phrase, "item")


def _lore(adventure: dict, cards: list[dict], memories: list[dict] | None) -> str:
    parts = [
        adventure.get("plot_essentials") or "",
        adventure.get("story_summary") or "",
    ]
    for card in cards:
        parts.append(card.get("title") or "")
        parts.append(card.get("entry") or "")
        parts.extend(_card_labels(card))
    for memory in memories or []:
        parts.append(memory.get("text") or "")
    return "\n".join(part for part in parts if part)


def _character_cards(cards: list[dict]) -> list[dict]:
    return [card for card in cards if (card.get("type") or "") == "character"]


def _card_blob(card: dict) -> str:
    sheet = npc_assets.parse_sheet(card.get("sheet") or "")
    return " ".join(
        part for part in (
            card.get("title") or "",
            card.get("entry") or "",
            str(sheet.get("personality") or ""),
            str(sheet.get("notes") or ""),
        ) if part
    )


def _known_character(name: str, cards: list[dict], blob: str) -> dict | None:
    folded = name.casefold()
    for card in _character_cards(cards):
        if any(label.casefold() == folded for label in _card_labels(card)):
            return card
        if any(affirmed_mention(name, label) or affirmed_mention(label, name) for label in _card_labels(card)):
            return card
    if affirmed_mention(blob, name) or _last_index(blob, name) >= 0:
        return {"title": name, "type": "character", "entry": "", "triggers": []}
    return None


def _yields(name: str, card: dict | None, blob: str) -> bool:
    chunks = []
    if card:
        chunks.append(_card_blob(card))
    if blob and name:
        sentences = re.split(r"(?<=[.!?])\s+", blob)
        chunks.extend(sentence for sentence in sentences if _last_index(sentence, name) >= 0)
    return _YIELD_RE.search(" ".join(chunks)) is not None


def _present_names(cards: list[dict], recent: str) -> list[str]:
    found: list[tuple[int, str]] = []
    for card in _character_cards(cards):
        index = max((_last_index(recent, label) for label in _card_labels(card)), default=-1)
        title = (card.get("title") or "").strip()
        if index >= 0 and title:
            found.append((index, title))
    found.sort()
    return [title for _, title in found]


def _recent_text(texts: list[str]) -> str:
    if not texts:
        return ""
    tail = "\n".join(texts[-2:])
    if len(tail) >= 400:
        return tail[-1500:]
    return "\n".join(texts)[-1500:]


def _where_in(text: str, cards: list[dict]) -> tuple[int, str] | None:
    found: list[tuple[int, str]] = []
    best_at = -1
    best_title = ""
    for card in cards:
        if (card.get("type") or "") != "location":
            continue
        for label in _card_labels(card):
            index = _last_index(text, label)
            if index > best_at:
                best_at = index
                best_title = (card.get("title") or label).strip()
    if best_title:
        found.append((best_at, best_title))
    for match in _WHERE_RE.finditer(text or ""):
        phrase = clean_phrase(match.group(1))
        if len(phrase) >= 3:
            found.append((match.start(), phrase))
    if not found:
        return None
    return max(found)


def _current_where(accepted: list[str], cards: list[dict], plot_text: str) -> str:
    for text in reversed(accepted):
        found = _where_in(text, cards)
        if found:
            return found[1]
    found = _where_in(plot_text, cards)
    if found:
        return found[1]
    return "unestablished"


def _fake_comply(text: str) -> bool:
    for match in _FAKE_OBEY_RE.finditer(text or ""):
        window = text[max(0, match.start() - 24):match.start()]
        if _NEG_RE.search(window):
            continue
        return True
    return False


_REFUSAL_BEAT = re.compile(
    r"(?i)\b(recus\w*|refus\w*|afast\w*|desvi\w*|revid\w*|contra-atac\w*|"
    r"n[aã]o deixa|n[aã]o cede|n[aã]o obedece|n[aã]o acerta|erra o|"
    r"te empurr\w*|empurr\w* voc[eê]|segura (?:o|seu|sua)|"
    r"d[aá] um passo para tr[aá]s|segura o pr[oó]prio espa[cç]o)\b"
)


def narration_lacks_refusal(text: str) -> bool:
    """O turno não mostra a pessoa parando a tentativa."""
    return _REFUSAL_BEAT.search(text or "") is None


def _place_leaked(phrase: str, text: str, before: str, cards: list[dict]) -> bool:
    if place_established(phrase, before, cards):
        return False
    if affirmed_mention(text, phrase):
        return True
    if is_generic_room(phrase) or is_private_room_phrase(phrase):
        if private_room_established(before, cards):
            return False
        return any(affirmed_mention(text, word) for word in _PRIVATE_ROOM + ("room", "sala"))
    return False


def _commands(text: str) -> bool:
    return _COMMAND_RE.search(text or "") is not None


def _destinations(text: str) -> list[str]:
    found: list[str] = []
    for pattern in (_TRAVEL_RE, _CONJURE_RE):
        for match in pattern.finditer(text or ""):
            phrase = clean_phrase(match.group(1))
            if len(phrase) >= 3 and phrase.casefold() not in _SKIP_NAMES:
                found.append(phrase)
    unique: list[str] = []
    seen: set[str] = set()
    for phrase in found:
        key = phrase.casefold()
        if key not in seen:
            seen.add(key)
            unique.append(phrase)
    return unique


def _items(text: str, cards: list[dict]) -> list[str]:
    found: list[str] = []
    for match in _ITEM_RE.finditer(text or ""):
        phrase = clean_phrase(match.group(1))
        if len(phrase) < 3 or phrase.casefold() in _SKIP_NAMES:
            continue
        if _known_character(phrase, cards, "") and not item_established(phrase, "", cards):
            continue
        found.append(phrase)
    return found


def _people_named(text: str) -> list[str]:
    found: list[str] = []
    for match in _NAME_AFTER_RE.finditer(text or ""):
        name = clean_phrase(match.group(1))
        if len(name) < 3 or name.casefold() in _SKIP_NAMES or is_generic_room(name):
            continue
        found.append(name)
    return found


@dataclass
class Assessment:
    where: str = "unestablished"
    places: list[str] = field(default_factory=list)
    items: list[str] = field(default_factory=list)
    blocked_places: list[str] = field(default_factory=list)
    blocked_items: list[str] = field(default_factory=list)
    resisted: list[str] = field(default_factory=list)
    absent: list[str] = field(default_factory=list)
    invented_places: list[str] = field(default_factory=list)
    invented_people: list[str] = field(default_factory=list)
    conjure_denied: bool = False
    canon_blob: str = ""
    safe_excerpt: str = ""
    image_directive: str = ""
    image_negative: str = ""
    facts_line: str = ""
    player_notice: str = ""
    ban_room: bool = False

    def image_moment(self, raw: str) -> str:
        if not self.image_directive:
            return raw
        parts = [self.image_directive]
        if self.safe_excerpt:
            parts.append(self.safe_excerpt)
        return "\n".join(parts)[-1400:]


def _judge(attempt: str, blob: str, cards: list[dict], recent: str) -> dict:
    blocked_places: list[str] = []
    blocked_items: list[str] = []
    resisted: list[str] = []
    absent: list[str] = []
    conjure_denied = False
    can_conjure = _POWER_RE.search(blob) is not None
    for phrase in _destinations(attempt):
        if _CONJURE_RE.search(attempt) and not can_conjure and not place_established(phrase, blob, cards):
            conjure_denied = True
        if not place_established(phrase, blob, cards):
            blocked_places.append(phrase)
    for phrase in _items(attempt, cards):
        if _known_character(phrase, cards, blob):
            continue
        if not item_established(phrase, blob, cards):
            blocked_items.append(phrase)

    targets: list[str] = []
    seen: set[str] = set()
    for card in _character_cards(cards):
        title = (card.get("title") or "").strip()
        if not title:
            continue
        if any(_last_index(attempt, label) >= 0 for label in _card_labels(card)):
            key = title.casefold()
            if key not in seen:
                seen.add(key)
                targets.append(title)
    for name in _people_named(attempt):
        if name.casefold() in seen or is_private_room_phrase(name):
            continue
        if any(name.casefold() == place.casefold() for place in blocked_places):
            continue
        seen.add(name.casefold())
        targets.append(name)
    pronouns = _PRONOUNS.intersection(_tokens(attempt))
    if pronouns and not targets:
        present = _present_names(cards, recent)
        if present:
            targets.append(present[-1])

    command = _commands(attempt) or bool(targets and _TRAVEL_RE.search(attempt or ""))
    for name in targets:
        card = _known_character(name, cards, blob)
        present = _last_index(recent, name) >= 0 or any(
            _last_index(recent, label) >= 0
            for label in (_card_labels(card) if card else [])
        )
        if card is None or not present:
            absent.append(name)
            continue
        if command and not _yields(name, card, blob):
            resisted.append((card.get("title") or name).strip())
    return {
        "blocked_places": _unique(blocked_places),
        "blocked_items": _unique(blocked_items),
        "resisted": _unique(resisted),
        "absent": _unique(absent),
        "conjure_denied": conjure_denied,
    }


def _unique(names: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for name in names:
        key = name.casefold()
        if name and key not in seen:
            seen.add(key)
            out.append(name)
    return out


def _accepted(actions: list[dict], cards: list[dict], lore: str) -> tuple[list[str], list[str], list[str]]:
    """Narração que entra no cânone. Continuações que cumprem um pedido bloqueado ficam de fora."""
    accepted: list[str] = []
    invented_places: list[str] = []
    invented_people: list[str] = []
    ordered = [action for action in actions if (action.get("text") or "").strip()]
    for index, action in enumerate(ordered):
        if action.get("type") not in CANON_TYPES:
            continue
        text = action.get("text") or ""
        if action.get("type") == "continue":
            prior = _previous_attempt(ordered, index)
            if prior:
                before = lore + "\n" + "\n".join(accepted)
                recent = _recent_text(accepted)
                judged = _judge(prior, before, cards, recent + "\n" + before)
                leaked = [
                    phrase for phrase in judged["blocked_places"]
                    if _place_leaked(phrase, text, before, cards)
                ]
                people = list(judged["resisted"]) if _fake_comply(text) else []
                if leaked or people:
                    invented_places.extend(leaked)
                    invented_people.extend(people)
                    continue
        accepted.append(text)
    return accepted, _unique(invented_places), _unique(invented_people)


def _previous_attempt(actions: list[dict], index: int) -> str:
    """Tentativas coladas antes desta narração. Um continue no meio corta."""
    chunks: list[str] = []
    for action in reversed(actions[:index]):
        if action.get("type") in ATTEMPT_TYPES:
            chunks.append(action.get("text") or "")
            continue
        if action.get("type") in CANON_TYPES:
            break
    chunks.reverse()
    return "\n".join(chunk for chunk in chunks if chunk)


def _pending(actions: list[dict]) -> str:
    for action in reversed(actions):
        if not (action.get("text") or "").strip():
            continue
        if action.get("type") in ATTEMPT_TYPES:
            return action.get("text") or ""
        if action.get("type") in CANON_TYPES:
            return ""
    return ""


def _display_places(cards: list[dict], where: str) -> list[str]:
    places: list[str] = []
    for card in cards:
        if (card.get("type") or "") != "location":
            continue
        title = (card.get("title") or "").strip()
        if title:
            places.append(title)
    if where != "unestablished" and all(where.casefold() != place.casefold() for place in places):
        places.append(where)
    return places[:8]


def _display_items(cards: list[dict]) -> list[str]:
    items: list[str] = []
    for card in cards:
        if (card.get("type") or "") != "item":
            continue
        title = (card.get("title") or "").strip()
        if title:
            items.append(title)
    return items[:8]


def _ban_room(phrases: list[str]) -> bool:
    return any(is_generic_room(phrase) or is_private_room_phrase(phrase) for phrase in phrases)


def _directive(where: str, places: list[str], items: list[str], absent: list[str],
               people: list[str], conjure_denied: bool) -> str:
    if not (places or items or absent or people or conjure_denied):
        return ""
    bits: list[str] = []
    if where != "unestablished":
        bits.append(f"Stay in {where}.")
    else:
        bits.append("Stay where the story already is. Do not invent a destination.")
    if places or conjure_denied:
        bits.append("The destination in the attempt does not exist. Paint only the current place.")
    if conjure_denied:
        bits.append("The character has no established power to conjure it.")
    if items:
        bits.append("The item in the attempt is not there. Do not add it.")
    if absent:
        bits.append("Someone named in the attempt is not in the scene. Do not add them.")
    if people:
        bits.append(
            REFUSAL_IMAGE + " Paint the refusal, not obedience, "
            "not following, not undressing."
        )
    return " ".join(bits)


def _negative(ban_room: bool, places: list[str], body: bool) -> str:
    bits: list[str] = []
    if ban_room:
        bits.append(_ROOM_NEGATIVE)
    for phrase in places:
        if not is_generic_room(phrase) and not is_private_room_phrase(phrase):
            bits.append(phrase)
    if body:
        bits.append("nude, naked, sex, undressing, kiss, kissing")
    return ", ".join(bits)


def _facts(where: str, places: list[str], items: list[str], notes: list[str]) -> str:
    place_text = ", ".join(places) if places else "none"
    item_text = ", ".join(items) if items else "none"
    line = f"[Facts: where={where}; places={place_text}; items={item_text}. {ATTEMPT_RULE}]"
    if notes:
        line += " " + " ".join(notes)
    return line


def _notes(assessment_bits: dict, where: str) -> list[str]:
    notes: list[str] = []
    places = _unique([*assessment_bits["blocked_places"], *assessment_bits["invented_places"]])
    people = _unique([*assessment_bits["resisted"], *assessment_bits["invented_people"]])
    if places:
        stay = where if where != "unestablished" else "the place already established"
        notes.append(
            "Blocked place: "
            + ", ".join(places)
            + ". It was not established by the opening, the narrator, a Story action, a card, or a memory. "
            + f"Do not create it, do not travel there, and ignore any sentence that arrives there. Stay in {stay}."
        )
    if assessment_bits["conjure_denied"]:
        notes.append("Conjuring that place is not an established power.")
    if assessment_bits["blocked_items"]:
        notes.append(
            "Blocked item: "
            + ", ".join(assessment_bits["blocked_items"])
            + ". It is not in the world. Do not produce it."
        )
    if assessment_bits["absent"]:
        notes.append(
            ", ".join(assessment_bits["absent"])
            + " is not in the scene. Do not invent them or bring them in."
        )
    if people:
        notes.append(
            ", ".join(people)
            + " resists. No established obedience, compulsion, or yes. "
            + "A normal person refuses an order to follow, leave, undress, or submit. "
            + "Narrate the refusal. Do not write instant compliance."
        )
    return notes


def _notice(where: str, places: list[str], items: list[str], absent: list[str], people: list[str]) -> str:
    bits: list[str] = []
    if places:
        bits.append("Não existe: " + ", ".join(places) + ".")
    if items:
        bits.append("Sem item: " + ", ".join(items) + ".")
    if absent:
        bits.append("Não está aqui: " + ", ".join(absent) + ".")
    if people:
        bits.append(
            ", ".join(people)
            + " recusa. Sem vínculo, ordem ou consentimento, uma pessoa não obedece."
        )
    if not bits:
        return ""
    if where != "unestablished":
        bits.append(f"Continua em {where}.")
    else:
        bits.append("A história ainda não te deu esse lugar.")
    return " ".join(bits)


def assess(adventure: dict, actions: list[dict], cards: list[dict],
           memories: list[dict] | None = None) -> Assessment:
    lore = _lore(adventure, cards, memories)
    plot_text = "\n".join(
        part for part in (
            adventure.get("plot_essentials") or "",
            adventure.get("story_summary") or "",
            "\n".join((memory.get("text") or "") for memory in (memories or [])),
        ) if part
    )
    accepted, invented_places, invented_people = _accepted(actions, cards, lore)
    blob = "\n".join(part for part in (lore, "\n".join(accepted)) if part)
    recent = _recent_text(accepted)
    where = _current_where(accepted, cards, plot_text)
    pending = _pending(actions)
    judged = {
        "blocked_places": [],
        "blocked_items": [],
        "resisted": [],
        "absent": [],
        "conjure_denied": False,
        "invented_places": invented_places,
        "invented_people": invented_people,
    }
    if pending:
        fresh = _judge(pending, blob, cards, recent if recent else blob)
        judged.update(fresh)
    places = _display_places(cards, where)
    items = _display_items(cards)
    ban_room = _ban_room(_unique([*judged["blocked_places"], *invented_places]))
    body = bool(judged["resisted"] or invented_people)
    directive = _directive(
        where,
        _unique([*judged["blocked_places"], *invented_places]),
        judged["blocked_items"],
        judged["absent"],
        _unique([*judged["resisted"], *invented_people]),
        bool(judged["conjure_denied"]),
    )
    notes = _notes(judged, where)
    return Assessment(
        where=where,
        places=places,
        items=items,
        blocked_places=list(judged["blocked_places"]),
        blocked_items=list(judged["blocked_items"]),
        resisted=list(judged["resisted"]),
        absent=list(judged["absent"]),
        invented_places=invented_places,
        invented_people=invented_people,
        conjure_denied=bool(judged["conjure_denied"]),
        canon_blob="\n".join(accepted)[-4000:],
        safe_excerpt=recent[-500:],
        image_directive=directive,
        image_negative=_negative(
            ban_room,
            _unique([*judged["blocked_places"], *invented_places]),
            body,
        ),
        facts_line=_facts(where, places, items, notes),
        player_notice=_notice(
            where,
            _unique([*judged["blocked_places"], *invented_places]),
            judged["blocked_items"],
            judged["absent"],
            _unique([*judged["resisted"], *invented_people]),
        ),
        ban_room=ban_room,
    )


def facts_for(adventure: dict, actions: list[dict], conn: sqlite3.Connection) -> str:
    cards = db.list_cards(conn, adventure["id"])
    memories = db.list_memories(conn, adventure["id"])
    return assess(adventure, actions, cards, memories).facts_line


def scrub_image_text(text: str, assessment: Assessment) -> str:
    if not text or not assessment.image_directive:
        return text
    words = list(assessment.blocked_places) + list(assessment.invented_places)
    words += list(assessment.blocked_items)
    if assessment.ban_room:
        words.extend(("bedroom", "bed", "quarto", "suite", "hotel"))
    cleaned = text
    for word in words:
        if len(word) < 3:
            continue
        cleaned = re.sub(r"(?i)(?<!\w)" + re.escape(word) + r"(?!\w)", " ", cleaned)
    return re.sub(r"\s{2,}", " ", cleaned).strip()


def name_is_absent(name: str, absent: list[str]) -> bool:
    folded = (name or "").casefold().strip()
    if not folded:
        return False
    for item in absent:
        other = (item or "").casefold().strip()
        if not other:
            continue
        if folded == other:
            return True
        if re.search(r"(?<!\w)" + re.escape(other) + r"(?!\w)", folded):
            return True
        if re.search(r"(?<!\w)" + re.escape(folded) + r"(?!\w)", other):
            return True
    return False


def chronicle_passage(actions: list[dict]) -> tuple[str, str]:
    canon = [
        action.get("text") or ""
        for action in actions
        if action.get("type") in CANON_TYPES and (action.get("text") or "").strip()
    ]
    attempt = ""
    for action in reversed(actions):
        if action.get("type") in ATTEMPT_TYPES and (action.get("text") or "").strip():
            attempt = action.get("text") or ""
            break
    return "\n".join(canon)[-3000:], attempt[-800:]


def filter_chronicle_payload(payload: dict, assessment: Assessment) -> dict:
    if not isinstance(payload, dict):
        return payload
    banned = _unique([
        *assessment.blocked_places,
        *assessment.invented_places,
        *assessment.blocked_items,
    ])
    if assessment.ban_room:
        banned.extend(["quarto", "bedroom", "bedchamber", "suite"])
    out = dict(payload)
    for key in ("places", "items"):
        rows = out.get(key)
        if not isinstance(rows, list):
            continue
        kept = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            labels = [str(row.get("name") or ""), *(row.get("aliases") or [])]
            if any(_label_banned(label, banned) for label in labels):
                continue
            kept.append(row)
        out[key] = kept
    people = {name.casefold() for name in (*assessment.resisted, *assessment.invented_people, *assessment.absent)}
    characters = out.get("characters")
    if isinstance(characters, list) and people:
        cleaned = []
        for row in characters:
            if not isinstance(row, dict):
                continue
            name = str(row.get("name") or "")
            if name.casefold() in {item.casefold() for item in assessment.absent} and not row.get("existing_title"):
                continue
            if name.casefold() in people:
                facts = row.get("facts") or []
                if isinstance(facts, list):
                    row = dict(row)
                    row["facts"] = [
                        fact for fact in facts
                        if not re.search(
                            r"(?i)(follow|obey|acompanh|obedece|despir|undress|goes with|vai com)",
                            str(fact),
                        )
                    ]
            cleaned.append(row)
        out["characters"] = cleaned
    return out


def _label_banned(label: str, banned: list[str]) -> bool:
    text = (label or "").strip()
    if len(text) < 3:
        return False
    for phrase in banned:
        if text.casefold() == phrase.casefold():
            return True
        if affirmed_mention(text, phrase) or affirmed_mention(phrase, text):
            return True
    return False
