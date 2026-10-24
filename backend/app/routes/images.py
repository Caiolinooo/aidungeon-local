"""Router de imagens (v2): /see, /images/{id} e /settings/image.

Fluxo do /see (contrato docs/openapi.yaml):
1) o momento é os dois últimos trechos (não a história inteira); o LLM
   descreve o gesto em inglês, com quem faz o quê;
2) o primeiro bloco do CLIP abre com o rosto do card (nome, cabelo, pele).
   A frase em inglês da ação vem depois. A prosa crua não entra. Estilo
   fica depois do BREAK. O retrato não vira img2img;
3) grava a linha em `images` + action type=see (mesmo seq vinculando);
4) devolve ImageResult.

Não usa o lock de geração: o jogador segue agindo enquanto a imagem sai, e o
`see` entra no fim da aventura (`jobs.store_see`). Com `Prefer: respond-async`
(ou `?async=1`) a rota devolve 202 {job_id} e o resto vira o job `see_image`.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import FileResponse, JSONResponse

from app import access, auth, db, image_client, jobs, likeness, llm_client, npc_assets, scene, settings_store
from app import world_facts
from app.deps import get_http_transport
from app.errors import GameError
from app.schemas import ImageSettingsUpdate, SeeRequest

log = logging.getLogger("aidungeon.images")

router = APIRouter()

_PROMPT_BUILDER_SYSTEM = """You write one caption for a text-to-image model of the moment happening right now.
One English sentence, present tense, under 40 words. Its main verb is the player order when one is given, otherwise what just happened.
Keep the place. Do not paint an earlier scene and do not invent a new one.
Name only the people listed. Include the player character when the text says "you".
Do not describe hair, face, eyes, skin, age, or clothing.
Do not turn the moment into a portrait or a close-up of one face.
Do not replace a named person with a different face, hair, or skin tone.
Do not add a person who is not listed.
If a player instruction for the picture is given, obey it.
No dialogue, no quotes, no style words. Output ONLY that sentence."""

_EXPLICIT_BUILDER_SYSTEM = """You describe one adult moment of an interactive story for an uncensored text-to-image model.
Write a single English sentence whose subject is the nudity or the sex act happening right now: who is naked, who does what to whom, in the place.
Use plain words. Do not add clothes. Do not fade to black. Do not censor adult nudity or sex.
Do not describe hair, face, eyes or age.
Do not replace a named person with a different face, hair, or skin tone.
Do not add people who are not in the moment. Do not change the place.
Do not sexualize a child. Output ONLY that sentence."""

# Cena de dois (beijo, luta, abraço) não pode colapsar num busto.
_PORTRAIT_NEGATIVE = "solo portrait, bust, headshot, close-up"
_INTERACTION_WORDS = (
    "kiss", "beijo", "beij", "embrace", "abrac", "abraç", "hug",
    "slap", "punch", "dance", "marry", "wedding", "stab", "fight",
)

# Palavras de rosto e roupa. Se não estão no card, saem do prompt do See:
# a prosa descreve a princesa de vestido e o modelo pintava outra mulher.
_LOOK_WORDS = (
    "eyes", "eye", "hair", "haired", "gown", "dress", "robe", "armor", "armour",
    "skin", "beard", "scar", "blonde", "brunette", "redhead", "helm",
    "cloak", "cape", "silk", "shirt",
    "cabelo", "cabelos", "olhos", "olho", "vestido", "armadura", "elmo", "barba",
    "cicatriz", "pele", "tunica", "túnica", "manto", "capa",
)

# O que decide se a frase contradiz o card. Verbo e lugar não entram:
# "curly-haired woman kisses" perde "curly", não o beijo.
_APPEARANCE_TOKENS = {
    "pale", "fair", "tan", "olive", "ebony", "ivory",
    "brown", "black", "white", "dark", "blonde", "brunette", "redhead",
    "auburn", "ginger", "golden", "silver", "gray", "grey", "red", "blue", "green",
    "straight", "curly", "wavy", "long", "short", "bald", "braided",
    "skin", "hair", "haired", "eye", "eyes", "beard", "scar", "freckle", "freckles",
    "gown", "dress", "robe", "armor", "armour", "plate", "helm", "cloak", "cape", "silk",
    "shirt", "tunic", "cabelo", "cabelos", "olhos", "olho", "vestido", "armadura",
    "pele", "barba", "cicatriz", "preto", "pretos", "preta", "loiro", "loira",
    "ruivo", "ruiva", "liso", "lisos", "cacheado", "cacheados",
}
_BODY_NOUNS = {"skin", "hair", "haired", "eye", "eyes", "beard", "scar"}
_GLUE = {"and", "the", "with", "her", "his", "its", "from", "into", "over"}


def moment_for_painter(actions: list[dict], action_seq: int) -> str:
    """Os dois últimos trechos, com o pedido do jogador marcado à parte.

    O modelo de imagem tem de pintar isto, não um trecho anterior.
    """
    eligible = [
        action for action in actions
        if action.get("type") != "see"
        and action.get("seq", 0) <= action_seq
        and (action.get("text") or "").strip()
    ]
    lines = []
    for action in eligible[-2:]:
        text = action["text"].strip()
        if action.get("type") in ("do", "say", "story"):
            lines.append("Player order (the picture must show this action): " + text)
        else:
            lines.append("What just happened (paint this, not an earlier scene): " + text)
    return "\n".join(lines)


def current_moment(actions: list[dict], action_seq: int) -> str:
    """Os dois últimos trechos até este seq. O See pinta isto, não o livro inteiro."""
    eligible = [
        action for action in actions
        if action.get("type") != "see"
        and action.get("seq", 0) <= action_seq
        and (action.get("text") or "").strip()
    ]
    if not eligible:
        return ""
    return "\n".join(action["text"].strip() for action in eligible[-2:])[-1400:]


def _token_in_canon(token: str, canon_l: str) -> bool:
    if re.search(rf"(?<!\w){re.escape(token)}(?!\w)", canon_l):
        return True
    if token.endswith("s") and re.search(rf"(?<!\w){re.escape(token[:-1])}(?!\w)", canon_l):
        return True
    return False


def _phrase_agrees(phrase: str, canon_l: str) -> bool:
    """A frase descreve o mesmo rosto do card.

    Só cor, cabelo, pele e roupa contam. "glowing" e o verbo ficam, e um
    adjetivo que o card confirma (pale) não vai para o negative só porque
    a palavra ao lado (glowing) não está na ficha.
    """
    tokens = [
        token for token in re.findall(r"[^\W\d_]{3,}", phrase.casefold())
        if token not in _GLUE
    ]
    appearance = [token for token in tokens if token in _APPEARANCE_TOKENS]
    if not appearance:
        return True
    return all(
        token in _BODY_NOUNS or _token_in_canon(token, canon_l)
        for token in appearance
    )


def contradicting_looks(text: str, canon: str) -> tuple[str, str]:
    """Tira o visual que o card não confirma. Devolve (texto limpo, negative).

    Sem card, a prosa fica — é ela que descreve a pessoa. Com card, "vestido
    branco" não entra no prompt e ainda vai para o negative. "pele pálida"
    do próprio card não vai para o negative.
    """
    if not text:
        return "", ""
    if not canon:
        return " ".join(text.split()), ""
    canon_l = canon.casefold()
    words = "|".join(re.escape(word) for word in _LOOK_WORDS)
    pattern = re.compile(
        rf"(?:\b[\w']+\b[\s-]+){{0,2}}(?:[\w']+-)?(?:{words})\b(?:[\s-]+\b[\w']+\b){{0,1}}",
        re.IGNORECASE,
    )
    banned: list[str] = []

    def scrub(match: re.Match) -> str:
        phrase = " ".join(match.group(0).split())
        if _phrase_agrees(phrase, canon_l):
            return match.group(0)
        banned.append(phrase)
        return " "

    cleaned = pattern.sub(scrub, text)
    cleaned = re.sub(r"\s+([,.;])", r"\1", cleaned)
    cleaned = re.sub(r"(?:\s*[,.;]\s*){2,}", ", ", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ,.;")
    return cleaned, ", ".join(dict.fromkeys(banned))


def clip_words(text: str, limit: int) -> str:
    """Corta no fim da frase ou na palavra. Nunca no meio de uma palavra."""
    text = " ".join((text or "").split())
    if limit <= 0 or len(text) <= limit:
        return text
    cut = text[:limit]
    sentence = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if sentence == -1 and cut.endswith((".", "!", "?")):
        sentence = len(cut) - 1
    if sentence >= limit // 2:
        return cut[: sentence + 1].rstrip()
    space = cut.rfind(" ")
    if space >= limit // 2:
        return cut[:space].rstrip(" ,.;")
    return cut.rstrip(" ,.;")


_PT_MARK = re.compile(r"[áàâãéêíóôõúçÁÀÂÃÉÊÍÓÔÕÚÇ]")


def one_image_sentence(text: str) -> str:
    """A primeira frase em inglês do resumo. O resto (entry, segunda frase) sai."""
    flat = " ".join((text or "").replace("\n", " ").split()).strip().strip('"')
    flat = re.sub(r"^>\s*You\s+(?:say\s+)?", "", flat, flags=re.IGNORECASE)
    if not flat:
        return ""
    parts = [part.strip() for part in re.findall(r"[^.!?]+[.!?]?", flat) if part.strip()]
    if not parts:
        return ""
    english = [part for part in parts if not _PT_MARK.search(part)]
    chosen = english[0] if english else parts[0]
    return clip_words(chosen, 280)


def _dedupe_negative(*parts: str) -> str:
    seen: list[str] = []
    folded = set()
    for part in parts:
        for bit in (part or "").split(","):
            bit = " ".join(bit.split())
            key = bit.casefold()
            if bit and key not in folded:
                folded.add(key)
                seen.append(bit)
    return ", ".join(seen)


# ------------------------------------------------------------ settings/image
def _image_settings_response(conn) -> dict:
    """Shape ImageSettings do openapi (api_key SEMPRE mascarada)."""
    cfg = image_client.effective_settings(conn)
    return {
        "enabled": cfg["enabled"],
        "provider": cfg["provider"],
        "sdwebui_base_url": cfg["sdwebui_base_url"],
        "sdwebui_steps": cfg["sdwebui_steps"],
        "sdwebui_width": cfg["sdwebui_width"],
        "sdwebui_height": cfg["sdwebui_height"],
        "negative_prompt": cfg["negative_prompt"],
        "openai_base_url": cfg["openai_base_url"],
        "openai_api_key": settings_store.mask_api_key(cfg["openai_api_key"]),
        "openai_model": cfg["openai_model"],
        "openai_size": cfg["openai_size"],
        "style_suffix": cfg["style_suffix"],
        "seed": cfg["seed"],
        "variation": cfg["variation"],
        "cfg_scale": cfg["cfg_scale"],
        "sampler_name": cfg["sampler_name"],
        "scheduler": cfg["scheduler"],
        "denoising_strength": cfg["denoising_strength"],
        "timeout_seconds": cfg["timeout_seconds"],
        "sd_model_checkpoint": cfg["sd_model_checkpoint"],
    }


@router.get("/settings/image")
def get_image_settings() -> dict:
    with db.session() as conn:
        return _image_settings_response(conn)


@router.put("/settings/image")
def put_image_settings(body: ImageSettingsUpdate) -> dict:
    update = body.model_dump(exclude_unset=True)
    with db.session() as conn:
        settings_store.apply_image_update(conn, update)
        return _image_settings_response(conn)


# ------------------------------------------------------------------ health
@router.get("/health/image")
def get_image_health(transport: Any = Depends(get_http_transport)) -> dict:
    """Sonda options/models, nunca txt2img/img2img; falhas do provider são dados."""
    try:
        with db.session() as conn:
            cfg = image_client.effective_settings(conn)
    except Exception as exc:
        log.warning("image health: configuração indisponível (%s)", type(exc).__name__)
        return {"enabled": None, "provider": None, "status": "error",
                "detail": "image settings could not be loaded"}
    return image_client.health(cfg, transport)


# -------------------------------------------------------------- cancelamento
@router.post("/jobs/{job_id}/cancel")
def cancel_image_job(job_id: str,
                     info: auth.AuthInfo = Depends(auth.require_auth),
                     transport: Any = Depends(get_http_transport)) -> dict:
    """Cancela jobs do dono; running vira canceled antes do interrupt cooperativo."""
    running = False
    with db.session() as conn:
        job = db.get_job(conn, job_id)
        adventure = db.get_adventure(conn, job["adventure_id"]) if job else None
        if not job or not adventure or not access.can_see(info.user, adventure.get("owner")):
            raise GameError.not_found(f"job {job_id} not found")
        if job["status"] in ("preparing", "pending", "running"):
            running = job["status"] == "running"
            canceled = jobs.cancel(conn, job_id)
            if canceled is None:
                current = db.get_job(conn, job_id)
                if current and current["status"] == "canceled":
                    return current
                raise GameError(409, "job_not_cancelable", "job already finished")
            job = canceled
        elif job["status"] in ("done", "error"):
            raise GameError(409, "job_not_cancelable",
                            f"job already finished with status '{job['status']}'")
    if running:
        jobs.interrupt_image(job, transport)
    return job


# -------------------------------------------------------------------- /see
def _protagonist_name(plot: str) -> str:
    match = re.search(r"Name:\s*([^\]\n;]+)", plot or "", re.IGNORECASE)
    return " ".join(match.group(1).split()) if match else ""


def _player_card(cards: list[dict], name: str) -> dict | None:
    """Card do protagonista. O texto diz 'you'; o título muitas vezes não aparece."""
    named = name.casefold()
    if named:
        for card in cards:
            if (card.get("type") or "") != "character":
                continue
            labels = [card.get("title") or "", *(card.get("triggers") or [])]
            if any(label.strip().casefold() == named for label in labels if label and label.strip()):
                return card
    for card in cards:
        if (card.get("type") or "") == "character" and card.get("use_for_character_creation"):
            return card
    return None


def _cards_in_frame(cards: list[dict], text: str, plot: str) -> list[dict]:
    """Quem a pintura tem de mostrar: citados pelo nome, e o jogador quando é 'you'."""
    frame = list(scene._cast(cards, text))
    name = _protagonist_name(plot)
    you = re.search(r"(?<!\w)you(?!\w)", text or "", re.IGNORECASE)
    if you or (name and npc_assets._mentioned(text, name)):
        player = _player_card(cards, name)
        if player and all(player.get("id") != card.get("id") for card in frame):
            frame.insert(0, player)
    return frame


def _look_clause(frame: list[dict], *, explicit: bool = False) -> str:
    """Rosto travado no começo do prompt. Sem isso o CLIP pinta um estranho.

    Cena nua ou sexual guarda o rosto e solta a roupa do card.
    """
    return likeness.opening_clause(frame, explicit=explicit)


def _is_interaction(text: str, names: list[str]) -> bool:
    """Gesto entre pessoas, ou mais de um nome no quadro."""
    folded = (text or "").casefold()
    if any(re.search(rf"(?<!\w){re.escape(word)}", folded) for word in _INTERACTION_WORDS):
        return True
    return len([name for name in names if name.strip()]) >= 2


def _build_final_prompt(adv: dict, actions: list[dict], body: SeeRequest,
                        img_cfg: dict, llm_cfg: dict, cards: list[dict],
                        transport: Any) -> tuple[str, str, str]:
    """O momento atual + o rosto do card. Devolve (prompt, negative, caption).

    O CLIP pinta o começo de cada bloco. O primeiro bloco abre com o rosto
    do card (cabelo, pele, porte); a frase em inglês vem em seguida. A
    prosa crua não entra. O estilo fica depois do BREAK. caption é a frase
    limpa gravada na action see. O retrato não entra como img2img.
    """
    pb = img_cfg["prompt_builder"]
    visible = [
        action for action in actions
        if action.get("type") != "see" and int(action.get("seq") or 0) <= int(body.action_seq)
    ]
    with db.session() as conn:
        memories = db.list_memories(conn, adv["id"])
    assessment = world_facts.assess(adv, visible, cards, memories)
    raw_moment = current_moment(actions, body.action_seq)
    # Cânone vence a tentativa: o momento pintado é o que o mundo estabeleceu.
    moment = assessment.image_moment(raw_moment)
    order = body.extra_prompt.strip()
    # Detecção de frame/recusa usa o texto CRU da cena (a diretiva não nomina
    # ninguém); o texto pintado continua sendo o canônico `moment`.
    guard_text = "\n".join(part for part in (raw_moment, order) if part)
    frame = _cards_in_frame(cards, guard_text, adv.get("plot_essentials") or "")
    if assessment.absent:
        frame = [
            card for card in frame
            if not world_facts.name_is_absent(card.get("title") or "", assessment.absent)
        ]
    # A recusa por menor usa o texto cru da cena — mesmo quando
    # o world-facts rebaixa o explícito para a recusa canônica.
    # A segurança olha o pedido do jogador tambem.
    decision = likeness.decide_frame(frame, explicit=scene.is_explicit(guard_text))
    if decision.refusal:
        raise GameError(422, decision.refusal, decision.message)
    frame = decision.cast
    # A pintura segue a narracao: um pedido do jogador nao veste a cena.
    # O See e o fundo do chat usam a mesma regra (scene.narration_moment).
    explicit = decision.allow_explicit and scene.is_explicit(
        scene.narration_moment(actions, body.action_seq)
    )
    if assessment.image_directive:
        # recusa/bloqueio canônico: a cena pintada não é explícita
        explicit = explicit and scene.is_explicit(assessment.safe_excerpt)
    if not explicit:
        order = likeness.strip_explicit(order)
    clause = _look_clause(frame, explicit=explicit) or (
        "" if explicit else npc_assets.image_canon_clause(frame, moment)
    )
    names = [card.get("title") for card in frame if card.get("title")]
    if not names:
        names = [card.get("title") for card in scene._cast(cards, raw_moment) if card.get("title")]
    player_name = _protagonist_name(adv.get("plot_essentials") or "") or "the player character"
    you = re.search(r"(?<!\w)you(?!\w)", raw_moment or "", re.IGNORECASE)
    if you and not any(name.casefold() == player_name.casefold() for name in names):
        names.insert(0, player_name)
    story_blob = "\n".join(
        (action.get("text") or "") for action in visible if (action.get("text") or "").strip()
    )
    by_name = {card.get("title"): card for card in frame if card.get("title")}
    for card in cards:
        title = (card.get("title") or "").strip()
        if title and npc_assets._mentioned(raw_moment, title) and title not in by_name:
            if not (assessment.absent and world_facts.name_is_absent(title, assessment.absent)):
                by_name[title] = card
                names.append(title)
    # Trava de sexo na frente do prompt: mulher fica mulher, homem fica homem.
    sex_bits = []
    seen_sex: set[str] = set()
    for name in names:
        key = name.casefold()
        if key in seen_sex:
            continue
        seen_sex.add(key)
        label = npc_assets.sex_label(name, story_blob, moment, card=by_name.get(name))
        phrase = npc_assets.sex_phrase(name, label, explicit=explicit)
        if phrase:
            sex_bits.append(phrase)
    sex_bits.sort(key=lambda bit: 0 if "adult woman" in bit else 1)
    who_bits = []
    for name in names:
        label = npc_assets.sex_label(name, story_blob, moment, card=by_name.get(name))
        who_bits.append(f"{name} ({label})" if label else name)
    canon = ""
    if clause and explicit:
        canon = (
            "Identity only, face and hair, not their usual clothes: "
            + clause
            + ". Do not restore outfits. Depict the adult nudity and the act."
        )
    elif clause:
        canon = (
            "Character reference, keep them recognizable: "
            + clause
            + ". Keep the same face, hair, age and build. The player's requested "
            "moment, clothing and action take priority over older descriptions."
        )
    location, entry = scene._location(cards, assessment.safe_excerpt or moment)
    if assessment.image_directive and assessment.where != "unestablished":
        # Lugar canônico vence: pinta o lugar real, não o destino da tentativa.
        location = assessment.where
        for card in cards:
            if (card.get("type") or "") == "location" and \
                    (card.get("title") or "").casefold() == location.casefold():
                entry = card.get("entry") or entry
                break
    atmosphere = scene.atmosphere_of(assessment.safe_excerpt or moment)
    place = ""
    if location and location != "the current scene":
        place = f"Place: {location}. {clip_words(entry or '', 200)}\n"
    who = ", ".join(who_bits) if who_bits else "only the people named in the moment"
    # Com diretiva (recusa/bloqueio), o builder recebe a versão canônica —
    # a tentativa crua do jogador não chega nem ao LLM de prompt.
    painted_source = moment if assessment.image_directive else \
        moment_for_painter(actions, body.action_seq)
    painted = painted_source or "(an empty scene)"
    user = (
        ("Player instruction for the picture (highest priority; obey it): " + order + "\n" if order else "")
        + (canon + "\n\n" if canon else "")
        + f"People who may appear, and no one else: {who}.\n"
        + place
        + "Moment to paint:\n"
        + painted
    )
    messages = [
        {"role": "system", "content": _EXPLICIT_BUILDER_SYSTEM if explicit else _PROMPT_BUILDER_SYSTEM},
        {"role": "user", "content": user},
    ]
    summarized = llm_client.chat_completion(
        llm_cfg, messages, role="image_prompt", temperature=0.2,
        max_tokens=int(pb.get("max_tokens", 180)), transport=transport).strip().strip('"')
    if not explicit:
        summarized = likeness.strip_explicit(summarized)
    summarized, neg_summary = contradicting_looks(summarized, clause)
    summarized = world_facts.scrub_image_text(summarized, assessment)
    scrubbed_moment = moment
    _, neg_moment = contradicting_looks(scrubbed_moment.replace("\n", " "), clause)
    # Uma frase em inglês na frente. A prosa crua não entra: ela vinha
    # cortada no meio da palavra e o narrador copiava o prompt.
    sentence = one_image_sentence(summarized)
    # Recusa estabelecida: a frase visual fixa lidera (e vira caption) — o
    # modelo resumidor poderia pintar o ato recusado.
    if world_facts.REFUSAL_IMAGE in assessment.image_directive:
        sentence = world_facts.REFUSAL_IMAGE
    where: list[str] = []
    if location and location != "the current scene":
        where.append(location)
    if atmosphere != "neutral":
        where.append(atmosphere.replace("+", " "))
    lead_bits = []
    # Trava de sexo antes de tudo. Recusa estabelecida: a frase visual da
    # recusa vem antes do rosto (senão o CLIP pinta o ato recusado). Caso
    # normal: o rosto lidera o primeiro chunk — "kisses on the sofa" na
    # frente pintava um casal genérico.
    if sex_bits:
        lead_bits.append(". ".join(sex_bits))
    refusal = world_facts.REFUSAL_IMAGE in assessment.image_directive
    if refusal:
        lead_bits.append(sentence)
    if clause:
        lead_bits.append(clip_words(clause, 420))
    if sentence and not refusal:
        lead_bits.append(sentence)
    if order:
        lead_bits.append(order)
    lead_bits.append("the action just described, same continuous scene, coherent bodies")
    if explicit:
        lead_bits.append("clothes off, nude, do not restore outfits, uncensored")
    if where:
        lead_bits.append(", ".join(where))
    if names:
        lead_bits.append("people present: " + ", ".join(who_bits))
    if _is_interaction(moment, names):
        lead_bits.append("wide shot of that action, full bodies, not a close-up portrait")
    lead = ", ".join(lead_bits)
    tail = ", ".join(part for part in (
        (entry or "")[:160] if location and location != "the current scene" else "",
        adv.get("image_style") or "",
        img_cfg["style_suffix"],
    ) if part)
    if lead and tail:
        prompt = f"{lead} BREAK {tail}"
    else:
        prompt = lead or tail
    negative = _dedupe_negative(neg_summary, neg_moment, assessment.image_negative)
    if clause:
        negative = _dedupe_negative(negative, npc_assets.IDENTITY_NEGATIVE)
    if _is_interaction(moment, names):
        negative = _dedupe_negative(negative, _PORTRAIT_NEGATIVE)
    caption = sentence or summarized
    return prompt, negative, caption


def _refuse_known_frame(adventure_id: str, body: SeeRequest) -> None:
    """422 antes de enfileirar, quando o texto e os cards já decidem a recusa.

    Nudez com menor não espera o LLM. O worker repete o critério:
    se o prompt nasce lá, o job fica `minor`.
    """
    with db.session() as conn:
        adv = db.get_adventure(conn, adventure_id)
        if not adv:
            raise GameError.not_found(f"adventure {adventure_id} not found")
        actions = db.list_actions(conn, adventure_id)
        cards = list(db.list_cards(conn, adventure_id))
    moment = current_moment(actions, body.action_seq)
    order = body.extra_prompt.strip()
    guard_text = "\n".join(part for part in (moment, order) if part)
    frame = _cards_in_frame(cards, guard_text, adv.get("plot_essentials") or "")
    decision = likeness.decide_frame(frame, explicit=scene.is_explicit(guard_text))
    if decision.refusal:
        raise GameError(422, decision.refusal, decision.message)


def _plan_see(adventure_id: str, body: SeeRequest, transport: Any,
              variation: str | int | None = None) -> dict:
    """Prompt do See com o estado da aventura agora. No 202 roda na thread do
    job, fora da fila do SD."""
    with db.session() as conn:
        adv = db.get_adventure(conn, adventure_id)
        if not adv:
            raise GameError.not_found(f"adventure {adventure_id} not found")
        actions = db.list_actions(conn, adventure_id)
        cards = [npc_assets.ensure_portrait_prompt(conn, card)
                 for card in db.list_cards(conn, adventure_id)]
        img_cfg = image_client.effective_settings(conn)
        llm_cfg = settings_store.effective_llm_settings(conn)
    try:
        prompt, negative_extra, caption = _build_final_prompt(
            adv, actions, body, img_cfg, llm_cfg, cards, transport)
    except llm_client.LlmError as exc:
        raise GameError(503, exc.code, exc.message) from exc
    return {
        "prompt": prompt,
        "negative_extra": negative_extra,
        "negative_prompt": _dedupe_negative(img_cfg["negative_prompt"], negative_extra),
        "caption": caption,
        "cache_key": image_client.request_key(
            img_cfg, prompt, negative_extra=negative_extra, variation=variation),
    }


@router.post("/adventures/{adventure_id}/see", response_model=None)
def post_see(adventure_id: str, body: SeeRequest,
             prefer: str = Header(default=""),
             respond_async: bool = Query(default=False, alias="async"),
             variation: int | None = Query(default=None, ge=0),
             transport: Any = Depends(get_http_transport)) -> dict | JSONResponse:
    """Sem lock de geração: o jogador age enquanto a imagem sai, e o `see`
    entra no fim da aventura. O 202 (C6) é opt-in porque o frontend v0.9.0
    espera o ImageResult na resposta."""
    with db.session() as conn:
        if not db.get_adventure(conn, adventure_id):
            raise GameError.not_found(f"adventure {adventure_id} not found")
        img_cfg = image_client.effective_settings(conn)
    if not img_cfg["enabled"]:
        raise GameError(503, "image_disabled",
                        "image generation is disabled (settings.image.enabled)")
    _refuse_known_frame(adventure_id, body)

    if respond_async or "respond-async" in prefer.lower():
        job = jobs.start_see(
            adventure_id,
            {"action_seq": body.action_seq, "extra_prompt": body.extra_prompt,
             "variation": variation},
            plan=lambda: _plan_see(adventure_id, body, transport, variation),
        )
        return JSONResponse(status_code=202, content={"job_id": job["id"]},
                            headers={"Preference-Applied": "respond-async"})

    plan = _plan_see(adventure_id, body, transport, variation)
    with db.session() as conn:
        cached = db.find_cached_image(
            conn, adventure_id, kind="scene", ref_id="",
            cache_key=plan["cache_key"],
        )
    if cached:
        image_id = db.new_id()
        result = {
            "file_path": cached["file_path"], "provider": cached["provider"],
            "width": cached["width"], "height": cached["height"],
        }
        image = jobs.store_see(
            adventure_id, plan["caption"], result, image_id=image_id,
            prompt=plan["prompt"], negative_prompt=plan["negative_prompt"],
            cache_key=plan["cache_key"], reused_from=cached["id"])
        log.info("see: imagem %s reusada para aventura %s", cached["id"], adventure_id)
        return image
    image_id = db.new_id()
    try:
        result = image_client.generate_image(
            img_cfg, plan["prompt"], adventure_id=adventure_id, image_id=image_id,
            transport=transport, negative_extra=plan["negative_extra"])
    except image_client.ImageError as exc:
        raise GameError(503, exc.code, exc.message) from exc
    image = jobs.store_see(adventure_id, plan["caption"], result, image_id=image_id,
                           prompt=plan["prompt"], negative_prompt=plan["negative_prompt"],
                           cache_key=plan["cache_key"])
    log.info("see: imagem %s gerada para aventura %s (%s)",
             image_id, adventure_id, result["provider"])
    return image


# ------------------------------------------------------------- arquivo PNG
@router.get("/images/{image_id}")
def get_image(image_id: str) -> FileResponse:
    with db.session() as conn:
        image = db.get_image(conn, image_id)
    if not image:
        raise GameError.not_found(f"image {image_id} not found")
    path = db.REPO_ROOT / image["file_path"]
    if not path.is_file():
        raise GameError.not_found(f"image file for {image_id} not found")
    return FileResponse(path, media_type="image/png")
