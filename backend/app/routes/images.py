"""Router de imagens (v2): /see, /images/{id} e /settings/image.

Fluxo do /see (contrato docs/openapi.yaml):
1) o momento é os dois últimos trechos (não a história inteira); o LLM
   descreve o gesto em inglês, com quem faz o quê;
2) o primeiro bloco do CLIP é essa frase (uma só), o lugar e o rosto.
   A prosa crua não entra. Estilo fica depois do BREAK. O retrato não
   vira img2img;
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

from app import db, image_client, jobs, llm_client, npc_assets, scene, settings_store
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
If a player instruction for the picture is given, obey it.
No dialogue, no quotes, no style words. Output ONLY that sentence."""

_EXPLICIT_BUILDER_SYSTEM = """You describe one adult moment of an interactive story for an uncensored text-to-image model.
Write a single English sentence whose subject is the nudity or the sex act happening right now: who is naked, who does what to whom, in the place.
Use plain words. Do not add clothes. Do not fade to black. Do not censor adult nudity or sex.
Do not describe hair, face, eyes or age.
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
    "eyes", "eye", "hair", "gown", "dress", "robe", "armor", "armour",
    "skin", "beard", "scar", "blonde", "brunette", "redhead", "helm",
    "cloak", "cape", "silk", "shirt",
    "cabelo", "olhos", "olho", "vestido", "armadura", "elmo", "barba",
    "cicatriz", "pele", "tunica", "túnica", "manto", "capa",
)


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


def contradicting_looks(text: str, canon: str) -> tuple[str, str]:
    """Tira o visual que o card não confirma. Devolve (texto limpo, negative).

    Sem card, a prosa fica — é ela que descreve a pessoa. Com card, "vestido
    branco" não entra no prompt e ainda vai para o negative.
    """
    if not text:
        return "", ""
    if not canon:
        return " ".join(text.split()), ""
    canon_l = canon.casefold()
    words = "|".join(re.escape(word) for word in _LOOK_WORDS)
    pattern = re.compile(
        rf"(?:\b[\w']+\b\s+){{0,2}}\b(?:{words})\b(?:\s+\b[\w']+\b){{0,2}}",
        re.IGNORECASE,
    )
    banned: list[str] = []

    def scrub(match: re.Match) -> str:
        phrase = " ".join(match.group(0).split())
        # "and" / "the" no meio da frase não são visual. Sem isso
        # "long red hair and plate" sai inteiro, mesmo estando no card.
        glue = {"and", "the", "with", "her", "his", "its", "from", "into", "over"}
        tokens = [
            token for token in re.findall(r"[^\W\d_]{3,}", phrase.casefold())
            if token not in glue
        ]
        if tokens and all(token in canon_l for token in tokens):
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
    cfg = settings_store.effective_image_settings(conn)
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
    lines = []
    for card in frame:
        bits = npc_assets.identity_bits(card) if explicit else npc_assets.visual_bits(card)
        title = (card.get("title") or "").strip()
        if not bits:
            if title:
                lines.append(title)
            continue
        lines.append(f"{title}: " + "; ".join(bits) if title else "; ".join(bits))
    if not lines:
        return ""
    return "exact likeness, " + ", ".join(lines)


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

    O CLIP pinta o começo de cada bloco. O primeiro bloco abre com a frase
    em inglês do modelo; o beat cru só entra se essa frase vier vazia. O
    rosto vem depois; o estilo fica depois do BREAK. caption é a frase
    limpa gravada na action see. O retrato não entra como img2img.
    """
    pb = img_cfg["prompt_builder"]
    moment = current_moment(actions, body.action_seq)
    explicit = scene.is_explicit(moment)
    frame = _cards_in_frame(cards, moment, adv.get("plot_essentials") or "")
    clause = _look_clause(frame, explicit=explicit) or (
        "" if explicit else npc_assets.image_canon_clause(cards, moment)
    )
    names = [card.get("title") for card in frame if card.get("title")]
    if not names:
        names = [card.get("title") for card in scene._cast(cards, moment) if card.get("title")]
    player_name = _protagonist_name(adv.get("plot_essentials") or "") or "the player character"
    you = re.search(r"(?<!\w)you(?!\w)", moment or "", re.IGNORECASE)
    if you and not any(name.casefold() == player_name.casefold() for name in names):
        names.insert(0, player_name)
    canon = ""
    if clause and explicit:
        canon = (
            "Identity only, face and hair, not their usual clothes: "
            + clause
            + ". Do not restore outfits. Depict the adult nudity and the act."
        )
    elif clause:
        canon = (
            "Character canon, mandatory whenever they are visible, overrides the prose: "
            + clause
            + ". Same hair, face, age, build and clothing. Do not invent a different person."
        )
    location, entry = scene._location(cards, moment)
    atmosphere = scene.atmosphere_of(moment)
    place = ""
    if location and location != "the current scene":
        place = f"Place: {location}. {clip_words(entry or '', 200)}\n"
    who = ", ".join(names) if names else "only the people named in the moment"
    order = body.extra_prompt.strip()
    user = (
        (canon + "\n\n" if canon else "")
        + f"People who may appear, and no one else: {who}.\n"
        + place
        + ("Player instruction for the picture (obey it): " + order + "\n" if order else "")
        + "Moment to paint:\n"
        + (moment_for_painter(actions, body.action_seq) or "(an empty scene)")
    )
    messages = [
        {"role": "system", "content": _EXPLICIT_BUILDER_SYSTEM if explicit else _PROMPT_BUILDER_SYSTEM},
        {"role": "user", "content": user},
    ]
    summarized = llm_client.chat_completion(
        llm_cfg, messages, role="image_prompt", temperature=0.2,
        max_tokens=int(pb.get("max_tokens", 180)), transport=transport).strip().strip('"')
    summarized, neg_summary = contradicting_looks(summarized, clause)
    _, neg_moment = contradicting_looks(moment.replace("\n", " "), clause)
    # Uma frase em inglês na frente. A prosa crua não entra: ela vinha
    # cortada no meio da palavra e o narrador copiava o prompt.
    sentence = one_image_sentence(summarized)
    where: list[str] = []
    if location and location != "the current scene":
        where.append(location)
    if atmosphere != "neutral":
        where.append(atmosphere.replace("+", " "))
    lead_bits = []
    if sentence:
        lead_bits.append(sentence)
    if body.extra_prompt.strip():
        lead_bits.append(body.extra_prompt.strip())
    lead_bits.append("the action just described, same continuous scene, coherent bodies")
    if explicit:
        lead_bits.append("clothes off, nude, do not restore outfits, uncensored")
    if where:
        lead_bits.append(", ".join(where))
    if names:
        lead_bits.append("people present: " + ", ".join(names))
    if clause:
        lead_bits.append(clip_words(clause, 260))
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
    negative = _dedupe_negative(neg_summary, neg_moment)
    if _is_interaction(moment, names):
        negative = _dedupe_negative(negative, _PORTRAIT_NEGATIVE)
    caption = sentence or summarized
    return prompt, negative, caption


def _plan_see(adventure_id: str, body: SeeRequest, transport: Any) -> dict:
    """Prompt do See com o estado da aventura agora. No 202 roda na thread do
    job, fora da fila do SD."""
    with db.session() as conn:
        adv = db.get_adventure(conn, adventure_id)
        if not adv:
            raise GameError.not_found(f"adventure {adventure_id} not found")
        actions = db.list_actions(conn, adventure_id)
        cards = [npc_assets.ensure_portrait_prompt(conn, card)
                 for card in db.list_cards(conn, adventure_id)]
        img_cfg = settings_store.effective_image_settings(conn)
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
    }


@router.post("/adventures/{adventure_id}/see", response_model=None)
def post_see(adventure_id: str, body: SeeRequest,
             prefer: str = Header(default=""),
             respond_async: bool = Query(default=False, alias="async"),
             transport: Any = Depends(get_http_transport)) -> dict | JSONResponse:
    """Sem lock de geração: o jogador age enquanto a imagem sai, e o `see`
    entra no fim da aventura. O 202 (C6) é opt-in porque o frontend v0.9.0
    espera o ImageResult na resposta."""
    with db.session() as conn:
        if not db.get_adventure(conn, adventure_id):
            raise GameError.not_found(f"adventure {adventure_id} not found")
        img_cfg = settings_store.effective_image_settings(conn)
    if not img_cfg["enabled"]:
        raise GameError(503, "image_disabled",
                        "image generation is disabled (settings.image.enabled)")

    if respond_async or "respond-async" in prefer.lower():
        job = jobs.start_see(
            adventure_id,
            {"action_seq": body.action_seq, "extra_prompt": body.extra_prompt},
            plan=lambda: _plan_see(adventure_id, body, transport),
        )
        return JSONResponse(status_code=202, content={"job_id": job["id"]},
                            headers={"Preference-Applied": "respond-async"})

    plan = _plan_see(adventure_id, body, transport)
    image_id = db.new_id()
    try:
        result = image_client.generate_image(
            img_cfg, plan["prompt"], adventure_id=adventure_id, image_id=image_id,
            transport=transport, negative_extra=plan["negative_extra"])
    except image_client.ImageError as exc:
        raise GameError(503, exc.code, exc.message) from exc
    image = jobs.store_see(adventure_id, plan["caption"], result, image_id=image_id,
                           prompt=plan["prompt"], negative_prompt=plan["negative_prompt"])
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
