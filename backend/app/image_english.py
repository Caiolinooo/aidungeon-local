"""Rewrite assembled positive image prompts into English before generation."""

from __future__ import annotations

from collections import OrderedDict
import re
import threading

from app import db, llm_client, settings_store

_SYSTEM = """You rewrite an image-model prompt into English.
Return only the rewritten prompt. No JSON, no markdown, no quotes, no explanation.
Rules:
- Translate every non-English word into English.
- Keep each placeholder [[0]], [[1]], [[2]] and so on exactly as written.
{break_rule}
- Keep every English word and every number.
- Do not add people, clothing, nudity, style, camera directions, or actions.
- If the prompt is already English, return it unchanged."""
# A regra do BREAK só entra quando a fonte tem BREAK. Citada num prompt sem
# BREAK, o modelo passa a trocar cada quebra de linha por um, e a contagem
# diferente reprovava um prompt que já estava em inglês.
_BREAK_KEEP = "- Keep the token BREAK exactly where it is, with the same count."
_BREAK_NONE = "- Never add the token BREAK."
_STRAY_BREAK = re.compile(r"\s*\bBREAK\b\s*")
_RETRY_SUFFIX = "The previous answer was rejected. Return only the English prompt."
_FAILURE = "generation_failed: image prompt could not be translated to English"
_NAME_SPAN = re.compile(
    r"[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][\w'’.-]*"
    r"(?:[ \t]+(?:d[aeo]s?|e|y|of|the|and)[ \t]+[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][\w'’.-]*"
    r"|[ \t]+[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][\w'’.-]*)+"
    r"|[A-ZÁÀÂÃÉÊÍÓÔÕÚÇ][\w'’.-]*[àáâãäéêíóôõúüç][\w'’.-]*"
)
_FENCE = re.compile(r"```(?:json)?[ \t]*\n?(.*?)\n?```", re.IGNORECASE | re.DOTALL)
_CACHE: OrderedDict[str, str] = OrderedDict()
_CACHE_LOCK = threading.Lock()
# O llama-server admite uma auxiliar por slot. Sem esta trava, dois jobs
# visuais disputam o mesmo slot e o segundo estoura o prazo de 20 s mesmo com
# o modelo saudável. A fila aqui acontece antes do prazo do role.
IMAGE_LLM_LOCK = threading.Lock()
_CACHE_SIZE = 128


def _mask_names(text: str) -> tuple[str, list[str]]:
    names: list[str] = []

    def replace(match: re.Match[str]) -> str:
        names.append(match.group(0))
        return f"[[{len(names) - 1}]]"

    return _NAME_SPAN.sub(replace, text), names


def _clean_reply(reply: str) -> str:
    text = reply.strip()
    fenced = _FENCE.fullmatch(text)
    if fenced:
        text = fenced.group(1).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        text = text[1:-1].strip()
    return text


def _system_for(masked: str) -> str:
    rule = _BREAK_KEEP if "BREAK" in masked else _BREAK_NONE
    return _SYSTEM.format(break_rule=rule)


def _drop_stray_breaks(masked: str, reply: str) -> str:
    """Remove BREAKs the model invented for a source that has none."""
    if "BREAK" in masked or "BREAK" not in reply:
        return reply
    return _STRAY_BREAK.sub(" ", reply).strip()


def _accepted(masked: str, reply: str) -> bool:
    if not reply or reply.count("BREAK") != masked.count("BREAK"):
        return False
    # Language cannot be inferred from accents: unaccented Portuguese must be
    # translatable. Lexical preservation is instructed in _SYSTEM; these are
    # the structural invariants that can be checked without a dictionary.
    placeholders = re.findall(r"\[\[\d+\]\]", masked)
    return all(placeholder in reply for placeholder in placeholders)


def _cached(text: str) -> str | None:
    with _CACHE_LOCK:
        value = _CACHE.get(text)
        if value is not None:
            _CACHE.move_to_end(text)
        return value


def _remember(text: str, translated: str) -> None:
    with _CACHE_LOCK:
        _CACHE[text] = translated
        _CACHE.move_to_end(text)
        while len(_CACHE) > _CACHE_SIZE:
            _CACHE.popitem(last=False)


def _failed() -> None:
    from app import image_client
    raise image_client.ImageError(_FAILURE)


_PT_STOPWORDS = {
    # "do" e "no" ficam de fora: também são inglês ("do not", "no jewelry").
    "de", "da", "dos", "das", "em", "na", "nos", "nas",
    "um", "uma", "uns", "umas", "com", "sem", "para", "por", "que",
    "olhos", "cabelos", "pele", "corpo", "rosto", "mulher", "homem",
    "vestindo", "usando", "roupa", "fundo", "cabeça", "braço",
    "perna", "calça", "camisa", "vestido", "olhando", "sorrindo",
}


def _is_probable_english(text: str) -> bool:
    # If text has no typical Portuguese stopwords and has ASCII letters, it is safe to treat as English
    words = re.findall(r"[a-zA-ZáàâãéêíóôõúçÁÀÂÃÉÊÍÓÔÕÚÇ]+", text.lower())
    if not words:
        return True
    pt_hits = sum(1 for w in words if w in _PT_STOPWORDS)
    return pt_hits == 0


def render_english(text: str, *, transport=None,
                   llm_cfg: dict | None = None,
                   context: str = "",
                   allow_passthrough: bool = True) -> str:
    """Return an English provider prompt, or fail rather than send the source.

    `context` is the story beat the fragment belongs to. It goes as a second
    system message, never into the user content, so the model translates
    pronouns and actions with the scene in view without echoing the context.
    """
    if text is None:
        return ""
    if not text.strip():
        return text
    cached = _cached(text)
    if cached is not None:
        return cached

    masked, names = _mask_names(text)
    # Uma linha só para o modelo: diante de quebras de linha ele "conserta" a
    # quebra (junta as linhas ou põe BREAK) e devolve o português sem traduzir.
    masked = " ".join(masked.split())
    if llm_cfg is None:
        with db.session() as conn:
            llm_cfg = settings_store.effective_llm_settings(conn)

    system_prompt = _system_for(masked)
    messages = [{"role": "system", "content": system_prompt}]
    if context and context.strip():
        messages[0]["content"] = (
            system_prompt
            + "\n\nStory context, for meaning only; never translate, repeat "
            "or output it:\n"
            + " ".join(context.split())[:1800]
        )
    messages.append({"role": "user", "content": masked})

    for attempt in range(2):
        system = messages[0]["content"] if attempt == 0 else (
            messages[0]["content"] + "\n" + _RETRY_SUFFIX)
        attempt_messages = [dict(message) for message in messages]
        attempt_messages[0]["content"] = system
        try:
            with IMAGE_LLM_LOCK:
                cached = _cached(text)
                if cached is not None:
                    return cached
                reply = llm_client.chat_completion(
                    llm_cfg,
                    attempt_messages,
                    role="image_prompt", temperature=0, max_tokens=800,
                    transport=transport,
                )
        except llm_client.LlmError:
            if attempt == 1:
                if allow_passthrough and _is_probable_english(text):
                    _remember(text, text)
                    return text
                _failed()
            continue
        cleaned = _drop_stray_breaks(masked, _clean_reply(reply or ""))
        if not _accepted(masked, cleaned):
            if attempt == 1:
                if allow_passthrough and _is_probable_english(text) and "BREAK" not in masked:
                    _remember(text, text)
                    return text
                _failed()
            continue
        for index, name in enumerate(names):
            cleaned = cleaned.replace(f"[[{index}]]", name)
        _remember(text, cleaned)
        return cleaned
    if allow_passthrough and _is_probable_english(text) and "BREAK" not in masked:
        _remember(text, text)
        return text
    _failed()
