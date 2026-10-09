"""Router de imagens (v2): /see, /images/{id} e /settings/image.

Fluxo do /see (contrato docs/openapi.yaml):
1) o momento é os dois últimos trechos (não a história inteira); o LLM
   descreve só lugar e ação, em inglês;
2) o primeiro bloco do CLIP é o rosto travado (card, e o jogador quando
   o texto diz "you") mais a ação. Estilo fica depois do BREAK;
3) se essa pessoa tem retrato, img2img parte da foto (não do tamanho dela);
4) grava a linha em `images` + action type=see (mesmo seq vinculando);
5) devolve ImageResult.

Mesmo lock por aventura do game_engine → 409 generation_in_progress.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from app import db, game_engine, image_client, llm_client, npc_assets, scene, settings_store
from app.deps import get_http_transport
from app.errors import GameError
from app.schemas import ImageSettingsUpdate, SeeRequest

log = logging.getLogger("aidungeon.images")

router = APIRouter()

_PROMPT_BUILDER_SYSTEM = """You describe one moment of an interactive story for a text-to-image model.
Write a single English sentence: the place, who is there (use the names given), and what they are doing right now.
Do not describe hair, face, eyes, skin, age, or clothing.
Do not add people who are not in the moment. Do not change the place.
No dialogue, no quotes, no meta text. Output ONLY that sentence."""

# Retrato como img2img. 0.35 reimprime o busto e a cena some; acima de ~0.75
# o rosto some. No meio, a cara ainda puxa o sample e o lugar pode mudar.
SEE_DENOISE = 0.62

# Palavras de rosto e roupa. Se não estão no card, saem do prompt do See:
# a prosa descreve a princesa de vestido e o modelo pintava outra mulher.
_LOOK_WORDS = (
    "eyes", "eye", "hair", "gown", "dress", "robe", "armor", "armour",
    "skin", "beard", "scar", "blonde", "brunette", "redhead", "helm",
    "naked", "nude", "cloak", "cape", "silk", "shirt",
    "cabelo", "olhos", "olho", "vestido", "armadura", "elmo", "barba",
    "cicatriz", "pele", "tunica", "túnica", "manto", "capa",
)


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


def _look_clause(frame: list[dict]) -> str:
    """Rosto travado no começo do prompt. Sem isso o CLIP pinta um estranho."""
    lines = []
    for card in frame:
        bits = npc_assets.visual_bits(card)
        title = (card.get("title") or "").strip()
        if not bits:
            if title:
                lines.append(title)
            continue
        lines.append(f"{title}: " + "; ".join(bits) if title else "; ".join(bits))
    if not lines:
        return ""
    return "exact likeness, " + ", ".join(lines)


def _focus_portrait_id(frame: list[dict]) -> str:
    """O citado por último que já tem foto. É essa cara que o img2img segura."""
    for card in reversed(frame):
        image_id = str(card.get("portrait_image_id") or "")
        if image_id:
            return image_id
    return ""


def _portrait_png(image: dict | None) -> bytes | None:
    raw = str((image or {}).get("file_path") or "")
    if not raw:
        return None
    path = Path(raw)
    if not path.is_absolute():
        path = db.REPO_ROOT / path
    if not path.is_file():
        return None
    return path.read_bytes()


def _build_final_prompt(adv: dict, actions: list[dict], body: SeeRequest,
                        img_cfg: dict, llm_cfg: dict, cards: list[dict],
                        transport: Any) -> tuple[str, str, str]:
    """O momento atual + o rosto do card. Devolve (prompt, negative, portrait id).

    O CLIP pinta o começo de cada bloco e corta o fim. O primeiro bloco é
    a cara e o que está acontecendo agora. O estilo fica depois do BREAK.
    """
    pb = img_cfg["prompt_builder"]
    moment = current_moment(actions, body.action_seq)
    frame = _cards_in_frame(cards, moment, adv.get("plot_essentials") or "")
    clause = _look_clause(frame) or npc_assets.image_canon_clause(cards, moment)
    names = [card.get("title") for card in frame if card.get("title")]
    if not names:
        names = [card.get("title") for card in scene._cast(cards, moment) if card.get("title")]
    canon = ""
    if clause:
        canon = (
            "Character canon, mandatory whenever they are visible, overrides the prose: "
            + clause
            + ". Same hair, face, age, build and clothing. Do not invent a different person."
        )
    location, entry = scene._location(cards, moment)
    atmosphere = scene.atmosphere_of(moment)
    place = ""
    if location and location != "the current scene":
        place = f"Place: {location}. {(entry or '')[:200]}\n"
    who = ", ".join(names) if names else "only the people named in the moment"
    user = (
        (canon + "\n\n" if canon else "")
        + f"People who may appear, and no one else: {who}.\n"
        + place
        + "Moment to paint:\n"
        + (moment or "(an empty scene)")
    )
    messages = [
        {"role": "system", "content": _PROMPT_BUILDER_SYSTEM},
        {"role": "user", "content": user},
    ]
    summarized = llm_client.chat_completion(
        llm_cfg, messages, temperature=0.2,
        max_tokens=int(pb.get("max_tokens", 180)), transport=transport).strip().strip('"')
    summarized, neg_summary = contradicting_looks(summarized, clause)
    beat, neg_moment = contradicting_looks(moment.replace("\n", " "), clause)
    # O começo do trecho é a ação. O fim é o que o CLIP jogava fora.
    beat = beat[:320].strip(" ,.;")
    where: list[str] = []
    if location and location != "the current scene":
        where.append(location)
    if atmosphere != "neutral":
        where.append(atmosphere.replace("+", " "))
    portrait_id = _focus_portrait_id(frame)
    lead_bits = []
    if clause:
        lead_bits.append(clause[:260].rstrip(" ,.;"))
    if beat:
        lead_bits.append(beat)
    if where:
        lead_bits.append(", ".join(where))
    if portrait_id:
        lead_bits.append("wide shot of that moment, same face, not a close-up portrait")
    lead = ", ".join(lead_bits)
    # A frase do modelo fica, mas depois da ação e antes do estilo.
    tail = ", ".join(part for part in (
        summarized,
        (entry or "")[:160] if location and location != "the current scene" else "",
        adv.get("image_style") or "",
        img_cfg["style_suffix"],
        body.extra_prompt.strip(),
    ) if part)
    if lead and tail:
        prompt = f"{lead} BREAK {tail}"
    else:
        prompt = lead or tail
    return prompt, _dedupe_negative(neg_summary, neg_moment), portrait_id


@router.post("/adventures/{adventure_id}/see")
def post_see(adventure_id: str, body: SeeRequest,
             transport: Any = Depends(get_http_transport)) -> dict:
    with db.session() as conn:
        adv = db.get_adventure(conn, adventure_id)
        if not adv:
            raise GameError.not_found(f"adventure {adventure_id} not found")
        actions = db.list_actions(conn, adventure_id)
        cards = [npc_assets.ensure_portrait_prompt(conn, card)
                 for card in db.list_cards(conn, adventure_id)]
        img_cfg = settings_store.effective_image_settings(conn)
        llm_cfg = settings_store.effective_llm_settings(conn)

    if not game_engine.acquire_generation_lock(adventure_id):
        raise GameError.conflict("generation_in_progress",
                                 "a generation is already running for this adventure")
    try:
        if not img_cfg["enabled"]:
            raise GameError(503, "image_disabled",
                            "image generation is disabled (settings.image.enabled)")

        try:
            prompt, negative_extra, portrait_id = _build_final_prompt(
                adv, actions, body, img_cfg, llm_cfg, cards, transport)
        except llm_client.LlmError as exc:
            raise GameError(503, exc.code, exc.message) from exc

        init_bytes = None
        if portrait_id and (img_cfg.get("provider") or "sdwebui") == "sdwebui":
            with db.session() as conn:
                init_bytes = _portrait_png(db.get_image(conn, portrait_id))

        image_id = db.new_id()
        try:
            # Sem width do retrato: o busto é quadrado e a cena tem o tamanho
            # configurado. O denoise deixa o lugar mudar sem trocar a pessoa.
            result = image_client.generate_image(
                img_cfg, prompt, adventure_id=adventure_id, image_id=image_id,
                transport=transport, negative_extra=negative_extra,
                init_image=init_bytes,
                denoising_strength=SEE_DENOISE if init_bytes else None)
        except image_client.ImageError as exc:
            raise GameError(503, exc.code, exc.message) from exc

        with db.session() as conn:
            last_seq = db.max_seq(conn, adventure_id)
            seq = 0 if last_seq is None else last_seq + 1
            action = db.insert_action(conn, adventure_id, seq, "see", prompt)
            db.insert_image(conn, adventure_id=adventure_id, action_seq=seq,
                            prompt=prompt,
                            negative_prompt=_dedupe_negative(
                                img_cfg["negative_prompt"], negative_extra),
                            file_path=result["file_path"],
                            provider=result["provider"],
                            width=result["width"], height=result["height"],
                            image_id=image_id)
            db.touch_adventure(conn, adventure_id)
            action = db.get_action(conn, action["id"])

        log.info("see: imagem %s gerada para aventura %s (%s)",
                 image_id, adventure_id, result["provider"])
        return {
            "id": image_id,
            "url": f"/api/images/{image_id}",
            "prompt": prompt,
            "provider": result["provider"],
            "action": action,
        }
    finally:
        game_engine.release_generation_lock(adventure_id)


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
