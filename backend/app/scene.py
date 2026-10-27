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

from app import db, jobs, likeness, npc_assets, npc_bonds, settings_store, world_facts
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
    names = [card.get("title") or "", *(card.get("triggers") or [])]
    title = " ".join(str(card.get("title") or "").split())
    if len(title.split()) > 1:
        names.append(title.split()[0])
    return list(dict.fromkeys(name for name in names if name))



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


def _cast_mood(text: str, name: str, sheet: dict) -> str | None:
    """Emoção já commitada. Sem ela, a palavra de mood mais perto do nome."""
    emotion = npc_assets.visual_reaction(sheet).get("emotion") or ""
    if emotion:
        return emotion
    return _mood_near(text, name)


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
    return [card for _, card in ranked[-_CAST_LIMIT:]]


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
    reaction = _reaction_clause(cast) if clause else ""
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
    sex_line = ". ".join(sex_bits)
    creature = any(npc_assets.creature_kind(card) for card in cast)
    style_text = style
    if creature and "natural skin texture" in style_text:
        style_text = style_text.replace(
            "natural skin texture",
            "scales, hide or fur, not human skin",
        )
    lock = npc_assets.coverage_lock(*cast, explicit=explicit)
    if clause:
        # images.py importa scene no topo; o scrub fica aqui por isso.
        from app.routes.images import contradicting_looks
        beat, _banned = contradicting_looks(moment, clause)
        beat = " ".join(beat.split())
    else:
        beat = ""
    if clause and explicit:
        parts = [
            sex_line,
            clause,
            reaction,
            "clothes off, nude, do not restore outfits, uncensored",
            beat[-400:],
            style_text,
            f"wide cinematic shot of {title}, only the characters named above, "
            "each with that exact face and hair",
            (entry or "")[:400],
            f"atmosphere: {atmosphere}",
            "detailed scene painting, no text, no watermark, no logo",
        ]
        text = " ".join(part.strip() for part in parts if part and part.strip())
        return text[:1400], True
    if clause:
        beat = beat[-280:]
        parts = [
            sex_line,
            clause,
            reaction,
            lock,
            style_text,
            f"wide cinematic shot of {title}, only the characters named above, "
            "each with that exact face, hair, age and clothing",
            beat,
            (entry or "")[:400],
            f"atmosphere: {atmosphere}",
            "what is happening now, same continuous scene, coherent bodies, "
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


def cast_reference_plan(conn, adventure_id: str, cast: list[dict]) -> dict:
    """Âncoras do elenco, em ordem. Sem PNG de alguém, `portrait_required`.

    Lista vazia é cena sem gente: não há referência para exigir.
    """
    card_ids, image_ids = likeness.cast_reference_ids(cast)
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
            jobs.enqueue_portrait(conn, adventure_id, card_id, replace=True)


def queue_missing_portraits(adventure_id: str, cast: list[dict]) -> None:
    """Igual a `_queue_missing_portraits`, numa transação que commita."""
    with db.session() as conn:
        _queue_missing_portraits(conn, adventure_id, cast)


def _backdrop_state(conn, adventure_id: str, ref: str, *,
                    enabled: bool, retry: bool, prompt: str,
                    include_people: bool = False, explicit: bool = False,
                    cache_key: str = "",
                    cast_card_ids: list[str] | None = None,
                    reference_image_ids: list[str] | None = None,
                    reference_hashes: list[str] | None = None) -> dict:
    """Decide ready/pending/error/disabled e enfileira no máximo um job."""
    current = db.latest_image(conn, adventure_id, "backdrop", ref)
    if not current and cache_key:
        current = db.find_cached_image(
            conn, adventure_id, kind="backdrop", ref_id=ref,
            cache_key=cache_key,
        )
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
        "cast_card_ids": list(cast_card_ids or []),
        "reference_image_ids": list(reference_image_ids or []),
        "reference_hashes": list(reference_hashes or []),
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
    # O ComfyUI aceita várias âncoras regionais. O SDXL anterior usa img2img
    # nos retratos; nas cenas, o elenco entra pelo prompt, sem fingir controle
    # multirreferência que o provider não possui.
    from app import consequences
    visual_effects = consequences.visual_context(adventure)
    if visual_effects:
        prompt = f"{prompt}, {visual_effects}"[:1400]
    plan = {
        "cast_card_ids": [],
        "reference_image_ids": [],
        "reference_hashes": [],
        "blobs": [],
    }
    waiting = None
    if (img_cfg.get("provider") == "comfyui" and img_cfg.get("enabled")
            and include_people and paint_cast):
        try:
            plan = cast_reference_plan(conn, adventure_id, paint_cast)
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
        cache_key = jobs.backdrop_cache_key(
            img_cfg, prompt, include_people=include_people, explicit=explicit,
            reference_images=plan["blobs"] or None)
        ref = f"{adventure_id}:{cache_key[:40]}"
        backdrop = _backdrop_state(
            conn, adventure_id, ref,
            enabled=bool(img_cfg.get("enabled")), retry=retry, prompt=prompt,
            include_people=include_people,
            explicit=explicit, cache_key=cache_key,
            cast_card_ids=plan["cast_card_ids"],
            reference_image_ids=plan["reference_image_ids"],
            reference_hashes=plan["reference_hashes"],
        )
    signature = cache_key[:40]

    cast = []
    for card in cast_cards:
        if not card.get("portrait") and img_cfg.get("enabled"):
            jobs.enqueue_portrait(conn, adventure_id, card["id"])
        sheet = npc_assets.parse_sheet(card.get("sheet") or "")
        member = {
            "id": card["id"],
            "title": card.get("title") or "",
            "portrait": card.get("portrait"),
            "mood": _cast_mood(text, card.get("title") or "", sheet),
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
