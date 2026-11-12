"""Cena do chat: lugar atual, clima e elenco a partir do texto e dos cards.

Não chama o LLM. O lugar vem do card type=location mencionado por último;
o clima (noite, chuva, fogo…) vem de palavras no texto recente. A combinação
é a assinatura do fundo: muda o lugar ou o clima → pinta outro backdrop.
Os NPCs presentes são os cards type=character mencionados; quem não tem
retrato entra na fila de portraits que já existe.

A pintura em si é o job `scene_backdrop` (app/jobs.py) — esta função só
decide e enfileira, para o POST /scene responder na hora. Fundo novo cancela
o pendente antigo da mesma aventura (`supersede_key`, contrato C5).

`sheet.visual_state` (ação, emoção, pose) entra no mood do elenco e no
prompt do fundo quando a pessoa já está na cena. Seq e números de bonds
ficam de fora: não há job por token nem por tick numérico. Prioridade
e cancelamento do backdrop não mudam.
"""

from __future__ import annotations

import re
import unicodedata

from app import db, jobs, likeness, npc_assets, npc_bonds, settings_store, voice_emotion, world_facts
from app.errors import GameError

# Clima que muda o fundo mesmo sem trocar de lugar. Ordem estável na assinatura.
_ATMOSPHERE = (
    ("night", ("night", "noite", "midnight", "moonlight", "lua", "escuro")),
    ("day", ("morning", "manhã", "sunrise", "amanhecer", "noon", "meio-dia")),
    ("rain", ("rain", "chuva", "storm", "tempestade", "thunder", "trovão")),
    ("fire", ("fire", "fogo", "flame", "chama", "burning", "incêndio", "incendio")),
    ("snow", ("snow", "neve", "blizzard", "nevasca")),
    ("blood", ("blood", "sangue", "gore")),
)

_CAST_LIMIT = 4

# Cena adulta. Não é um bloqueio: é o que faz a roupa do card ceder ao momento.
_EXPLICIT_RE = re.compile(
    r"(?<!\w)(?:"
    r"naked|nude|nudes|topless|bottomless|undress(?:ed|ing)?|"
    r"nua|nuas|nu|nus|desnud[oa]s?|pelad[oa]s?|sem roupa|"
    r"sex|sexo|sexual|fuck(?:ing)?|foder|transar|transando|"
    r"orgasm(?:o|s)?|gozar|gozando|ejacul\w*|"
    r"penis|p[eê]nis|vagina|buceta|pau|cock|pussy|clit(?:oris)?|"
    r"nipples?|breasts?|seios|bunda|boquete|"
    r"anal|intercourse|thrust(?:ing)?"
    r")(?!\w)",
    re.IGNORECASE,
)


# Uma fala negada ou hipotética não pinta ninguém nu. "não estava nua",
# "se estiver nuda", "seria nua" descrevem ausência de roupa, não nudez.
# O teste é por limite de palavra: "se desliza" (pronome) não é hipótese.
_EXPLICIT_NEGATION = re.compile(
    r"(?<!\w)(?:nao|nunca|nem|recusou|recusa|nega|negou|negando|declinou|"
    r"not|never|without|unclothed|clothed)(?!\w)"
    r"|\bsem\s+(?:(?:a|o|as|os|uma|um|nenhuma|nenhum)\s+)?"
    r"(?:roupa|vestes|vestido|nu|nudez|nua|estar|ser)\b"
    r"|\bcom\s+(?:(?:a|o|as|os|uma|um)\s+)?(?:roupa|vestes|vestido)\b"
    r"|\bvestida\b"
    r"|\bno need to\b|\binstead of\b|\bfully clothed\b"
)
# Palavras que abrem uma hipótese, dentro da mesma frase.
_EXPLICIT_HYPOTHESIS = re.compile(
    r"(?<!\w)(?:caso|seria|seriam|fosse|fossem|poderia|poderiam|talvez|"
    r"perhaps|maybe|might|would|were|unless|if)(?!\w)"
    r"|\bse\s+(?:estiv\w+|estiver\w+|fosse|for|ser\w+|fic\w+|pudess\w+|aparec\w+)\b"
)
_CLAUSE_SPLIT = re.compile(r"[,;:.!?…]")


def _clause_before(text: str, at: int) -> str:
    """A cláusula onde a palavra está. Serve para ler negação e hipótese."""
    head = text[max(0, at - 180):at]
    parts = [part.strip() for part in _CLAUSE_SPLIT.split(head) if part.strip()]
    return parts[-1].casefold() if parts else ""


def is_explicit(text: str) -> bool:
    """O momento descreve nudez ou sexo entre adultos.

    Uma fala do jogador ("não posso com uma rainha nua!") ou uma hipótese
    ("se estiver nuda") não conta: a cena segue vestida.
    """
    for match in _EXPLICIT_RE.finditer(text or ""):
        clause = _clause_before(text, match.start())
        if not clause:
            return True
        if _EXPLICIT_NEGATION.search(clause):
            continue
        if _EXPLICIT_HYPOTHESIS.search(clause):
            continue
        return True
    return False


def narration_moment(actions: list[dict], action_seq: int | None = None) -> str:
    """Os dois últimos trechos de NARRAÇÃO ('start' e 'continue').

    A ação e a fala do jogador não entram: pedir não é descrever. O See e o
    fundo do chat pintam a mesma coisa — o que a história narrou, não o que
    o jogador protestou.
    """
    eligible = [
        action for action in actions
        if (action.get("type") or "") in world_facts.CANON_TYPES
        and (action_seq is None or action.get("seq", 0) <= action_seq)
        and (action.get("text") or "").strip()
    ]
    if not eligible:
        return ""
    return "\n".join(action["text"].strip() for action in eligible[-2:])[-1400:]


def _slug(text: str) -> str:
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text.lower()).strip("-")
    return (slug or "scene")[:60]


def _last_mention(text: str, name: str) -> int:
    """Índice da última ocorrência com fronteira de palavra, ou -1."""
    name = (name or "").strip()
    if len(name) < 3:
        return -1
    pattern = re.compile(r"(?<!\w)" + re.escape(name) + r"(?!\w)", re.IGNORECASE)
    last = -1
    for match in pattern.finditer(text):
        last = match.start()
    return last

def _mention_names(card: dict) -> list[str]:
    """Título, gatilhos e o primeiro nome de um personagem com sobrenome.

    A narração chama "Thalita" depois de apresentar "Thalita Massoto".
    Sem o nome curto, a carta — e a foto de referência — fica fora da cena.
    """
    title = " ".join(str(card.get("title") or "").split())
    names = [
        title,
        *npc_assets.title_aliases(title),
        *(card.get("triggers") or []),
    ]
    return list(dict.fromkeys(name for name in names if name))



def atmosphere_of(text: str) -> str:
    lower = text.lower()
    hits = [name for name, words in _ATMOSPHERE if any(word in lower for word in words)]
    return "+".join(hits) or "neutral"


def _cast_moods(cast: list[dict], text: str) -> dict[str, str | None]:
    """Emoção commitada não neutra, senão última evidência por sujeito."""
    attributed = voice_emotion.emotions_for_subjects(
        text,
        {
            str(card.get("id") or card.get("title")): _mention_names(card)
            for card in cast
        },
    )
    moods: dict[str, str | None] = {}
    for card in cast:
        key = str(card.get("id") or card.get("title"))
        sheet = npc_assets.parse_sheet(card.get("sheet") or "")
        committed = npc_assets.visual_reaction(sheet).get("emotion") or ""
        emotion = committed if committed and committed != "neutral" else attributed.get(key) or committed
        moods[key] = {
            "joy": "happy", "sadness": "sad", "anger": "angry",
            "tenderness": "love",
        }.get(emotion, emotion)
    return moods


def _location(cards: list[dict], text: str) -> tuple[str, str]:
    """(título, entry) do card de lugar mencionado por último. Sem card, usa a prosa.

    Card de clima ("A Nóvoa") perde para um lugar físico, mesmo que a chuva
    apareça depois: o fundo pinta o apartamento, não a névoa.
    """
    physical: tuple[int, str, str] | None = None
    atmospheric: tuple[int, str, str] | None = None
    for card in cards:
        if (card.get("type") or "") != "location":
            continue
        title = " ".join(str(card.get("title") or "").split())
        names = [*npc_assets.title_aliases(title), *(card.get("triggers") or [])]
        index = max((_last_mention(text, name) for name in names), default=-1)
        if index < 0:
            continue
        found = (index, title or names[0], card.get("entry") or "")
        target = atmospheric if world_facts.is_atmospheric_place(card) else physical
        if target is None or index >= target[0]:
            if world_facts.is_atmospheric_place(card):
                atmospheric = found
            else:
                physical = found
    best = physical or atmospheric
    if best:
        return best[1], best[2]
    return "the current scene", ""


def _cast(cards: list[dict], text: str) -> list[dict]:
    from app import death_rules
    ranked: list[tuple[int, dict]] = []
    for card in death_rules.living_cast(cards):
        if (card.get("type") or "") != "character":
            continue
        names = _mention_names(card)
        index = max((_last_mention(text, name) for name in names), default=-1)
        if index >= 0:
            ranked.append((index, card))
    ranked.sort(key=lambda item: item[0])
    cast = [card for _, card in ranked[-_CAST_LIMIT:]]
    # A narração em segunda pessoa ("you"/"você") não repete o nome do
    # protagonista. Sem o card dele, o fundo pinta só o NPC e o jogador
    # desaparece da cena.
    if re.search(r"(?i)(?<!\w)(?:you|você|voce)(?!\w)", text or ""):
        player = next((
            card for card in cards
            if (card.get("type") or "") == "character"
            and card.get("use_for_character_creation")
        ), None)
        if player is not None and all(
                player.get("id") != card.get("id") for card in cast):
            cast = [player, *cast][:_CAST_LIMIT]
    return cast


def _reaction_clause(cast: list[dict]) -> str:
    """Ação, emoção e pose do elenco. Sem seq, sem bonds, sem motivo oculto."""
    bits: list[str] = []
    for card in cast:
        raw_sheet = card.get("sheet") or ""
        if isinstance(raw_sheet, dict):
            sheet = raw_sheet
        else:
            sheet = npc_assets.parse_sheet(raw_sheet if isinstance(raw_sheet, str) else "")
        reaction = npc_assets.visual_reaction(sheet)
        if not reaction:
            continue
        name = " ".join(str(card.get("title") or "").split()) or "character"
        parts = [
            f"{key} {reaction[key]}"
            for key in ("action", "emotion", "pose")
            if reaction.get(key)
        ]
        if parts:
            bits.append(name + ": " + ", ".join(parts))
    if not bits:
        return ""
    return "current reaction, " + "; ".join(bits)


def _visual_clause(cast: list[dict], *, explicit: bool = False) -> str:
    """Rosto travado de quem está na cena. Vazio se ninguém tem visual ainda.

    Cena nua não carrega a roupa do card. O rosto sai em inglês, na frente.
    """
    if not cast:
        return ""
    return likeness.opening_clause(cast, explicit=explicit)


def _reference_layout_guard(cast: list[dict], *, explicit: bool = False,
                            nude: list[str] | None = None) -> str:
    """Referência de identidade não pode virar estúdio, grade ou split na cena."""
    for card in cast:
        raw_sheet = card.get("sheet") or ""
        sheet = raw_sheet if isinstance(raw_sheet, dict) else npc_assets.parse_sheet(raw_sheet)
        reference = sheet.get("reference") if isinstance(sheet.get("reference"), dict) else {}
        if isinstance(reference.get("turnaround"), dict):
            # Só em termos positivos. Citar "grey studio backgrounds, multi-panel
            # layouts, split screens" aqui fazia o modelo pintar exatamente isso:
            # com a guarda inteira no prompt, 3 de 3 fundos saíram como tríptico
            # de estúdio; com este texto, 4 de 4 saíram como cena contínua.
            together = (
                "Everyone is together in the same place, in one single continuous "
                "photograph of that place."
                if len(cast) > 1 else
                "The image is one single continuous photograph of that place."
            )
            if explicit:
                # A roupa da referência some pelo estado positivo do prompt, e a
                # âncora explícita já é só o rosto.
                return (
                    "Use the reference images only for the named characters' identity, face, "
                    "hair and distinctive facial details. The clothing state in this prompt "
                    "is authoritative: follow it even when a reference image shows different "
                    "clothing. " + together
                )
            return (
                "Use the reference images only for the named characters' identity, face, "
                "hair, body, outfit and distinctive details. " + together
            )
    return ""


_PROMPT_LIMIT = 1400


def _sentences(parts: list) -> str:
    """Junta as partes como frases. Sem o ponto, "…masculine face exact likeness
    of X, Y: …" virava uma frase só e o modelo trocava quem é quem."""
    out: list[str] = []
    for part in parts:
        text = " ".join(str(part or "").split()).strip() if part else ""
        if not text:
            continue
        if text[-1] not in ".!?":
            text += "."
        out.append(text)
    return " ".join(out)


def _latest_words(text: str, limit: int) -> str:
    """As últimas frases que cabem no limite: o fundo pinta o fim da narração,
    que é o agora, e não o começo do trecho anterior."""
    text = " ".join((text or "").split())
    if limit <= 0 or len(text) <= limit:
        return text
    kept: list[str] = []
    size = 0
    for sentence in reversed(re.split(r"(?<=[.!?…])\s+", text)):
        extra = len(sentence) + (1 if kept else 0)
        if size + extra > limit:
            break
        kept.append(sentence)
        size += extra
    if kept:
        return " ".join(reversed(kept))
    tail = text[-limit:]
    space = tail.find(" ")
    return tail[space + 1:] if 0 <= space < limit // 2 else tail


_BEAT_FLOOR = 110
_LOOK_FLOOR = 160


def _drop_traits(person: str, limit: int) -> str:
    """Encurta "Nome: traço; traço; traço" tirando traços inteiros do fim.

    Os traços vêm em ordem de importância (óculos, barba, máscara, cabelo,
    corpo). Cortar por caracteres deixava "…wire frames; just past." no fundo
    de produção: meio traço que o modelo não entende.
    """
    from app.routes.images import clip_words
    if len(person) <= limit:
        return person
    head, sep, tail = person.partition(": ")
    traits = [bit.strip() for bit in tail.rstrip(".").split(";") if bit.strip()] if sep else []
    if len(traits) < 2:
        return clip_words(person, limit)
    kept: list[str] = []
    for trait in traits:
        candidate = f"{head}: " + "; ".join([*kept, trait])
        if len(candidate) > limit:
            break
        kept.append(trait)
    if not kept:
        return clip_words(f"{head}: {traits[0]}", limit)
    return f"{head}: " + "; ".join(kept)


def _shrink_look(look: str, limit: int) -> str:
    """Encolhe o bloco de aparência tirando um pouco de cada personagem.

    Cortar pelo fim tirava a descrição inteira do último, e sem ela o modelo
    lê só o nome: "Ladrão de Jibóia" saiu pintado como uma cobra. A frase
    "exact likeness of …" só tem nomes e fica inteira; quem já é curto cede o
    que sobra para os outros.
    """
    look = " ".join((look or "").split())
    if limit <= 0 or len(look) <= limit:
        return look
    parts = [part for part in re.split(r"(?<=[.!?])\s+", look) if part]
    people = [part for part in parts
              if ":" in part or not part.lower().startswith("exact likeness")]
    if len(parts) <= 1 or not people:
        from app.routes.images import clip_words
        return clip_words(look, limit)
    budget = limit - sum(len(part) + 1 for part in parts if part not in people) - len(people)
    share = {id(part): 0 for part in people}
    waiting = sorted(people, key=len)
    while waiting:
        each = max(60, budget // len(waiting))
        person = waiting.pop(0)
        share[id(person)] = min(len(person), each)
        budget -= share[id(person)]
    return _sentences([
        _drop_traits(part, share[id(part)]) if id(part) in share else part for part in parts])


_CROWD_NOUN = (r"(?:crowds?|patrons|customers|onlookers|bystanders|passers-?by|pedestrians|"
               r"shoppers|vendors|merchants|throngs?|people|sailors|drinkers|travellers|travelers)")
_CROWD_FILL = re.compile(
    r"(?i),?\s*\b(?:full of|packed with|crowded with|teeming with|bustling with|thronged with|"
    r"filled with)\s+[^,.;]*")
_CROWD_SUB = re.compile(r"(?i),?\s*\b(?:where|while|as)\b[^,.;]*\b" + _CROWD_NOUN + r"\b[^,.;]*,?")
_CROWD_SENTENCE = re.compile(r"(?i)\b" + _CROWD_NOUN + r"\b")
_CROWD_WORD = re.compile(r"(?i)\b(?:busy|bustling|crowded|packed|teeming|thronged|lively|noisy)\s+")


def uncrowded(entry: str) -> str:
    """O texto do lugar sem a multidão, para a cena que mostra só o elenco.

    "A busy harbour market" enchia o fundo de feirantes mesmo com "no other
    people" no prompt: o Flux pinta o que o texto descreve. Com o elenco no
    quadro saem "full of patrons", a oração "where merchants shout…", a frase
    que só fala de gente e o adjetivo de movimento; o lugar em si fica.
    """
    text = _CROWD_SUB.sub(" ", _CROWD_FILL.sub("", entry or ""))
    sentences = [part.strip() for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()]
    quiet = [part for part in sentences if not _CROWD_SENTENCE.search(part)]
    text = _CROWD_WORD.sub("", " ".join(quiet or sentences[:1]))
    text = re.sub(r"\s+([,.;])", r"\1", " ".join(text.split()))
    text = re.sub(r"\b([Aa])n (?=[^aeiouAEIOU\W])", r"\1 ", text)
    text = re.sub(r"\b([Aa]) (?=[aeiouAEIOU])", r"\1n ", text)
    return text.strip(" ,;")


def _alone_there(count: int) -> str:
    """Só o elenco no lugar, em termos positivos (negação o Flux ignora)."""
    who = "This person is alone there" if count == 1 else \
        f"These {count} are the only people there"
    return (f"{who}: the place is quiet at this hour, and everything behind them is "
            "unattended and empty of people")


def _headcount(cast: list[dict]) -> str:
    """Quantas pessoas, pelo nome. Sem isto o fundo repetia um rosto ou perdia alguém."""
    names = [" ".join(str(card.get("title") or "").split()) for card in cast]
    names = [name for name in names if name]
    if len(names) < 2:
        return ""
    return (f"Exactly {len(names)} people in the frame, each one a different person: "
            + ", ".join(names))


def _fit(required: list, beat: str, beat_at: int, optional: list,
         *, beat_limit: int, look: str = "") -> str:
    """Monta o prompt dentro de `_PROMPT_LIMIT` sem cortar no meio.

    As partes obrigatórias entram inteiras; o momento narrado recebe o que
    sobrar (até `beat_limit`) e as opcionais só entram se couberem. Se as
    obrigatórias não deixam `_BEAT_FLOOR` (uma frase de ação) para o momento,
    quem encolhe é `look`, o bloco de aparência, um pouco de cada personagem.
    """
    from app.routes.images import clip_words
    required = list(required)
    beat = " ".join((beat or "").split())
    floor = min(_BEAT_FLOOR, len(beat))
    room = _PROMPT_LIMIT - len(_sentences(required)) - 2
    if room < floor and look and look in required:
        at = required.index(look)
        target = max(_LOOK_FLOOR, len(look) - (floor - room) - 2)
        required[at] = _shrink_look(look, target)
        room = _PROMPT_LIMIT - len(_sentences(required)) - 2
    beat = clip_words(beat, max(0, min(beat_limit, room))) if room > 0 else ""
    parts = list(required)
    if beat:
        parts.insert(beat_at, beat)
    text = _sentences(parts)
    for extra in optional:
        candidate = _sentences([text, extra])
        if extra and len(candidate) <= _PROMPT_LIMIT:
            text = candidate
    if len(text) > _PROMPT_LIMIT:
        text = clip_words(text, _PROMPT_LIMIT)
    return text


def _prompt(title: str, entry: str, style: str, atmosphere: str,
            cast: list[dict], moment: str = "") -> tuple[str, bool]:
    """Lugar, e as pessoas só quando o card já tem o rosto.

    A prosa não entra numa cena vestida: ela descreve a princesa de vestido
    e o modelo pintava outra mulher. Nudez e sexo entram no prompt, e a roupa
    do card não volta. Sem visual travado, o fundo continua vazio de gente.
    """
    decision = likeness.decide_frame(cast, explicit=is_explicit(moment))
    if decision.refusal:
        cast = []
        moment = ""
        explicit = False
    else:
        cast = decision.cast
        explicit = decision.allow_explicit
    clause = _visual_clause(cast, explicit=explicit)
    if clause:
        # images.py importa scene no topo; helpers ficam aqui por isso.
        from app.routes.images import clip_words, contradicting_looks
        clause = _shrink_look(clause, 400)
    reaction = clip_words(_reaction_clause(cast), 250) if clause else ""
    nude = npc_assets.nude_names(cast, moment) if clause and explicit else []
    layout_guard = _reference_layout_guard(cast, explicit=explicit, nude=nude) if clause else ""
    sex_bits = []
    for card in cast:
        person = (card.get("title") or "").strip()
        phrase = npc_assets.sex_phrase(
            person,
            npc_assets.sex_label(person, moment, card=card),
            explicit=explicit,
            card=card,
        )
        if phrase:
            sex_bits.append(phrase)
    sex_bits.sort(key=lambda bit: 0 if "adult woman" in bit else 1)
    sex_line = clip_words(". ".join(sex_bits), 300) if clause else ". ".join(sex_bits)
    creature = any(npc_assets.creature_kind(card) for card in cast)
    style_text = re.sub(r"(?i)\b(?:nude|naked)\b", "", style)
    style_text = " ".join(style_text.replace(", ,", ",").split()).strip(" ,")
    if creature and "natural skin texture" in style_text:
        style_text = style_text.replace(
            "natural skin texture",
            "scales, hide or fur, not human skin",
        )
    lock = npc_assets.coverage_lock(*cast, explicit=explicit)
    if clause:
        beat, _banned = contradicting_looks(moment, clause)
        # O fim da narração é o agora; o corte antigo guardava o começo do
        # trecho anterior (muitas vezes a descrição que o jogador escreveu).
        beat = _latest_words(beat, 400)
    else:
        beat = ""
    if clause and explicit:
        from app.routes.images import (
            _clothed_predicate, _nude_predicate, _stateful_action, scrub_clothing_states,
        )
        states: list[tuple[str, str]] = []
        for card in cast:
            name = (card.get("title") or "").strip()
            if not name:
                continue
            if name in nude:
                label = "" if npc_assets.creature_kind(card, name) else \
                    npc_assets.sex_label(name, moment, card=card)
                states.append((name, _nude_predicate(name, card, label)))
            else:
                states.append((name, _clothed_predicate(card, name)))
        state_line = "; ".join(
            f"{name} is {predicate}" for name, predicate in states)
        if beat and states:
            beat = _stateful_action(scrub_clothing_states(beat), states) or beat
        # A guarda de layout já diz que todos estão no mesmo lugar; repetir
        # tirava espaço da aparência de cada personagem.
        composition = len(cast) > 1 and not layout_guard and \
            "everyone together in one shared space, one single continuous image"
        full_body = "wide full-body composition, entire body visible, not a close-up portrait"
        single = f"{cast[0].get('title')} is the only person in the entire scene, no one else" if len(cast) == 1 else ""
        headcount = _headcount(cast)
        required = [
            sex_line,
            clause,
            headcount,
            state_line,
            single,
            full_body,
            f"wide cinematic shot of {title}, only the characters named above, "
            "each with that exact face and hair, no other people, no patrons, no bystanders",
            f"atmosphere: {atmosphere}",
            layout_guard,
            composition,
            style_text,
            "detailed scene painting, no text, no watermark, no logo",
        ]
        required = [part for part in required if part and str(part).strip()]
        beat_at = sum(1 for part in (sex_line, clause, headcount, state_line, single, full_body)
                      if part and str(part).strip())
        text = _fit(required, beat, beat_at, [reaction, clip_words(entry or "", 400)],
                    beat_limit=400, look=clause)
        return text, True
    if clause:
        headcount = _headcount(cast)
        # Roupa do momento e marcas visíveis que a história gravou: sem isto o
        # fundo pintava sempre a roupa padrão das referências.
        state_line = npc_assets.story_state_line(cast, limit=90)
        required = [
            sex_line,
            clause,
            headcount,
            state_line,
            f"wide cinematic shot of {title}, only the characters named above, "
            f"each with that exact face, hair and age{'' if state_line else ' and clothing'}. "
            f"{_alone_there(len(cast))}",
            f"atmosphere: {atmosphere}",
            layout_guard,
            lock,
            style_text,
            "what is happening now, same continuous scene, coherent bodies, "
            "detailed scene painting, no text, no watermark, no logo",
        ]
        required = [part for part in required if part and str(part).strip()]
        beat_at = sum(1 for part in (sex_line, clause, headcount, state_line)
                      if part and str(part).strip())
        beat = _latest_words(beat, 280)
        text = _fit(required, beat, beat_at,
                    [reaction, clip_words(uncrowded(entry or ""), 400)],
                    beat_limit=280, look=clause)
        return text, True
    from app.routes.images import clip_words
    parts = [
        style,
        f"wide cinematic establishing shot of {title}, environment only, no people, no characters, no faces",
        clip_words(entry or "", 400),
        f"atmosphere: {atmosphere}",
        "detailed background painting, empty of people, no text, no watermark, no logo",
    ]
    return clip_words(_sentences(parts), _PROMPT_LIMIT), False


def cast_reference_plan(conn, adventure_id: str, cast: list[dict], *,
                        provider: str = "sdwebui", explicit: bool = False) -> dict:
    """Âncoras e, no ComfyUI, vistas do elenco. Sem PNG, `portrait_required`.

    Lista vazia é cena sem gente: não há referência para exigir. Em cena
    explícita usa uma âncora por pessoa, sem folhas de roupa ou composição.
    """
    # Explícito usa só a âncora de cada pessoa. Mestre e ficha são folhas de
    # roupa: mesmo recortados, gastam slots e puxam o figurino de volta.
    card_ids, image_ids = likeness.cast_reference_ids_for_provider(
        cast, provider, explicit=explicit)
    if any(not image_id for image_id in image_ids):
        raise likeness.portrait_required()
    blobs = (
        likeness.load_reference_images(conn, adventure_id, image_ids)
        if image_ids else []
    )
    return {
        "cast_card_ids": card_ids,
        "reference_image_ids": image_ids,
        "reference_hashes": likeness.reference_hashes(blobs),
        "blobs": blobs,
    }


def _queue_missing_portraits(conn, adventure_id: str, cast: list[dict]) -> None:
    """Enfileira só personagens do elenco que não têm PNG legível."""
    if not settings_store.effective_image_settings(conn).get("enabled"):
        return
    characters = [card for card in cast if card.get("type") == "character"]
    for card in characters[:likeness.CAST_REFERENCE_LIMIT]:
        card_id = str(card.get("id") or "")
        if not card_id:
            continue
        image_id = likeness.reference_image_id(card)
        if not image_id:
            jobs.enqueue_portrait(conn, adventure_id, card_id)
            continue
        try:
            likeness.load_reference_images(conn, adventure_id, [image_id])
        except GameError:
            jobs.enqueue_portrait(conn, adventure_id, card_id, replace=True,
                                  automatic=True)


def queue_missing_portraits(adventure_id: str, cast: list[dict]) -> None:
    """Igual a `_queue_missing_portraits`, numa transação que commita."""
    with db.session() as conn:
        _queue_missing_portraits(conn, adventure_id, cast)


def _finished_backdrop(conn, adventure_id: str, ref: str) -> dict | None:
    """Imagem do último job concluído para este ref.

    O executor traduz o prompt para inglês e grava a imagem sob a chave do texto
    traduzido, não sob o `ref` do texto de origem que a cena pede. Sem este elo
    a cena nunca achava o fundo pronto e enfileirava outro job a cada leitura.
    """
    previous = db.latest_job(conn, "scene_backdrop", ref)
    if not previous or previous.get("status") != "done":
        return None
    result = previous.get("result")
    image_ref = result.get("image") if isinstance(result, dict) else None
    image_id = image_ref.get("id") if isinstance(image_ref, dict) else None
    if not image_id:
        return None
    image = db.get_image(conn, str(image_id))
    if not image or image.get("adventure_id") != adventure_id \
            or image.get("kind") != "backdrop":
        return None
    return image


def _backdrop_state(conn, adventure_id: str, ref: str, *,
                    enabled: bool, retry: bool, prompt: str,
                    include_people: bool = False, explicit: bool = False,
                    reference_faces: bool | None = None,
                    cache_key: str = "", expected_people: int = 0,
                    cast_card_ids: list[str] | None = None,
                    reference_image_ids: list[str] | None = None,
                    reference_hashes: list[str] | None = None,
                    reference_provider: str = "") -> dict:
    """Decide ready/pending/error/disabled e enfileira no máximo um job."""
    current = db.latest_image(conn, adventure_id, "backdrop", ref)
    if not current and cache_key:
        current = db.find_cached_image(
            conn, adventure_id, kind="backdrop", ref_id=ref,
            cache_key=cache_key,
        )
    if not current:
        current = _finished_backdrop(conn, adventure_id, ref)
    fallback = current or db.latest_image(conn, adventure_id, "backdrop")
    image = db.image_ref(fallback) if fallback else None

    if not enabled:
        return {"status": "disabled" if not current else "ready",
                "image": image, "error": None}

    if current and not retry:
        return {"status": "ready", "image": db.image_ref(current), "error": None}

    active = db.find_active_job(conn, "scene_backdrop", ref)
    if active:
        active_key = str((active.get("payload") or {}).get("cache_key") or "")
        if cache_key and active_key != cache_key \
                and active.get("status") in ("preparing", "pending"):
            db.cancel_job(conn, active["id"])
        else:
            return {"status": "pending", "image": image, "error": None}

    previous = db.latest_job(conn, "scene_backdrop", ref)
    if previous and previous["status"] == "error" and not retry:
        return {"status": "error", "image": image, "error": previous.get("error")}

    jobs.enqueue(conn, "scene_backdrop", adventure_id, ref, {
        "prompt": prompt,
        "include_people": include_people,
        "cache_key": cache_key,
        "explicit": explicit,
        "reference_faces": explicit if reference_faces is None else bool(reference_faces),
        "expected_people": expected_people,
        "cast_card_ids": list(cast_card_ids or []),
        "reference_image_ids": list(reference_image_ids or []),
        "reference_hashes": list(reference_hashes or []),
        "reference_provider": reference_provider,
    }, priority=jobs.PRIORITY_BACKDROP, supersede_key=f"backdrop:{adventure_id}")
    return {"status": "pending", "image": image, "error": None}


def sync_scene(conn, adventure_id: str, *, retry: bool = False,
               viewer: str | None = None) -> dict:
    """Estado da cena para a UI. Enfileira fundo e retratos que faltam.

    O fundo usa o momento narrado agora, nao a fala do jogador. Pedido do
    jogador não pinta um quarto que a história não deu, nem veste a cena com
    a obediência que o NPC não deu.
    """
    adventure = db.get_adventure(conn, adventure_id) or {}
    if adventure:
        jobs.ensure_player_card(conn, adventure)
    actions = [
        dict(row) for row in conn.execute(
            "SELECT seq, type, text FROM actions WHERE adventure_id=? ORDER BY seq ASC",
            (adventure_id,),
        )
    ]
    cards = [db.attach_portrait(conn, card) for card in db.list_cards(conn, adventure_id)]
    memories = db.list_memories(conn, adventure_id)
    assessment = world_facts.assess(adventure, actions, cards, memories)
    text = assessment.canon_blob
    if not text.strip():
        return {
            "location": None,
            "atmosphere": "neutral",
            "signature": "",
            "backdrop": {"status": "none", "image": None, "error": None},
            "cast": [],
            "where": assessment.where,
            "notice": assessment.player_notice,
        }

    title, entry = _location(cards, text)
    atmosphere = atmosphere_of(text)
    cast_cards = []
    for card in _cast(cards, text):
        cast_cards.append(npc_assets.ensure_portrait_prompt(conn, card))
    # A segurança olha tudo: o pedido do jogador tambem conta.
    decision = likeness.decide_frame(cast_cards, explicit=is_explicit(text))
    # O fundo pinta o momento narrado agora, nao o protesto do jogador.
    moment = narration_moment(actions)
    paint_cast = [] if decision.refusal else decision.cast
    paint_text = "" if decision.refusal else moment
    explicit = False if decision.refusal else (
        decision.allow_explicit and is_explicit(moment)
    )
    img_cfg = settings_store.effective_image_settings(conn)
    prompt, include_people = _prompt(
        title, entry, img_cfg.get("style_suffix") or "", atmosphere, paint_cast,
        paint_text,
    )
    # Recorte só do rosto apenas com alguém nu; vestidos, mestre e ficha
    # mantêm corpo, cabelo, óculos e roupa (mesma regra do See).
    face_refs = bool(explicit and npc_assets.nude_names(paint_cast, paint_text))
    # ComfyUI usa âncoras regionais; SDWebUI faz passes InstantID mascarados.
    from app import consequences
    plan = {
        "cast_card_ids": [card["id"] for card in paint_cast] if include_people else [],
        "reference_image_ids": [],
        "reference_hashes": [],
        "blobs": [],
    }
    waiting = None
    if (img_cfg.get("provider") in ("comfyui", "sdwebui") and img_cfg.get("enabled")
            and include_people and paint_cast):
        try:
            plan = cast_reference_plan(
                conn, adventure_id, paint_cast,
                provider=str(img_cfg.get("provider") or "sdwebui"), explicit=face_refs,
            )
        except GameError as exc:
            if exc.code != "portrait_required":
                raise
            _queue_missing_portraits(conn, adventure_id, paint_cast)
            current = db.latest_image(conn, adventure_id, "backdrop")
            waiting = {
                "status": "pending",
                "image": db.image_ref(current) if current else None,
                "error": exc.message,
            }
    if waiting is not None:
        cache_key = jobs.backdrop_cache_key(
            img_cfg, prompt, include_people=include_people, explicit=explicit)
        backdrop = waiting
    else:
        reference_blobs = (
            likeness.identity_face_pngs(plan["blobs"]) if face_refs else plan["blobs"]
        )
        cache_key = jobs.backdrop_cache_key(
            img_cfg, prompt, include_people=include_people, explicit=explicit,
            reference_images=reference_blobs or None)
        ref = f"{adventure_id}:{cache_key[:40]}"
        backdrop = _backdrop_state(
            conn, adventure_id, ref,
            enabled=bool(img_cfg.get("enabled")), retry=retry, prompt=prompt,
            include_people=include_people,
            explicit=explicit, reference_faces=face_refs, cache_key=cache_key,
            expected_people=len(paint_cast) if include_people else 0,
            cast_card_ids=plan["cast_card_ids"],
            reference_image_ids=plan["reference_image_ids"],
            reference_hashes=plan["reference_hashes"],
            reference_provider=str(img_cfg.get("provider") or ""),
        )
    signature = cache_key[:40]


    cast = []
    moods = _cast_moods(cast_cards, text)
    for card in cast_cards:
        if not card.get("portrait") and img_cfg.get("enabled"):
            jobs.enqueue_portrait(conn, adventure_id, card["id"])
        sheet = npc_assets.parse_sheet(card.get("sheet") or "")
        member = {
            "id": card["id"],
            "title": card.get("title") or "",
            "portrait": card.get("portrait"),
            "mood": moods.get(str(card.get("id") or card.get("title"))),
            "stance": npc_bonds.stance_of(
                npc_bonds.read_bonds(sheet),
                obedience=world_facts.obedience_of(card),
            ),
        }
        # Sem viewer (job/LLM) os fatos continuam no payload. Share não recebe.
        if viewer is None or db.is_adventure_owner(adventure, viewer):
            member["private_facts"] = consequences.private_facts_for(
                adventure, card.get("title") or "",
            )
        cast.append(member)

    return {
        "location": title,
        "atmosphere": atmosphere,
        "signature": signature,
        "backdrop": backdrop,
        "cast": cast,
        "where": assessment.where,
        "notice": assessment.player_notice,
    }
