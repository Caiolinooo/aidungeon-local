"""Cena do chat: lugar atual, clima e elenco a partir do texto e dos cards.

Não chama o LLM. O lugar vem do card type=location mencionado por último;
o clima (noite, chuva, fogo…) vem de palavras no texto recente. A combinação
é a assinatura do fundo: muda o lugar ou o clima → pinta outro backdrop.
Os NPCs presentes são os cards type=character mencionados; quem não tem
retrato entra na fila de portraits que já existe.

A pintura em si é o job `scene_backdrop` (app/jobs.py) — esta função só
decide e enfileira, para o POST /scene responder na hora.
"""

from __future__ import annotations

import re
import unicodedata

from app import db, jobs, npc_assets, settings_store, world_facts

# Clima que muda o fundo mesmo sem trocar de lugar. Ordem estável na assinatura.
_ATMOSPHERE = (
    ("night", ("night", "noite", "midnight", "moonlight", "lua", "escuro")),
    ("day", ("morning", "manhã", "sunrise", "amanhecer", "noon", "meio-dia")),
    ("rain", ("rain", "chuva", "storm", "tempestade", "thunder", "trovão")),
    ("fire", ("fire", "fogo", "flame", "chama", "burning", "incêndio", "incendio")),
    ("snow", ("snow", "neve", "blizzard", "nevasca")),
    ("blood", ("blood", "sangue", "gore")),
)

_MOODS = (
    ("happy", ("feliz", "happy", "sorri", "smile", "laugh", "alegre")),
    ("sad", ("triste", "sad", "chora", "cry", "lágrima")),
    ("angry", ("furios", "angry", "raiva", "grita", "rage")),
    ("fear", ("medo", "fear", "assust", "terror", "scared", "afraid")),
    ("love", ("amor", "love", "beij", "kiss", "abraç")),
)

_CAST_LIMIT = 4

# Cena adulta. Não é um bloqueio: é o que faz a roupa do card ceder ao momento.
_EXPLICIT_RE = re.compile(
    r"(?<!\w)(?:"
    r"naked|nude|nudes|topless|bottomless|undress(?:ed|ing)?|"
    r"nua|nuas|nus|pelad[oa]s?|sem roupa|"
    r"sex|sexo|sexual|fuck(?:ing)?|foder|transar|transando|"
    r"orgasm(?:o|s)?|gozar|gozando|ejacul\w*|"
    r"penis|p[eê]nis|vagina|buceta|pau|cock|pussy|clit(?:oris)?|"
    r"nipples?|breasts?|seios|bunda|boquete|"
    r"anal|intercourse|thrust(?:ing)?"
    r")(?!\w)",
    re.IGNORECASE,
)


def is_explicit(text: str) -> bool:
    """O momento descreve nudez ou sexo entre adultos."""
    return _EXPLICIT_RE.search(text or "") is not None


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


def atmosphere_of(text: str) -> str:
    lower = text.lower()
    hits = [name for name, words in _ATMOSPHERE if any(word in lower for word in words)]
    return "+".join(hits) or "neutral"


def _mood_near(text: str, name: str) -> str | None:
    """Mood cuja palavra cai mais perto da última menção do NPC.

    A janela pode cobrir a menção anterior (outra emoção); vence a mais próxima.
    """
    index = _last_mention(text, name)
    if index < 0:
        return None
    start = max(0, index - 200)
    window = text[start:index + len(name) + 200].lower()
    mention_at = index - start
    best: tuple[int, str] | None = None
    for mood, words in _MOODS:
        for word in words:
            search = 0
            while True:
                found = window.find(word, search)
                if found < 0:
                    break
                distance = abs(found - mention_at)
                if best is None or distance < best[0]:
                    best = (distance, mood)
                search = found + len(word)
    return best[1] if best else None


def _location(cards: list[dict], text: str) -> tuple[str, str]:
    """(título, entry) do card de lugar mencionado por último. Sem card, usa a prosa."""
    best: tuple[int, str, str] | None = None
    for card in cards:
        if (card.get("type") or "") != "location":
            continue
        names = [card.get("title") or "", *(card.get("triggers") or [])]
        for name in names:
            index = _last_mention(text, name)
            if index >= 0 and (best is None or index >= best[0]):
                best = (index, card.get("title") or name, card.get("entry") or "")
    if best:
        return best[1], best[2]
    return "the current scene", ""


def _cast(cards: list[dict], text: str) -> list[dict]:
    ranked: list[tuple[int, dict]] = []
    for card in cards:
        if (card.get("type") or "") != "character":
            continue
        names = [card.get("title") or "", *(card.get("triggers") or [])]
        index = max((_last_mention(text, name) for name in names), default=-1)
        if index >= 0:
            ranked.append((index, card))
    ranked.sort(key=lambda item: item[0])
    return [card for _, card in ranked[-_CAST_LIMIT:]]


def _visual_clause(cast: list[dict], *, explicit: bool = False) -> str:
    """Rosto travado de quem está na cena. Vazio se ninguém tem visual ainda.

    Cena nua não carrega a roupa do card.
    """
    if not cast:
        return ""
    if explicit:
        lines = []
        for card in cast:
            bits = npc_assets.identity_bits(card)
            title = (card.get("title") or "").strip()
            if bits:
                lines.append(f"{title}: " + "; ".join(bits) if title else "; ".join(bits))
            elif title:
                lines.append(title)
        if not lines:
            return ""
        return "exact likeness, " + ", ".join(lines)
    text = " ".join((card.get("title") or "") for card in cast)
    return npc_assets.image_canon_clause(cast, text)


def _prompt(title: str, entry: str, style: str, atmosphere: str,
            cast: list[dict], moment: str = "") -> tuple[str, bool]:
    """Lugar, e as pessoas só quando o card já tem o rosto.

    A prosa não entra numa cena vestida: ela descreve a princesa de vestido
    e o modelo pintava outra mulher. Nudez e sexo entram no prompt, e a roupa
    do card não volta. Sem visual travado, o fundo continua vazio de gente.
    """
    explicit = is_explicit(moment)
    clause = _visual_clause(cast, explicit=explicit)
    sex_bits = []
    for card in cast:
        person = (card.get("title") or "").strip()
        phrase = npc_assets.sex_phrase(
            person,
            npc_assets.sex_label(person, moment, card=card),
            explicit=explicit,
        )
        if phrase:
            sex_bits.append(phrase)
    sex_bits.sort(key=lambda bit: 0 if "adult woman" in bit else 1)
    sex_line = ". ".join(sex_bits)
    if clause and explicit:
        parts = [
            sex_line,
            clause,
            "clothes off, nude, do not restore outfits, uncensored",
            (moment or "")[-400:],
            style,
            f"wide cinematic shot of {title}, only the characters named above, "
            "each with that exact face and hair",
            (entry or "")[:400],
            f"atmosphere: {atmosphere}",
            "detailed scene painting, no text, no watermark, no logo",
        ]
        text = " ".join(part.strip() for part in parts if part and part.strip())
        return text[:1400], True
    if clause:
        parts = [
            sex_line,
            clause,
            style,
            f"wide cinematic shot of {title}, only the characters named above, "
            "each with that exact face, hair, age and clothing",
            (entry or "")[:400],
            f"atmosphere: {atmosphere}",
            "detailed scene painting, no text, no watermark, no logo",
        ]
        text = " ".join(part.strip() for part in parts if part and part.strip())
        return text[:1400], True
    parts = [
        style,
        f"wide cinematic establishing shot of {title}, environment only, no people, no characters, no faces",
        (entry or "")[:400],
        f"atmosphere: {atmosphere}",
        "detailed background painting, empty of people, no text, no watermark, no logo",
    ]
    text = " ".join(part.strip() for part in parts if part and part.strip())
    return text[:1400], False


def _backdrop_state(conn, adventure_id: str, ref: str, *,
                    enabled: bool, retry: bool, prompt: str,
                    include_people: bool = False, explicit: bool = False) -> dict:
    """Decide ready/pending/error/disabled e enfileira no máximo um job."""
    current = db.latest_image(conn, adventure_id, "backdrop", ref)
    fallback = current or db.latest_image(conn, adventure_id, "backdrop")
    image = db.image_ref(fallback) if fallback else None

    if not enabled:
        return {"status": "disabled" if not current else "ready",
                "image": image, "error": None}

    if current and not retry:
        return {"status": "ready", "image": db.image_ref(current), "error": None}

    active = db.find_active_job(conn, "scene_backdrop", ref)
    if active:
        return {"status": "pending", "image": image, "error": None}

    previous = db.latest_job(conn, "scene_backdrop", ref)
    if previous and previous["status"] == "error" and not retry:
        return {"status": "error", "image": image, "error": previous.get("error")}

    db.insert_job(conn, adventure_id=adventure_id, type="scene_backdrop",
                  ref_id=ref, payload={
                      "prompt": prompt,
                      "include_people": include_people,
                      "explicit": explicit,
                  })
    return {"status": "pending", "image": image, "error": None}


def sync_scene(conn, adventure_id: str, *, retry: bool = False) -> dict:
    """Estado da cena para a UI. Enfileira fundo e retratos que faltam.

    O fundo usa só o cânone. Pedido do jogador não pinta um quarto que a
    história não deu, nem veste a cena com a obediência que o NPC não deu.
    """
    adventure = db.get_adventure(conn, adventure_id) or {}
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
    explicit = is_explicit(text)
    clause = _visual_clause(cast_cards, explicit=explicit)
    signature = f"{_slug(title)}|{atmosphere}"
    if clause:
        # O fundo antigo (pessoa errada) não serve quando o rosto do card entra.
        signature = f"{signature}|{_slug(clause)[:48]}"
    if explicit:
        signature = f"{signature}|explicit"
    ref = f"{adventure_id}:{signature}"
    img_cfg = settings_store.effective_image_settings(conn)
    prompt, include_people = _prompt(
        title, entry, img_cfg.get("style_suffix") or "", atmosphere, cast_cards,
        text,
    )
    backdrop = _backdrop_state(
        conn, adventure_id, ref,
        enabled=bool(img_cfg.get("enabled")), retry=retry, prompt=prompt,
        include_people=include_people,
        explicit=explicit,
    )

    cast = []
    for card in cast_cards:
        if not card.get("portrait") and img_cfg.get("enabled"):
            jobs.enqueue_portrait(conn, adventure_id, card["id"])
        cast.append({
            "id": card["id"],
            "title": card.get("title") or "",
            "portrait": card.get("portrait"),
            "mood": _mood_near(text, card.get("title") or ""),
        })

    return {
        "location": title,
        "atmosphere": atmosphere,
        "signature": signature,
        "backdrop": backdrop,
        "cast": cast,
        "where": assessment.where,
        "notice": assessment.player_notice,
    }
