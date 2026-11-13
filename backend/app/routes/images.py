"""Router de imagens (v2): /see, /images/{id} e /settings/image.

Fluxo do /see (contrato docs/openapi.yaml):
1) o momento é os dois últimos trechos (não a história inteira); o LLM
   descreve o gesto em inglês, com quem faz o quê;
2) o primeiro bloco do CLIP abre com o rosto do card (nome, cabelo, pele).
   A frase em inglês da ação vem depois. A prosa crua não entra. Estilo
   fica depois do BREAK. O retrato gerado não vira img2img; a foto enviada
   entra como referência rígida do ControlNet;
3) grava a linha em `images` + action type=see (mesmo seq vinculando);
4) devolve ImageResult.

Não usa o lock de geração: o jogador segue agindo enquanto a imagem sai, e o
`see` entra no fim da aventura (`jobs.store_see`). Com `Prefer: respond-async`
(ou `?async=1`) a rota devolve 202 {job_id} e o resto vira o job `see_image`.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, Header, Query
from fastapi.responses import FileResponse, JSONResponse

from app import look_lock, access, auth, db, death_rules, image_budget, image_client, image_english, jobs, likeness, llm_client, npc_assets, scene, settings_store
from app import world_facts
from app.deps import get_http_transport
from app.errors import GameError
from app.schemas import ClipRequest, ImageSettingsUpdate, SeeRequest

log = logging.getLogger("aidungeon.images")

router = APIRouter()

_PROMPT_BUILDER_SYSTEM = """You write one caption for a text-to-image model of the moment happening right now.
One English sentence, present tense, under 40 words. Its main verb is the player order when one is given, otherwise what just happened.
Keep the place. Do not paint an earlier scene and do not invent a new one. If the moment has just moved the people to a specific spot inside that place (a cabin, a boat, a room, a cave), say that spot.
Name only the people listed. Include the player character when the text says "you".
Do not describe hair, face, eyes, skin, age, or clothing.
Do not add nudity, sex, or bare skin. If the moment does not undress anyone, they stay covered.
A salamander, frog, insect, or other creature stays that creature, not a human.
Do not turn the moment into a portrait or a close-up of one face.
Do not replace a named person with a different face, hair, or skin tone.
Do not add a person who is not listed.
If a player instruction for the picture is given, obey it.
No dialogue, no quotes, no style words.
Never copy a Portuguese word. Translate the player order and the place into English.
Output ONLY that sentence."""

_EXPLICIT_BUILDER_SYSTEM = """You describe one adult moment of an interactive story for an uncensored text-to-image model.
Write exactly one English sentence, under 35 words, describing the action happening right now: who does what to whom, in the place.
The person who is nude is the subject of the main clause and comes first. Put every clothed person in a "while <name>, ..." clause after the main clause.
Use pronouns (his, her, hers) for second mentions. Never write a possessive full name such as "Caio's body" or "A Donzela's body": that puts the clothed person before the verb and the renderer paints his trousers on the nude woman.
Use plain words. The renderer applies the authoritative clothing states separately, so do not describe clothing, nudity, undress or bare skin in the sentence. Never write "wearing", "dressed in", "nude", "naked", "undressed", "bare chest" or any garment.
Do not fade to black. Do not censor adult nudity or sex.
Do not describe hair, face, eyes or age.
Do not replace a named person with a different face, hair, or skin tone.
Do not add people who are not in the moment. Do not change the place.
Do not sexualize a child.
Never copy a Portuguese word. Translate the player order and the place into English.
Output ONLY that sentence."""

# Roupa do momento, lida na hora do See. A crônica grava a roupa atual depois
# do turno, mas leva dezenas de segundos; quem aperta o See logo depois da
# narração via a roupa antiga. Só o que o trecho ou o pedido dizem.
_WARDROBE_SYSTEM = """You report what named people are WEARING RIGHT NOW in a story passage.
Reply with ONLY one JSON object. Keys are the exact names given. Each value is one short English clothing phrase, at most 14 words, e.g. "long yellow hooded raincoat over her clothes" or "white tank top and jeans, no shirt".
Include a person ONLY when the passage names a garment that person puts on, takes off, changes into or receives, or when the player's picture instruction asks for specific clothing; the instruction wins.
Sweat, rain, dirt or mood are not clothing: without a named garment, leave the person out.
Leave everyone else out. Never invent clothing. Never describe hair, face or body.
Never describe nudity, underwear or bare skin: leave that person out instead.
Never copy a Portuguese word."""
_WARDROBE_HINT = re.compile(
    r"(?i)(?<!\w)(?:vest\w*|veste|usa(?:ndo)?|coloc\w+|p[õo]e|tir[ao]u?|tira|troc\w+|despe\w*|"
    r"capa|casaco|jaqueta|camis\w+|regata|vestido|saia|cal[çc]a|bota|chap[ée]u|capuz|armadura|"
    r"manto|uniforme|roupa\w*|encharcad\w+|molhad\w+|rasgad\w+|"
    r"wear\w*|wore|puts? on|takes? off|took off|dress\w*|changes? into|coat|jacket|shirt|"
    r"raincoat|cloak|hood|armou?r|uniform|outfit|soaked|drenched|torn)(?!\w)")


def _story_outfits(names: list[str], passage: str, order: str, *,
                   llm_cfg: dict, transport: Any) -> dict[str, str]:
    """{nome: roupa de agora} para quem o trecho ou o pedido vestiu diferente.

    Sem palavra de roupa no texto não gasta chamada. Falha ou resposta
    estranha devolve vazio: a pintura segue com a roupa conhecida.
    """
    text = "\n".join(part for part in (passage, order) if part)
    names = [name for name in names if name]
    if not names or not _WARDROBE_HINT.search(text):
        return {}
    user = (
        (f"Player instruction for the picture: {order}\n" if order else "")
        + "People: " + "; ".join(names) + "\nPassage:\n" + (passage or "(none)")[-1400:]
    )
    try:
        with image_english.IMAGE_LLM_LOCK:
            reply = llm_client.chat_completion(
                llm_cfg, [{"role": "system", "content": _WARDROBE_SYSTEM},
                          {"role": "user", "content": user}],
                role="image_prompt", temperature=0, max_tokens=160, transport=transport)
        match = re.search(r"\{.*\}", reply or "", flags=re.S)
        data = json.loads(match.group(0)) if match else {}
    except (llm_client.LlmError, ValueError) as exc:
        # Sem a leitura o See pinta a roupa antiga; antes isso não deixava rastro.
        log.warning("see: roupa do momento não lida (%s)", type(exc).__name__)
        return {}
    if not isinstance(data, dict):
        return {}
    worn: dict[str, str] = {}
    for name in names:
        aliases = {alias.casefold() for alias in (name, *npc_assets.title_aliases(name))}
        value = next((raw for key, raw in data.items()
                      if isinstance(key, str) and " ".join(key.split()).casefold() in aliases),
                     None)
        if not isinstance(value, str):
            continue
        outfit = " ".join(value.split()).strip(" .;,")
        # Nudez não entra por aqui: resposta que fala de corpo nu é descartada
        # inteira, não "limpa" (sobraria "completely" como roupa).
        if (not npc_assets.is_outfit(outfit) or not npc_assets.names_garment(outfit)
                or _PT_MARK.search(outfit)
                or likeness.strip_explicit(outfit) != outfit
                or re.search(r"(?i)\b(?:nude|naked|bare|underwear|lingerie|topless|undressed)\b",
                             outfit)):
            continue
        worn[name] = clip_words(outfit, 110)
    return worn


def _with_story_outfits(cards: list[dict], worn: dict[str, str]) -> list[dict]:
    """Cópia dos cards com a roupa lida agora por cima da roupa gravada."""
    if not worn:
        return cards
    dressed = []
    for card in cards:
        outfit = worn.get(card.get("title") or "")
        if not outfit:
            dressed.append(card)
            continue
        sheet = dict(npc_assets.parse_sheet(card.get("sheet") or ""))
        sheet["current_outfit"] = outfit
        dressed.append({**card, "sheet": json.dumps(sheet, ensure_ascii=False)})
    return dressed


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
_BODY_NOUNS = {
    "skin", "hair", "haired", "eye", "eyes", "beard", "scar",
    "olho", "olhos", "cabelo", "cabelos", "pele", "barba", "cicatriz",
}
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


def _conflicts_with_canon(phrase: str, canon_l: str) -> bool:
    """Só cor e roupa que o card não confirma viram negative.

    "olhos compostos" não está no card em inglês, mas não é contradição:
    jogar isso no negative manda o modelo evitar o olhar certo.
    """
    tokens = [
        token for token in re.findall(r"[^\W\d_]{3,}", phrase.casefold())
        if token not in _GLUE and token in _APPEARANCE_TOKENS and token not in _BODY_NOUNS
    ]
    if not tokens:
        return False
    return any(not _token_in_canon(token, canon_l) for token in tokens)


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
        if _conflicts_with_canon(phrase, canon_l):
            banned.append(phrase)
        return " "

    cleaned = pattern.sub(scrub, text)
    # O trecho de roupa some inteiro, mas o verbo fica: "A, wearing,
    # presses..." virava uma frase quebrada e o modelo pintava roupa.
    cleaned = re.sub(
        r"(?i)(?<!\w)(?:wearing|dressed\s+in|clad\s+in|vestindo|usando)\s*(?=[,.;]|$)",
        "",
        cleaned,
    )
    cleaned = re.sub(r"\s+([,.;])", r"\1", cleaned)
    cleaned = re.sub(r"(?:\s*[,.;]\s*){2,}", ", ", cleaned)
    cleaned = re.sub(r",\s*,+", ", ", cleaned)
    cleaned = re.sub(r"\s{2,}", " ", cleaned).strip(" ,.;")
    return cleaned, ", ".join(dict.fromkeys(banned))


_CLOTHING_STATE = re.compile(
    r"(?i)(?:\b(?:fully|completely|totally|entirely|still|now|already)\s+)?"
    r"\b(?:nude|naked|topless|undressed|clothed|dressed|clad)\b"
)
_CLOTHING_ACTION = re.compile(
    r"(?i)(?:"
    r"\b(?:after|while|before|by)\s+"
    r"(?:removing|taking\s+off|slipping\s+out\s+of|pulling\s+off|unbuttoning|unzipping)\s+"
    r"(?:his|her|their|the)\s+"
    r"(?:clothes|clothing|suit|dress|shirt|top|blouse|pants|trousers|skirt|robe|gown|coat|jacket|uniform|outfit)\b"
    r"(?:\s+[^,.;]{0,40})?"
    r"|"
    # O builder também descreve a roupa já removida: "while he, having
    # shed his suit, stands beside her". Isso virava contradição no CLIP.
    r"\b(?:having\s+)?(?:shed|stripped\s+(?:off|out\s+of)|taken\s+off|"
    r"removed|doffed|discarded|slipped\s+out\s+of|pulled\s+off)\s+"
    r"(?:his|her|their|the)\s+"
    r"(?:clothes|clothing|suit|dress|shirt|top|blouse|pants|trousers|skirt|robe|gown|coat|jacket|uniform|outfit)\b"
    r"(?:\s+and\s+(?:his|her|their)\s+[a-z-]+)?"
    r")"
)
_CLOTHING_PHRASE = re.compile(
    r"(?i)\b(?:wearing|dressed\s+in|clad\s+in|in)\s+"
    r"(?:a|an|the|his|her|their)?\s*"
    r"(?:[a-z-]+\s+){0,3}"
    r"(?:suit|dress|shirt|top|blouse|pants|trousers|skirt|gown|robe|coat|jacket|"
    r"armor|armour|cloak|cape|tie|underwear|bra|panties|uniform|outfit)\b"
    r"(?:\s+[^,.;]{0,50})?"
)
_BODY_EXPOSED = re.compile(
    r"(?i),\s*(?:his|her|their)\s+[^,.;]{0,100}?\b(?:body|skin|chest|torso)\b"
    r"[^,.;]{0,100}?(?=,|\.|$)"
)
_BARE_BODY = re.compile(
    r"(?i)\b(?:bare|naked|nude)\s+(?=(?:chest|body|skin|torso|shoulders?|legs?|arms?|back|breasts?|buttocks?)\b)"
)


def scrub_clothing_states(text: str) -> str:
    """Remove roupa e nudez da frase de ação.

    O estado de roupa é aplicado no começo do prompt, de forma autoritativa.
    A frase do LLM pode contradizê-lo ("Caio stands nude" com o Caio vestido)
    e o CLIP obedece a frase. Aqui a frase fica só com a ação.
    """
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return ""
    cleaned = _BODY_EXPOSED.sub("", cleaned)
    cleaned = _CLOTHING_ACTION.sub("", cleaned)
    cleaned = _CLOTHING_PHRASE.sub("", cleaned)
    cleaned = _BARE_BODY.sub("", cleaned)
    cleaned = _CLOTHING_STATE.sub("", cleaned)
    cleaned = re.sub(r"\s+([,.;])", r"\1", cleaned)
    cleaned = re.sub(r"(?:\s*,\s*){2,}", ", ", cleaned)
    cleaned = re.sub(r",\s*(?=(?:while|and)\b)", " ", cleaned)
    cleaned = re.sub(r"\b(while|and)\s*,", r"\1 ", cleaned)
    return " ".join(cleaned.split()).strip(" ,.;")


def clothing_safe_fallback(names: list[str], place: str) -> str:
    """Última linha de defesa: ação neutra, sem roupa nem nudez."""
    clean = [name for name in names if name]
    where = place or "the current scene"
    if len(clean) == 1:
        return f"{clean[0]} is in {where}."
    if len(clean) >= 2:
        return f"{clean[0]} and {clean[1]} are together in {where}."
    return f"The scene is in {where}."


def _nude_predicate(name: str, card: dict | None, label: str) -> str:
    """Estado nu em palavras positivas que o renderizador obedece.

    "Sem roupa" e "no pasties" fazem o oposto: o CLIP lê o substantivo. O
    corpo precisa ser afirmado, não negado.
    """
    if npc_assets.creature_kind(card, name):
        return "completely naked, fully nude, bare body exposed"
    if label == "adult woman":
        # Predicado curto venceu o predicado longo em 3/3 seeds no Flux2 com
        # CFG 2.0. Listar quadril/coxa/perna reintroduzia calça e sutiã.
        return ("completely naked, bare breasts fully exposed with visible "
                "nipples, natural female anatomy, her whole body bare, "
                "natural skin")
    return "completely naked, fully nude, bare body exposed"


_OUTFIT_GARMENT = re.compile(
    r"(?i)\b(suit|dress|gown|robe|armor|armour|uniform|shirt|blouse|skirt|"
    r"trousers|pants|coat|jacket|cloak|cape|tunic|vest|top)\b")
_OUTFIT_COLOR = re.compile(
    r"(?i)\b(black|white|red|blue|green|grey|gray|silver|gold|brown|purple|"
    r"pink|dark)\b")


def _short_outfit(text: str) -> str:
    """Roupa em duas ou três palavras para não vazar no corpo nu.

    "a sharply tailored black or dark grey suit" vira "black suit". O Flux2
    copia o terno inteiro para a mulher nua quando a frase do homem é longa;
    a cor + a peça bastam para vestir quem está vestido.
    """
    flat = " ".join((text or "").split()).strip(" ,.;")
    flat = re.sub(r"(?i)^(?:a|an|the)\s+", "", flat)
    garment = _OUTFIT_GARMENT.search(flat)
    color = _OUTFIT_COLOR.search(flat)
    if garment:
        piece = garment.group(1).casefold()
        return f"{color.group(1).casefold()} {piece}" if color else piece
    words = flat.split()
    return " ".join(words[:4]).casefold()


def _pronoun_for(name: str, card: dict | None) -> str:
    label = npc_assets.sex_label(name, "", "", card=card)
    if label == "adult man":
        return "his"
    if label == "adult woman":
        return "her"
    return "their"


def _clothed_predicate(card: dict | None, name: str = "") -> str:
    outfit = _short_outfit(npc_assets.current_outfit_text(card))
    pronoun = _pronoun_for(name, card)
    if outfit:
        return f"fully clothed in {pronoun} {outfit}"
    return f"fully clothed in {pronoun} current outfit"


def _name_pattern(name: str) -> re.Pattern[str]:
    """Nome com artigo opcional: "A Donzela" também casa "the Donzela".

    O LLM escreve "the Donzela da Noite" mesmo quando o card se chama
    "A Donzela da Noite". Sem isso o estado era empurrado para o começo e
    virava o sujeito da frase, que é o que pintava a calça do Caio nela.
    """
    bare = re.sub(r"(?i)^(?:a|an|the)\s+", "", name).strip()
    variants = [name] if name else []
    if bare and bare.casefold() != name.casefold():
        variants.append(bare)
    body = "|".join(re.escape(variant) for variant in variants)
    return re.compile(
        rf"(?<![\w'])(?:(?:the|a|an)\s+)?(?:{body})(?![\w'])")


def _stateful_action(sentence: str, states: list[tuple[str, str]]) -> str:
    """Mete o estado de roupa dentro da frase de ação.

    A frase do LLM é o sinal mais forte do renderizador; um bloco separado
    no começo não vence o "Caio stands nude" que ele inventa. Inserir o
    estado logo depois do nome mantém a ação e trava a roupa.

    Nome ausente não é preposto: virar o sujeito da frase é justamente o que
    fazia a calça do homem vestido aparecer na mulher nua. Ele entra como
    frase curta no fim.
    """
    cleaned = " ".join((sentence or "").split()).strip(" ,.;")
    if not cleaned:
        return ""
    missing: list[tuple[str, str]] = []
    for name, predicate in states:
        if not name or not predicate:
            continue
        match = _name_pattern(name).search(cleaned)
        if match:
            before = cleaned[:match.end()]
            after = cleaned[match.end():]
            if after.lstrip().startswith(","):
                cleaned = before + f" {predicate}," + after.lstrip()[1:]
            else:
                cleaned = before + f", {predicate}," + after
        else:
            missing.append((name, predicate))
    cleaned = re.sub(r"(?:\s*,\s*){2,}", ", ", cleaned)
    cleaned = re.sub(r"\s+([,.;])", r"\1", cleaned)
    cleaned = re.sub(r",\s*(?=(?:while|and)\b)", " ", cleaned)
    cleaned = re.sub(r"\b(while|and)\s*,", r"\1 ", cleaned)
    cleaned = " ".join(cleaned.split()).strip(" ,.;")
    if missing:
        tail = ". ".join(f"{name} is {predicate}" for name, predicate in missing)
        cleaned = f"{cleaned}. {tail}" if cleaned else tail
    return cleaned


_POSTURE_ACTION = re.compile(
    r"(?i)\b(?:lying|lies|lie|laid|reclin\w*|sitting|sits|sit|seated|"
    r"kneeling|kneels|kneel|crouch\w*|squat\w*|spraw\w*|"
    r"on\s+(?:the|a)\s+(?:bed|sofa|couch|floor|ground|chair|lap)|"
    r"on\s+(?:her|his|their)\s+back|on\s+all\s+fours|"
    r"in\s+(?:the|a)\s+(?:bed|sofa|couch|chair)|"
    r"deitad\w*|sentad\w*|ajoelh\w*|na\s+cama|no\s+sof[áa]|"
    r"no\s+ch[ãa]o|na\s+cadeira|no\s+colo)\b"
)
# Pedaços do predicado nu que o scrub deixa para trás. Eles não são ação e
# não podem entrar no miolo da frase encenada.
_STATE_SEGMENT = re.compile(
    r"(?i)^(?:completely\s+(?:naked|nude)|fully\s+(?:naked|nude)|"
    r"(?:bare\s+breasts|breasts\s+fully\s+exposed)\b[^,]*|"
    r"natural\s+(?:female\s+)?anatomy|natural\s+skin|"
    r"bare\s+(?:stomach|hips|thighs?|legs?|body)\b[^,]*|"
    r"(?:her|his|their)\s+whole\s+body\s+(?:bare|completely\s+bare)\b[^,]*|"
    r"bare\s+body\s+exposed|"
    r"(?:completely|fully|totally|entirely|still|now|already))$"
)
_STANCE_AND_ACTION = re.compile(
    r"(?i)^stands?\b[^,.;]*(?:,\s*[^,.;]*)?\s+as\s+"
    r"(?:she|he|they)\s+(.+)$"
)


def _action_core(main_clause: str, name: str) -> str:
    """A ação da pessoa nua, sem o nome nem os pedaços do estado nu."""
    match = _name_pattern(name).search(main_clause)
    if not match:
        return ""
    rest = main_clause[match.end():].lstrip(" ,")
    segments = [segment.strip() for segment in rest.split(",")]
    while segments and _STATE_SEGMENT.match(segments[0]):
        segments.pop(0)
    core = ", ".join(segment for segment in segments if segment).strip(" ,.;")
    core = re.sub(r"(?i)^(?:as\s+)?(?:she|he|they)\s+", "", core)
    core = re.sub(r"(?i)^(?:is|was)\s+", "", core)
    core = re.sub(r"(?i)^(?:and|as)\s+", "", core)
    return core.strip(" ,.;")


def _staged_explicit_action(
    sentence: str,
    nude: list[str],
    clothed: list[str],
    predicate_by_name: dict[str, str],
    pronoun_by_name: dict[str, str],
) -> str:
    """Encena a pessoa nua de corpo inteiro diante da pessoa vestida.

    O Flux2 veste a mulher nua quando a ação é só um gesto de tronco. Em
    3/3 seeds a forma que funcionou foi: sujeito nu + predicado nu + "stands
    in the center of the room, ... arms raised as she <ação>", seguida de
    "while <vestido>, <predicado>, standing before her". A postura original
    (deitada, sentada, ajoelhada) é preservada e cai no `_stateful_action`.
    """
    if len(nude) != 1 or len(clothed) != 1:
        return ""
    nude_name, clothed_name = nude[0], clothed[0]
    nude_predicate = predicate_by_name.get(nude_name) or ""
    clothed_predicate = predicate_by_name.get(clothed_name) or ""
    if not (nude_name and clothed_name and nude_predicate and clothed_predicate):
        return ""
    cleaned = " ".join((sentence or "").split()).strip(" ,.;")
    if not cleaned:
        return ""
    # A pessoa vestida não pode ser o sujeito da oração principal.
    main_clause = re.split(r"(?i)\bwhile\b", cleaned, maxsplit=1)[0].strip(" ,.;")
    core = _action_core(main_clause, nude_name)
    if not core:
        return ""
    if _POSTURE_ACTION.search(main_clause):
        return ""
    stance = _STANCE_AND_ACTION.match(core)
    if stance:
        core = stance.group(1).strip(" ,.;")
    elif re.match(r"(?i)^(?:stands?|standing)\b", core):
        # Já existe uma postura, mas sem a oração de ação que o template
        # precisa; mexer nela só trocaria uma frase por outra pior.
        return ""
    if len(core.split()) < 2:
        return ""
    possessive = pronoun_by_name.get(nude_name, "their")
    subject = {"his": "he", "her": "she"}.get(possessive, "they")
    object_ = {"his": "him", "her": "her"}.get(possessive, "them")
    words = core.split()
    if words[0].casefold().endswith("ing") or (
            len(words) > 1 and words[0].casefold().endswith("ly")
            and words[1].casefold().endswith("ing")):
        core = "is " + core
    return (
        f"{nude_name}, {nude_predicate}, stands in the center of the room, "
        f"{possessive} arms raised as {subject} {core}, "
        f"while {clothed_name}, {clothed_predicate}, standing before {object_}."
    )


_CLAUSE_CHARS = 700


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


# "as", "a", "de", "se" também aparecem em inglês e em nomes ("Dona Ana de
# Souza", "watches as Rio sleeps"): não contam.
_PT_FUNCTION = frozenset(
    "o os um uma uns umas da do das dos na no nas nos em com para pelo pela "
    "que seu sua seus suas ele ela eles elas e mas nao sobre entre ao aos esta".split())
_EN_FUNCTION = frozenset(
    "the a an of in on at with to for by and but his her their its is are was were "
    "he she they it from into over".split())


def _mostly_portuguese(text: str) -> bool:
    """Frase sem acento ainda pode ser português ("Bruno corre pelo mato"):
    mais palavras de ligação do português que do inglês. Frase sem nenhuma
    ("Marina segura a lanterna acesa") não dá para decidir e fica."""
    found = re.findall(r"[A-Za-z]+", text or "")
    # Palavra com maiúscula no meio da frase é nome ("Dona Ana", "Rio"); a
    # primeira conta em minúscula ("Eles seguem juntos").
    words = [word.lower() for index, word in enumerate(found)
             if index == 0 or not word[0].isupper()]
    pt = sum(1 for word in words if word in _PT_FUNCTION and word not in _EN_FUNCTION)
    en = sum(1 for word in words if word in _EN_FUNCTION and word not in _PT_FUNCTION)
    return pt > en


def one_image_sentence(text: str) -> str:
    """A primeira frase em inglês do resumo. O resto (entry, segunda frase) sai."""
    flat = " ".join((text or "").replace("\n", " ").split()).strip().strip('"')
    flat = re.sub(r"^>\s*You\s+(?:say\s+)?", "", flat, flags=re.IGNORECASE)
    if not flat:
        return ""
    parts = [part.strip() for part in re.findall(r"[^.!?]+[.!?]?", flat) if part.strip()]
    if not parts:
        return ""
    english = [part for part in parts
               if not _PT_MARK.search(part) and not _mostly_portuguese(part)]
    if not english:
        # Só português: melhor sem frase (o pedido e o lugar já vão em inglês)
        # do que palavras que o modelo de imagem não entende.
        return ""
    return clip_words(english[0], 280)


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
        "comfyui_base_url": cfg["comfyui_base_url"],
        "comfyui_steps": cfg["comfyui_steps"],
        "comfyui_width": cfg["comfyui_width"],
        "comfyui_height": cfg["comfyui_height"],
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
        **_public_engine_fields(cfg),
    }


def _public_engine_fields(cfg: dict) -> dict:
    """Campos que o formulário edita. Omissão no PUT não passa por aqui."""
    flux = cfg.get("comfyui_flux2") if isinstance(cfg.get("comfyui_flux2"), dict) else {}
    zimage = cfg.get("comfyui_zimage") if isinstance(cfg.get("comfyui_zimage"), dict) else {}
    video = cfg.get("comfyui_video") if isinstance(cfg.get("comfyui_video"), dict) else {}
    return {
        "comfyui_engine": cfg.get("comfyui_engine") or "qwen",
        "flux2_steps": int(flux.get("steps", 4)),
        "flux2_cfg": float(flux.get("cfg", 1.0)),
        "flux2_sampler": str(flux.get("sampler") or "euler"),
        "flux2_megapixels": float(flux.get("megapixels", 0.25)),
        "zimage_steps": int(zimage.get("steps", 8)),
        "zimage_cfg": float(zimage.get("cfg", 1.0)),
        "zimage_shift": float(zimage.get("shift", 3.0)),
        "video_engine": str(video.get("engine") or "ltxv"),
        "video_steps": int(video.get("steps", 8)),
        "video_cfg": float(video.get("cfg", 1.0)),
        "video_width": int(video.get("width", 768)),
        "video_height": int(video.get("height", 448)),
        "video_frames": int(video.get("frames", 121)),
        "video_fps": float(video.get("fps", 24)),
    }


def _fit(live: int | None, need: int) -> bool | None:
    if live is None:
        return None
    return live >= need


@router.get("/settings/image/budget")
def get_image_budget(kind: str = Query(...), engine: str = Query(...),
                     width: int = Query(...), height: int = Query(...),
                     frames: int = Query(1)) -> dict:
    """Estimativa do preset. Sempre 200. Não descarrega o narrador nem bloqueia o save."""
    if kind not in ("image", "video"):
        raise GameError(422, "validation_error", "kind deve ser image ou video")
    try:
        budget = image_budget.preset_budget(
            kind, engine, width=width, height=height, frames=frames)
    except ValueError as exc:
        raise GameError(422, "validation_error", str(exc)) from exc
    from app.routes import health as health_routes
    memory = health_routes._memory()
    if "vram_total_mb" in memory and "vram_used_mb" in memory:
        vram_free = int(memory["vram_total_mb"]) - int(memory["vram_used_mb"])
    else:
        vram_free = None
    ram_available = (
        int(memory["ram_available_mb"]) if "ram_available_mb" in memory else None)
    return {
        **budget,
        "vram_free_mib": vram_free,
        "ram_available_mib": ram_available,
        "fits_vram": _fit(vram_free, int(budget["vram_mib"])),
        "fits_ram": _fit(ram_available, int(budget["ram_mib"])),
    }


@router.get("/settings/image")
def get_image_settings() -> dict:
    with db.session() as conn:
        return _image_settings_response(conn)


@router.put("/settings/image")
def put_image_settings(body: ImageSettingsUpdate,
                       info: auth.AuthInfo = Depends(access.require_admin)) -> dict:
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
    return jobs.protagonist_from_plot(plot).get("name") or ""


def _same_person(left: str, right: str) -> bool:
    """'Sandra' e 'Sandra, A Salamandra' são a mesma pessoa."""
    a = " ".join((left or "").split()).casefold()
    b = " ".join((right or "").split()).casefold()
    if not a or not b:
        return False
    if a == b:
        return True
    a_head = a.split(",")[0].strip()
    b_head = b.split(",")[0].strip()
    return (a_head == b and "," in a) or (b_head == a and "," in b)


def _card_named(cards: list[dict], name: str) -> dict | None:
    for card in cards:
        title = card.get("title") or ""
        if title and _same_person(name, title):
            return card
    return None


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
            if _same_person(name, card.get("title") or ""):
                return card
    for card in cards:
        if (card.get("type") or "") == "character" and card.get("use_for_character_creation"):
            return card
    return None


def _cards_in_frame(cards: list[dict], text: str, plot: str) -> list[dict]:
    """Quem a pintura tem de mostrar: citados e o protagonista da aventura.

    A narração em terceira pessoa chama o NPC de "ela" e frequentemente não
    repete "você"/"Caio". Sem o card do jogador, o See recebia só a referência
    do NPC e inventava outro homem. O jogador entra sempre que está vivo e não
    foi declarado ausente; `assessment.absent` ainda o remove depois.
    """
    frame = list(scene._cast(cards, text))
    player = _player_card(cards, _protagonist_name(plot))
    if (player is not None and not death_rules.is_dead(player)
            and all(player.get("id") != card.get("id") for card in frame)):
        frame.insert(0, player)
    return frame


_ORDER_SELF = re.compile(
    r"(?i)(?<!\w)(?:eu|me|mim|comigo|você|voce|you|myself|ao meu lado|do meu lado)(?!\w)")


def _painted_cast(cast: list[dict], order: str, plot: str) -> list[dict]:
    """Quem entra na pintura, dentre os já liberados por `decide_frame`.

    O pedido do jogador manda no elenco. Se ele nomeia alguém ("Marina
    sentada, Bruno de joelhos"), a pintura mostra quem foi nomeado, e o
    protagonista só entra se o pedido fala dele ("eu", "comigo", o nome). Sem
    isso o corte em `CAST_REFERENCE_LIMIT` tirava justamente o último citado:
    o See pedido sobre o Bruno saiu sem o Bruno e com dois personagens que o
    pedido não citava. Sem nomes no pedido vale o momento narrado, e o corte
    guarda o protagonista e os citados por último.

    A recusa (menor, pessoa real) já foi decidida sobre o elenco inteiro da
    cena; aqui só se escolhe quem aparece.
    """
    limit = likeness.CAST_REFERENCE_LIMIT
    player = _player_card(cast, _protagonist_name(plot))
    asked = list(scene._cast(cast, order)) if (order or "").strip() else []
    if asked:
        frame = asked[-limit:]
        if (player is not None and _ORDER_SELF.search(order) and len(frame) < limit
                and all(player.get("id") != card.get("id") for card in frame)):
            frame.insert(0, player)
        return frame
    if len(cast) <= limit:
        return list(cast)
    others = [card for card in cast if player is None or card.get("id") != player.get("id")]
    if player is not None:
        return [player, *others[-(limit - 1):]]
    return others[-limit:]


def _look_clause(frame: list[dict], *, explicit: bool = False) -> str:
    """Rosto travado no começo do prompt. Sem isso o CLIP pinta um estranho.

    Cena nua ou sexual guarda o rosto e solta a roupa do card.
    """
    return likeness.opening_clause(frame, explicit=explicit)


def _require_cast(adventure_id: str, cast: list[dict]) -> None:
    """Exige o PNG de cada membro. Na falta, grava o retrato e responde 409."""
    if not cast:
        return
    try:
        with db.session() as conn:
            scene.cast_reference_plan(conn, adventure_id, cast)
    except GameError as exc:
        if exc.code != "portrait_required":
            raise
        scene.queue_missing_portraits(adventure_id, cast)
        raise


def _is_interaction(text: str, names: list[str]) -> bool:
    """Gesto entre pessoas, ou mais de um nome no quadro."""
    folded = (text or "").casefold()
    if any(re.search(rf"(?<!\w){re.escape(word)}", folded) for word in _INTERACTION_WORDS):
        return True
    return len([name for name in names if name.strip()]) >= 2


def _explicit_order_lock(order: str) -> str:
    """Instrução original prioritária; nudez específica sai de nude_names."""
    folded = (order or "").casefold()
    if not re.search(r"(?:\bnud|\bnua\b|\bnude\b|\bnaked\b|sem roupa|tirar\w*\s+(?:a\s+)?roupa)", folded):
        return ""
    return f"Player instruction, highest priority: {order}"


def _build_final_prompt(adv: dict, actions: list[dict], body: SeeRequest,
                        img_cfg: dict, llm_cfg: dict, cards: list[dict],
                        transport: Any) -> tuple[
                            str, str, str, list, list[str], list[str], int, bool]:
    """O momento atual + o rosto do card.

    Devolve prompt, negative, caption, frame, cast_card_ids,
    reference_image_ids e expected_people. Os ids seguem `decision.cast`,
    protagonista incluso.

    O CLIP pinta o começo de cada bloco. O primeiro bloco abre com o rosto
    do card (cabelo, pele, porte); a frase em inglês vem em seguida. A
    prosa crua não entra. O estilo fica depois do BREAK. caption é a frase
    limpa gravada na action see. O retrato gerado não entra como img2img.
    A foto enviada volta no quarto item, para travar o rosto na cena.
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
    decision = likeness.decide_frame(
        frame, explicit=scene.is_explicit(raw_moment) or scene.is_explicit(order))
    if decision.refusal:
        raise GameError(422, decision.refusal, decision.message)
    # A pintura segue a narracao: um pedido do jogador nao veste a cena.
    # O See e o fundo do chat usam a mesma regra (scene.narration_moment).
    explicit = decision.allow_explicit and (
        scene.is_explicit(scene.narration_moment(actions, body.action_seq))
        or scene.is_explicit(order)
    )
    if world_facts.REFUSAL_IMAGE in assessment.image_directive:
        # Apenas uma recusa estabelecida rebaixa a instrução explícita.
        explicit = explicit and scene.is_explicit(assessment.safe_excerpt)
    # O pedido escolhe o elenco só na cena comum; a cena explícita segue com
    # o elenco do momento, como antes.
    frame = list(decision.cast)[:likeness.CAST_REFERENCE_LIMIT] if explicit else \
        _painted_cast(list(decision.cast), order, adv.get("plot_essentials") or "")
    cast_locked = list(frame)
    _require_cast(adv["id"], cast_locked)
    if not explicit:
        order = likeness.strip_explicit(order)
    # Roupa do momento lida agora (cena comum): entra nos cards antes de montar
    # a linha de cada pessoa, para o vestido novo ficar colado ao nome dela.
    worn = {} if explicit else _story_outfits(
        [card.get("title") or "" for card in frame], moment, order,
        llm_cfg=llm_cfg, transport=transport)
    frame = _with_story_outfits(frame, worn)
    cast_locked = list(frame)
    clause = _look_clause(frame, explicit=explicit) or (
        "" if explicit else npc_assets.image_canon_clause(frame, moment)
    )
    names = [card.get("title") for card in frame if card.get("title")]
    if not names:
        names = [card.get("title") for card in scene._cast(cards, raw_moment) if card.get("title")]
    player_name = _protagonist_name(adv.get("plot_essentials") or "") or "the player character"
    you = re.search(r"(?<!\w)you(?!\w)", raw_moment or "", re.IGNORECASE)
    # Pedido que nomeia quem aparece e não fala do jogador: ele fica de fora.
    order_names_cast = bool(not explicit and order and scene._cast(cast_locked, order))
    if order_names_cast and not _ORDER_SELF.search(order):
        you = None
    if you and not any(_same_person(player_name, name) for name in names):
        names.insert(0, player_name)
    story_blob = "\n".join(
        (action.get("text") or "") for action in visible if (action.get("text") or "").strip()
    )
    by_name = {card.get("title"): card for card in cast_locked if card.get("title")}
    collapsed: list[str] = []
    for name in names:
        twin = next((other for other in collapsed if _same_person(name, other)), "")
        if twin:
            if len(name) > len(twin):
                collapsed[collapsed.index(twin)] = name
            continue
        collapsed.append(name)
    names = collapsed
    allowed = [card.get("title") or "" for card in cast_locked]
    names = [
        name for name in names
        if _same_person(player_name, name)
        or any(_same_person(allowed_name, name) for allowed_name in allowed)
    ]
    # O estado de roupa é decidido antes do LLM: o pedido do jogador manda
    # quando nomeia quem fica nu; o resto do elenco continua vestido.
    locked = cast_locked[:likeness.CAST_REFERENCE_LIMIT]
    nude_titles, _covered_titles = npc_assets.scene_clothing(locked, moment, order)
    nude_keys = {title.casefold() for title in nude_titles if title}
    nude = [
        card.get("title") for card in locked
        if (card.get("title") or "").casefold() in nude_keys
    ]
    clothed = [
        card.get("title") for card in locked
        if (card.get("title") or "").casefold() not in nude_keys
        and not npc_assets.creature_kind(card, card.get("title") or "")
    ]
    predicate_by_name: dict[str, str] = {}
    for card in locked:
        name = card.get("title") or ""
        if not name:
            continue
        if name.casefold() in nude_keys:
            label = "" if npc_assets.creature_kind(card, name) else \
                npc_assets.sex_label(name, story_blob, moment, card=card)
            predicate_by_name[name] = _nude_predicate(name, card, label)
        else:
            predicate_by_name[name] = _clothed_predicate(card, name)
    # Trava de sexo na frente do prompt: mulher fica mulher, homem fica homem.
    # Criatura não vira "adult woman": isso pinta um humano nu.
    sex_bits = []
    seen_sex: set[str] = set()
    resolved: dict[str, dict | None] = {}
    for name in names:
        key = name.casefold()
        if key in seen_sex:
            continue
        seen_sex.add(key)
        card = by_name.get(name) or _card_named(cards, name)
        resolved[name] = card
        kind = npc_assets.creature_kind(card, name)
        label = "" if kind else npc_assets.sex_label(
            name, story_blob, moment, card=card)
        phrase = npc_assets.sex_phrase(name, label, explicit=explicit, card=card)
        if phrase:
            sex_bits.append(phrase)
    sex_bits.sort(key=lambda bit: 0 if "adult woman" in bit else 1)
    who_bits = []
    for name in names:
        card = resolved.get(name) or by_name.get(name) or _card_named(cards, name)
        kind = npc_assets.creature_kind(card, name)
        if kind:
            who_bits.append(f"{name} ({kind})")
            continue
        label = npc_assets.sex_label(name, story_blob, moment, card=card)
        who_bits.append(f"{name} ({label})" if label else name)
    canon = ""
    if clause and explicit:
        canon = (
            "Identity only, face and hair: "
            + clause
            + ". Preserve each person's requested clothing state and action."
        )
    elif clause:
        canon = (
            "Character reference, keep them recognizable: "
            + clause
            + ". Keep the same face, hair, age and build. The player's requested "
            "moment, clothing and action take priority over older descriptions."
        )
    # O cânone inteiro, não só o último trecho: o lugar pode ter sido
    # estabelecido na abertura e a narração recente só falar de chuva.
    location, entry = scene._location(cards, assessment.canon_blob or moment)
    if (assessment.where != "unestablished"
            and not world_facts.is_atmospheric_place_name(assessment.where, cards)):
        # Lugar canônico vence: pinta o lugar real, não o destino da tentativa.
        location = assessment.where
        entry = ""
        for card in cards:
            if (card.get("type") or "") == "location" and \
                    (card.get("title") or "").casefold() == location.casefold():
                entry = card.get("entry") or ""
                break
    # Ponto sem cartão em que a história entrou (cabana, caverna, barco): o See
    # pinta esse ponto dentro do lugar, não a descrição do cartão.
    from app import scene_spot
    location, entry = scene_spot.place(
        location, entry, scene_spot.current(adv, visible, cards, location=location,
                                            assessment=assessment))
    atmosphere = scene.atmosphere_of(assessment.safe_excerpt or moment)
    place = ""
    if location and location != "the current scene":
        place = f"Place: {location}. {clip_words(entry or '', 200)}\n"
    who = ", ".join(who_bits) if who_bits else "only the people named in the moment"
    clothing_states = ""
    if explicit:
        states = []
        for name in nude:
            predicate = predicate_by_name.get(name)
            if predicate:
                states.append(f"{name} is {predicate}")
        for name in clothed:
            predicate = predicate_by_name.get(name)
            if predicate:
                states.append(f"{name} is {predicate}")
        if states:
            clothing_states = (
                "Authoritative clothing states (obey exactly; never contradict them): "
                + "; ".join(states) + ".\n"
            )
    nude_instruction = ""
    if explicit and nude:
        first = nude[0]
        nude_instruction = (
            f"The nude person is {first}. Start the action sentence with "
            f"{first}. "
        )
        if clothed:
            nude_instruction += (
                f"Put {', '.join(clothed)} in a \"while ...\" clause after "
                "the main clause, never as the subject of the first clause. "
            )
        nude_instruction += "\n"
    # Com diretiva (recusa/bloqueio), o builder recebe a versão canônica —
    # a tentativa crua do jogador não chega nem ao LLM de prompt.
    painted_source = moment if assessment.image_directive else \
        moment_for_painter(actions, body.action_seq)
    painted = painted_source or "(an empty scene)"
    user = (
        ("Player instruction for the picture (highest priority; obey it and remove any conflicting clothing, pose or composition from the moment): " + order + "\n" if order else "")
        + (canon + "\n\n" if canon else "")
        + f"People who may appear, and no one else: {who}.\n"
        + clothing_states
        + nude_instruction
        + place
        + "Moment to paint:\n"
        + painted
    )
    messages = [
        {"role": "system", "content": _EXPLICIT_BUILDER_SYSTEM if explicit else _PROMPT_BUILDER_SYSTEM},
        {"role": "user", "content": user},
    ]
    with image_english.IMAGE_LLM_LOCK:
        summarized = llm_client.chat_completion(
            llm_cfg, messages, role="image_prompt", temperature=0.2,
            max_tokens=int(pb.get("max_tokens", 180)),
            transport=transport).strip().strip('"')
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
    elif explicit:
        # O builder pode inverter os estados ("Caio stands nude" com o Caio
        # vestido). A frase fica só com a ação e recebe o estado correto
        # dentro dela: o renderizador obedece a ação, não um bloco separado.
        cleaned = scrub_clothing_states(sentence)
        if not cleaned:
            cleaned = clothing_safe_fallback(names, location)
        states = [
            (name, predicate_by_name[name])
            for name in names if name in predicate_by_name
        ]
        pronoun_by_name = {
            name: _pronoun_for(
                name,
                resolved.get(name) or by_name.get(name) or _card_named(cards, name),
            )
            for name in names
        }
        # Só encena quando o quadro tem exatamente duas pessoas; com uma
        # terceira criatura o template deixaria a ação dela de fora.
        staged = _staged_explicit_action(
            cleaned, nude, clothed, predicate_by_name, pronoun_by_name,
        ) if len(names) == 2 else ""
        # Todo mundo vestido (a cena só foi marcada pela palavra da narração):
        # o estado já abre o prompt. Enfiado na frase, ela virava "Caio fully
        # clothed in his blue shirt, Ladrão, fully clothed in his worn boots
        # and you watch", e o modelo perdia a ação.
        sentence = (staged or _stateful_action(cleaned, states) or cleaned) if nude else cleaned
    where: list[str] = []
    if location and location != "the current scene":
        where.append(location)
    if atmosphere != "neutral":
        where.append(atmosphere.replace("+", " "))
    order_en = image_english.render_english(
        order,
        transport=transport,
        llm_cfg=llm_cfg,
        context=f"{sentence}. Place: {location}.",
    ) if order else ""
    order_lock = _explicit_order_lock(order_en) if explicit else ""
    lead_bits = [order_lock] if order_lock else []
    # Trava de sexo antes de tudo. Recusa estabelecida: a frase visual da
    # recusa vem antes do rosto (senão o CLIP pinta o ato recusado). Caso
    # normal: o rosto lidera o primeiro chunk — "kisses on the sofa" na
    # frente pintava um casal genérico.
    if sex_bits and not explicit:
        lead_bits.append(". ".join(sex_bits))
    creature = any(
        npc_assets.creature_kind(resolved.get(name) or _card_named(cards, name), name)
        for name in names
    )
    refusal = world_facts.REFUSAL_IMAGE in assessment.image_directive
    if refusal:
        lead_bits.append(sentence)
    if clause:
        # Traços inteiros de cada pessoa (scene._shrink_look): o corte por
        # caracteres deixava as duas últimas de quatro sem linha nenhuma.
        lead_bits.append(scene._shrink_look(clause, _CLAUSE_CHARS))
    if not explicit:
        # Depois do rosto. Na frente, o CLIP larga a cara e pinta só a roupa.
        lead_bits.append(npc_assets.coverage_lock(*locked) if locked else (
            npc_assets.CREATURE_LOCK if creature else npc_assets.CLOTHED_LOCK))
        # Roupa do momento e marcas já estão na linha de cada pessoa; esta
        # frase só faz o texto vencer a roupa da referência.
        state_line = npc_assets.story_state_note(frame)
        if state_line:
            lead_bits.append(state_line)
            # A referência fica para rosto, cabelo e corpo; sem esta frase as
            # referências puxavam de volta a roupa antiga no See.
            guard = scene._reference_layout_guard(locked) if locked else ""
            if guard:
                lead_bits.append(guard)
    # A instrução do jogador não engole mais a ação: antes o order_lock
    # omitia a frase do momento, e "donzela sem roupa" virava só um retrato.
    if sentence and not refusal:
        lead_bits.append(sentence)
    if order_en and not order_lock:
        lead_bits.append(order_en)
    if where:
        lead_bits.append("Place: " + ", ".join(where))
    if entry and entry == scene_spot._INDOOR_NOTE:
        # Dentro de um ponto sem cartão (cabana, caverna): a nota só ia para o
        # construtor de frase e a imagem final podia sair ao ar livre.
        lead_bits.append(entry)
    lead_bits.append("same continuous scene, coherent bodies, preserve every person's stated action and position")
    # Bicho conta como bicho na cena comum: "Exactly 4 people" com um lobo no
    # elenco pintava quatro humanos e o lobo.
    beasts = 0 if explicit else sum(
        1 for name in names
        if npc_assets.animal_kind(
            resolved.get(name) or by_name.get(name) or _card_named(cards, name), name))
    if names:
        counted = npc_assets.count_phrase(len(names) - beasts, beasts) if beasts \
            else f"{len(names)} people"
        lead_bits.append(
            f"Exactly {counted}, no more, no fewer: " + ", ".join(who_bits)
        )
        lead_bits.append(
            "no other people in the background or elsewhere, no patrons, no bystanders"
            if explicit else scene._alone_there(len(names), beasts))
    if len(locked) > 1:
        if explicit:
            lead_bits.append(
                "distinct different people in the same shared space, no split screen, "
                "no collage, no duplicate faces: " + ", ".join(card.get("title") or "person" for card in locked)
            )
        else:
            # Em fila, da esquerda para a direita. "top left"/"top right" para
            # o terceiro e o quarto empilhava gente numa cena larga.
            rows = {2: ["left", "right"], 3: ["left", "centre", "right"],
                    4: ["far left", "centre left", "centre right", "far right"]}
            positions = rows.get(len(locked)) or [
                f"position {index + 1} from the left" for index in range(len(locked))]
            def placed(card: dict) -> str:
                title = card.get("title") or "person"
                kind = npc_assets.animal_kind(card, title)
                return f"{title} (the {kind})" if kind else title

            assignments = ", ".join(
                f"{placed(card)} on the {positions[index]}"
                for index, card in enumerate(locked)
            )
            # "people" com o lobo no meio pedia mais um humano ao lado do bicho.
            crowd = (
                "distinct different subjects, each one once, no duplicate faces: "
                if beasts else
                "distinct different people, not the same person, no duplicate faces: "
            )
            lead_bits.append(crowd + assignments)
    if _is_interaction(moment, names) and not explicit:
        lead_bits.append("wide shot of that action, people clothed, not a close-up portrait"
                         if beasts else
                         "wide shot of that action, clothes on, not a close-up portrait")
    suffix = re.sub(r"(?i)\b(?:nude|naked)\b", "", img_cfg["style_suffix"] or "")
    suffix = " ".join(suffix.replace(", ,", ",").split()).strip(" ,")
    if creature and not explicit and "natural skin texture" in suffix:
        suffix = suffix.replace(
            "natural skin texture",
            "scales, hide or fur, not human skin",
        )
    if explicit:
        # Blocos de frase, não uma cadeia de vírgulas. O Flux2 obedece o
        # começo de cada frase; o estado nu lidera e a ação vem logo depois.
        # Repetir o estado depois da ação foi o que derrubou o thong.
        blocks: list[str] = []
        if order_lock:
            blocks.append(order_lock.rstrip(".") + ".")
        states = [
            (name, predicate_by_name[name])
            for name in nude if name in predicate_by_name
        ] + [
            (name, predicate_by_name[name])
            for name in clothed if name in predicate_by_name
        ]
        for name, predicate in states:
            blocks.append(f"{name} is {predicate}.")
        if clause:
            # Rosto e cabelo de cada um (sem a roupa do card). Sem este bloco o
            # See explícito só dizia quem estava vestido e pintava estranhos:
            # a Renata saía sem óculos e com outro cabelo.
            blocks.append(scene._shrink_look(clause, _CLAUSE_CHARS).rstrip(".") + ".")
        if len(names) == 1:
            blocks.append(
                f"{names[0]} is the only person in the entire scene, no one else.")
        blocks.append(
            "Wide full-body composition, entire body visible, not a close-up portrait.")
        if sentence:
            blocks.append(sentence.rstrip(".") + ".")
        if order_en and not order_lock:
            blocks.append(order_en.rstrip(".") + ".")
        if nude:
            # Só com alguém nu o estado volta depois da ação; com todos
            # vestidos era a mesma frase três vezes no prompt.
            for name, predicate in states:
                blocks.append(f"{name} is {predicate}.")
        if location and location != "the current scene":
            blocks.append(f"Place: {location}.")
        if names:
            if len(who_bits) == 2:
                people = " and ".join(who_bits)
            else:
                people = ", ".join(who_bits)
            noun = "person" if len(names) == 1 else "people"
            blocks.append(
                f"Exactly {len(names)} {noun}: {people}, no one else, "
                "no other people in the background.")
        blocks.append("Wide shot, full bodies, not a close-up portrait.")
        lead = " ".join(block for block in blocks if block)
        explicit_suffix = img_cfg["style_suffix"] or ""
        if not nude:
            explicit_suffix = re.sub(
                r"(?i)\b(?:nude|naked)\b", "", explicit_suffix)
            explicit_suffix = " ".join(
                explicit_suffix.replace(", ,", ",").split()).strip(" ,")
        tail = ", ".join(part for part in (
            adv.get("image_style") or "", explicit_suffix) if part)
        prompt = f"{lead} BREAK {tail}" if lead and tail else lead or tail
    else:
        lead = ", ".join(lead_bits)
        tail = ", ".join(part for part in (
            adv.get("image_style") or "",
            suffix,
        ) if part)
        prompt = f"{lead} BREAK {tail}" if lead and tail else lead or tail
    negative = _dedupe_negative(neg_summary, assessment.image_negative)
    if explicit and nude:
        negative = _dedupe_negative(
            negative, npc_assets.EXPLICIT_CLOTHING_NEGATIVE)
    if not explicit:
        negative = _dedupe_negative(negative, neg_moment, npc_assets.NUDITY_BAN)
    if clause:
        negative = _dedupe_negative(negative, npc_assets.IDENTITY_NEGATIVE)
    if _is_interaction(moment, names):
        negative = _dedupe_negative(negative, _PORTRAIT_NEGATIVE)
    caption = sentence or summarized
    final_cast = frame[:likeness.CAST_REFERENCE_LIMIT]
    if [card.get("id") for card in final_cast] != [card.get("id") for card in cast_locked]:
        _require_cast(adv["id"], final_cast)
        cast_locked = final_cast
    # Cena explícita usa o mestre de corpo inteiro + ficha, não só o busto:
    # o rosto continua travado, mas a pessoa tem corpo e ângulos coerentes.
    # Só recorta o rosto quando alguém fica nu: aí a roupa da ficha atrapalha.
    # Com todos vestidos ("bunda" na narração já marca explícito), mestre e
    # ficha mantêm corpo, óculos, cabelo e roupa da pessoa.
    face_refs = bool(explicit and nude)
    cast_card_ids, reference_image_ids = likeness.cast_reference_ids_for_provider(
        cast_locked, str(img_cfg.get("provider") or "sdwebui"), explicit=face_refs)
    return (prompt, negative, caption, frame, cast_card_ids,
            reference_image_ids, len(names) - beasts, explicit, face_refs)


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
        jobs.ensure_player_card(conn, adv)
        actions = db.list_actions(conn, adventure_id)
        cards = [npc_assets.ensure_portrait_prompt(conn, card)
                 for card in db.list_cards(conn, adventure_id)]
        img_cfg = image_client.effective_settings(conn)
        llm_cfg = settings_store.effective_llm_settings(conn)
    # Todo mundo que pode entrar na cena leva os traços da própria referência.
    # Quem ainda não tem look é lido aqui (uma vez por card), fora da fila.
    cards = look_lock.ensure_many(adventure_id, cards, transport=transport, llm_cfg=llm_cfg)
    try:
        (prompt, negative_extra, caption, frame, cast_card_ids,
         reference_image_ids, expected_people, explicit, face_refs) = _build_final_prompt(
            adv, actions, body, img_cfg, llm_cfg, cards, transport)
    except image_client.ImageError as exc:
        raise GameError(503, "generation_failed", str(exc)) from exc
    except llm_client.LlmError as exc:
        raise GameError(503, exc.code, exc.message) from exc
    # Cena explícita no Flux2: CFG 2.0 para o negativo de roupa valer.
    # O valor não é persistido, e entra no cache_key abaixo.
    flux2_cfg = 2.0 if explicit else None
    if flux2_cfg is not None:
        img_cfg = image_client.with_flux2_cfg(img_cfg, flux2_cfg)
    try:
        with db.session() as conn:
            references = (
                likeness.load_reference_images(conn, adv["id"], reference_image_ids)
                if reference_image_ids else []
            )
    except GameError as exc:
        if exc.code == "portrait_required":
            scene.queue_missing_portraits(
                adventure_id, frame[:likeness.CAST_REFERENCE_LIMIT])
        raise
    original_hashes = likeness.reference_hashes(references)
    if face_refs:
        references = likeness.identity_face_pngs(references)
    if img_cfg.get("provider") not in ("comfyui", "sdwebui"):
        # OpenAI-compatible não tem âncoras visuais de identidade.
        reference_image_ids = []
        references = []
    base_negative = (
        npc_assets.negative_without_nudity_ban(img_cfg["negative_prompt"])
        if explicit else img_cfg["negative_prompt"]
    )
    if explicit:
        negative_extra = npc_assets.negative_without_nudity_ban(negative_extra)
    return {
        "prompt": prompt,
        "negative_extra": negative_extra,
        "negative_prompt": _dedupe_negative(base_negative, negative_extra),
        "base_negative": base_negative,
        "caption": caption,
        "cast_card_ids": cast_card_ids,
        "reference_image_ids": reference_image_ids,
        "reference_hashes": original_hashes,
        "reference_provider": str(img_cfg.get("provider") or "sdwebui"),
        "expected_people": expected_people,
        "explicit": explicit,
        "reference_faces": face_refs,
        "flux2_cfg": flux2_cfg,
        "cache_key": image_client.request_key(
            {**img_cfg, "negative_prompt": base_negative}, prompt, negative_extra=negative_extra, variation=variation,
            reference_images=references or None),
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
        with db.session() as conn:
            references = (
                likeness.load_reference_images(
                    conn, adventure_id, plan.get("reference_image_ids") or [])
                if plan.get("reference_image_ids") else []
            )
        if likeness.reference_hashes(references) != list(plan.get("reference_hashes") or []):
            raise likeness.portrait_required()
        if jobs._reference_faces(plan):
            references = likeness.identity_face_pngs(references)
        cfg_for_plan = image_client.with_flux2_cfg(
            {**img_cfg, "negative_prompt": plan["base_negative"]},
            plan.get("flux2_cfg"))
        # O caminho assíncrono (o da interface) passa o prompt para inglês
        # antes de pintar; aqui o título e os trechos em português iam crus.
        with db.session() as conn:
            llm_cfg = settings_store.effective_llm_settings(conn)
        painted_prompt = image_english.render_english(
            plan["prompt"], transport=transport, llm_cfg=llm_cfg)
        result = image_client.generate_image(
            cfg_for_plan,
            painted_prompt, adventure_id=adventure_id, image_id=image_id,
            transport=transport, negative_extra=plan["negative_extra"],
            reference_images=references or None,
            expected_people=plan.get("expected_people"))
    except image_client.ImageError as exc:
        raise GameError(503, exc.code, exc.message) from exc
    image = jobs.store_see(adventure_id, plan["caption"], result, image_id=image_id,
                           prompt=plan["prompt"], negative_prompt=plan["negative_prompt"],
                           cache_key=plan["cache_key"])
    log.info("see: imagem %s gerada para aventura %s (%s)",
             image_id, adventure_id, result["provider"])
    return image


# --------------------------------------------------------------- clipe Wan
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _clip_source_bytes(image: dict) -> bytes:
    """PNG raster desta aventura. Vídeo, outro MIME ou arquivo ilegível não servem."""
    media = str(image.get("media_type") or "image/png").split(";", 1)[0].strip().lower()
    if media not in ("image/png", "image/x-png"):
        raise GameError.not_found(f"image {image.get('id')} not found")
    try:
        raw = likeness.read_stored_image_bytes(image)
    except (OSError, FileNotFoundError) as exc:
        raise GameError.not_found(f"image {image.get('id')} not found") from exc
    if not raw.startswith(_PNG_MAGIC):
        raise GameError.not_found(f"image {image.get('id')} not found")
    return raw


@router.post("/adventures/{adventure_id}/clips", status_code=202)
def post_clip(adventure_id: str, body: ClipRequest,
              info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    """Enfileira scene_clip. Não gera aqui e não chama narrador nem voz."""
    prompt = " ".join(body.prompt.split())
    if not prompt:
        raise GameError.bad_request("validation_error", "prompt is required")
    with db.session() as conn:
        adventure = db.get_adventure(conn, adventure_id)
        if not adventure or not access.can_see(info.user, adventure.get("owner")):
            raise GameError.not_found(f"adventure {adventure_id} not found")
        img_cfg = image_client.effective_settings(conn)
        if not img_cfg["enabled"]:
            raise GameError(503, "image_disabled",
                            "image generation is disabled (settings.image.enabled)")
        source = db.get_image(conn, body.image_id)
        if not source or source["adventure_id"] != adventure_id:
            raise GameError.not_found(f"image {body.image_id} not found")
        raw = _clip_source_bytes(source)
        job = jobs.enqueue_scene_clip(
            conn, adventure_id, source["id"], prompt,
            cache_key=image_client.clip_request_key(img_cfg, prompt, raw),
        )
    return {"job_id": job["id"]}


def _served_media_type(image: dict) -> str:
    media = str(image.get("media_type") or "image/png").split(";", 1)[0].strip().lower()
    if media in ("image/png", "image/x-png", ""):
        return "image/png"
    if media == "video/mp4":
        return "video/mp4"
    if str(image.get("file_path") or "").lower().endswith(".mp4"):
        return "video/mp4"
    return "image/png"


# ------------------------------------------------------------- arquivo
@router.get("/images/{image_id}")
def get_image(image_id: str, download: bool = Query(default=False)) -> FileResponse:
    with db.session() as conn:
        image = db.get_image(conn, image_id)
    if not image:
        raise GameError.not_found(f"image {image_id} not found")
    try:
        path = likeness.stored_image_path(image["file_path"])
    except OSError:
        raise GameError.not_found(f"image file for {image_id} not found")
    if not path.is_file():
        raise GameError.not_found(f"image file for {image_id} not found")
    media = _served_media_type(image)
    return FileResponse(path, media_type=media, filename=path.name if download else None)
