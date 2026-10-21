"""Modelos pydantic dos REQUESTS (validação → 422 conforme contrato).

As respostas são dicts produzidos pelos helpers de `app.db` (que já emitem
exatamente os campos do openapi.yaml: tags/triggers como arrays, booleans
como true/false, timestamps ISO-8601).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class LoginRequest(BaseModel):
    username: str
    password: str


class PasswordChange(BaseModel):
    current_password: str
    new_password: str


class UserCreate(BaseModel):
    username: str
    password: str


class UserPassword(BaseModel):
    password: str


class StoryCardUpsert(BaseModel):
    title: str | None = None
    type: str | None = None
    triggers: list[str] | None = None
    entry: str | None = None
    pinned: bool | None = None
    use_for_character_creation: bool | None = None
    # v0.3.0: assets do NPC (portrait_image_id é interno — definido pelo job)
    portrait_prompt: str | None = None
    voice: str | None = None
    vrm_id: str | None = None
    # v0.5.0: ficha estruturada do NPC (objeto JSON — ver docs/openapi.yaml)
    sheet: dict | None = None


class PortraitSourceRequest(BaseModel):
    source: Literal["anchor", "reference"] = "anchor"


class CardRegenerateRequest(BaseModel):
    scope: Literal["all", "field"]
    field: str | None = None
    overwrite: bool = False


class CardBulkAction(BaseModel):
    action: Literal["delete", "pin", "unpin", "retype"]
    card_ids: list[str] = Field(min_length=1)
    new_type: str | None = None  # required if action == "retype"


class ScenarioCreate(BaseModel):
    title: str
    description: str = ""
    ai_instructions: str = ""
    plot_essentials: str = ""
    authors_note: str = ""
    opening_text: str = ""
    image_style: str = ""
    tags: list[str] = Field(default_factory=list)
    # cards iniciais do rascunho do assistente (v2) — herdados pela aventura
    story_cards: list[StoryCardUpsert] = Field(default_factory=list)
    third_person: bool = False


class ScenarioUpdate(BaseModel):
    """PATCH/PUT /api/scenarios/{id} — todos os campos opcionais."""
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    ai_instructions: str | None = None
    plot_essentials: str | None = None
    authors_note: str | None = None
    opening_text: str | None = None
    image_style: str | None = None
    tags: list[str] | None = None
    story_cards: list[StoryCardUpsert] | None = None
    third_person: bool | None = None


class AdventureCreate(BaseModel):
    scenario_id: str | None = None
    title: str | None = None
    tags: list[str] | None = None
    image_style: str | None = None
    ai_instructions: str | None = None
    plot_essentials: str | None = None
    authors_note: str | None = None
    third_person: bool | None = None
    character_rules: str | None = Field(default=None, max_length=4000)


class AdventureUpdate(BaseModel):
    """PATCH /adventures/{id} — campos ausentes não são alterados."""
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    tags: list[str] | None = None
    ai_instructions: str | None = None
    plot_essentials: str | None = None
    authors_note: str | None = None
    story_summary: str | None = None
    image_style: str | None = None
    third_person: bool | None = None
    turn_mode: bool | None = None
    character_rules: str | None = Field(default=None, max_length=4000)


class CharacterSheet(BaseModel):
    """Ficha do personagem do jogador. Campos ausentes ficam de fora.

    `class` é palavra reservada: o campo Python é `class_`, o JSON é `class`.
    """
    model_config = ConfigDict(populate_by_name=True)

    appearance: str = ""
    body: str = ""
    clothing: str = ""
    status: str = ""
    stats: dict = Field(default_factory=dict)
    notes: str = ""
    class_: str = Field(default="", alias="class")


class PlayerCharacterUpsert(BaseModel):
    """PUT /adventures/{id}/character — cria ou atualiza o personagem de quem joga."""
    title: str = Field(min_length=1, max_length=80)
    sheet: CharacterSheet = Field(default_factory=CharacterSheet)
    portrait_prompt: str | None = None


class AdventureShareCreate(BaseModel):
    """POST /adventures/{id}/sharing — inclui um jogador na aventura."""
    username: str = Field(min_length=1, max_length=32)


class ActionRequest(BaseModel):
    type: Literal["do", "say", "story"]
    text: str = Field(min_length=1)
    web_search: bool = False


class ActionPatch(BaseModel):
    text: str


class MemoryUpdate(BaseModel):
    plot_essentials: str | None = None
    authors_note: str | None = None
    story_summary: str | None = None
    ai_instructions: str | None = None
    image_style: str | None = None
    third_person: bool | None = None


class MemoryCreate(BaseModel):
    """POST /api/adventures/{id}/memories — cria memória avulsa no memory bank."""
    text: str = Field(min_length=1, max_length=4000, description="Texto do fato a ser lembrado")
    seq_start: int | None = Field(default=None, description="Primeira ação coberta")
    seq_end: int | None = Field(default=None, description="Última ação coberta")


class MemoryPatch(BaseModel):
    """PATCH /api/adventures/{id}/memories/{memory_id} — campos ausentes não são alterados."""
    text: str | None = Field(default=None, min_length=1, max_length=4000, description="Novo texto do fato")
    seq_start: int | None = Field(default=None, description="Primeira ação coberta")
    seq_end: int | None = Field(default=None, description="Última ação coberta")


class MemoryRegenerateResponse(BaseModel):
    """Resposta de POST /api/adventures/{id}/memories/regenerate."""
    status: Literal["regenerated"] = "regenerated"
    count: int


class LlmSettingsUpdate(BaseModel):
    """Todos os campos opcionais; omitidos permanecem (api_key="" remove a chave)."""
    base_url: str | None = None
    api_key: str | None = None
    model_alias: str | None = None
    context_size: int | None = Field(default=None, ge=4096, le=262144)
    max_tokens: int | None = Field(default=None, ge=100, le=2048)
    streaming: bool | None = None
    disable_thinking: bool | None = None
    timeout_seconds: int | None = None
    # sampler (persistidos como overrides sampler.*)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    min_p: float | None = Field(default=None, ge=0.0, le=1.0)
    repetition_penalty: float | None = Field(default=None, ge=1.0, le=2.0)
    presence_penalty: float | None = Field(default=None, ge=-2.0, le=2.0)
    frequency_penalty: float | None = Field(default=None, ge=-2.0, le=2.0)
    dry_multiplier: float | None = Field(default=None, ge=0.0, le=5.0)
    dry_base: float | None = Field(default=None, ge=1.0, le=3.0)
    dry_allowed_length: int | None = Field(default=None, ge=1, le=10)
    dry_penalty_last_n: int | None = Field(default=None)
    retry_temperature_step: float | None = Field(default=None, ge=0.0, le=0.5)
    max_retry_temperature: float | None = Field(default=None, ge=0.0, le=2.0)


# ------------------------------------------------------------------ v2: see
class SeeRequest(BaseModel):
    action_seq: int
    extra_prompt: str = ""


# ------------------------------------------------------------- v2: settings
class ImageSettingsUpdate(BaseModel):
    """Todos os campos opcionais; omitidos permanecem (openai_api_key="" remove)."""
    enabled: bool | None = None
    provider: Literal["sdwebui", "openai_compatible"] | None = None
    sdwebui_base_url: str | None = None
    sdwebui_steps: int | None = Field(default=None, ge=1, le=150)
    sdwebui_width: int | None = Field(default=None, ge=64, le=4096)
    sdwebui_height: int | None = Field(default=None, ge=64, le=4096)
    negative_prompt: str | None = None
    openai_base_url: str | None = None
    openai_api_key: str | None = None
    openai_model: str | None = None
    openai_size: str | None = None
    style_suffix: str | None = None


# ------------------------------------------------------------------ v4: voice
class SceneSyncRequest(BaseModel):
    """POST /api/adventures/{id}/scene — retry força outra pintura do fundo."""
    retry: bool = False


class TtsRequest(BaseModel):
    """POST /api/tts — voice omitida (null) usa narrator_voice da config.

    `emotion` é uma dica (joy, anger, fear, sadness, tenderness, surprise,
    disgust, neutral). A linha vence; sem palavra de emoção, vale a ficha
    do NPC citado quando `adventure_id` vem junto.
    """
    text: str
    voice: str | None = None
    emotion: str | None = None
    adventure_id: str | None = None


class VoiceSettingsUpdate(BaseModel):
    """Todos os campos opcionais; omitidos permanecem (api_key="" remove a chave)."""
    enabled: bool | None = None
    base_url: str | None = None
    tts_model: str | None = None
    stt_model: str | None = None
    narrator_voice: str | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=600)
    api_key: str | None = None


# ------------------------------------------------------------ v2/v9: assistant
class AssistantScenarioRequest(BaseModel):
    brief: str = Field(min_length=3)
    language: str = "en"
    genre: str | None = None
    tone: str | None = None
    pov: Literal["second", "third", "first"] | None = None
    protagonist_name: str | None = None
    protagonist_role: str | None = None
    protagonist_appearance: str | None = None
    protagonist_description: str | None = None
    ruleset_name: str | None = None
    ruleset_rules: str | None = None
    image_style: str | None = None
    card_count: int | None = Field(default=None, ge=3, le=8)
    web_search_mode: Literal["auto", "always", "off"] = "auto"


class AssistantGenerateRequest(BaseModel):
    brief: str = Field(min_length=3)
    current: dict | None = None
    language: str = "en"
    genre: str = ""
    tone: str = ""
    pov: Literal["second", "third", "first"] | None = None
    protagonist_name: str = ""
    protagonist_role: str = ""
    protagonist_appearance: str = ""
    protagonist_description: str = ""
    ruleset_name: str = ""
    ruleset_rules: str = ""
    image_style: str = ""
    card_count: int = Field(default=5, ge=3, le=8)
    web_search_mode: Literal["auto", "always", "off"] = "auto"
    deep_characters: bool = True


class AssistantFieldRequest(BaseModel):
    field: Literal["title", "description", "ai_instructions", "plot_essentials",
                   "authors_note", "opening_text", "image_style", "tags", "story_cards"]
    brief: str | None = None
    current: str | None = None
    draft: dict | None = None
    instruction: str | None = None
    web_search: bool = False
    language: str = "en"


class AssistantCardRequest(BaseModel):
    concept: str = Field(min_length=2)
    card_type: str = "character"
    adventure_id: str | None = None
    draft: dict | None = None
    current_card: dict | None = None
    instruction: str | None = None
    web_search: bool = False
    language: str = "pt"


class AssistantSyncPlotRequest(BaseModel):
    adventure_id: str
    instruction: str | None = None
    language: str = "pt"


class WebSearchRequest(BaseModel):
    query: str = Field(min_length=2)
    language: str = "pt"
    max_results: int = Field(default=5, ge=1, le=10)


class EmbeddingsSettingsUpdate(BaseModel):
    enabled: bool | None = None
    base_url: str | None = None
    model: str | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=600)


class ScriptingSettingsUpdate(BaseModel):
    input_modifier: str | None = None
    context_modifier: str | None = None
    output_modifier: str | None = None


class RulesetUpsert(BaseModel):
    """POST exige name e rules (a rota recusa ausência). PUT aceita qualquer subconjunto."""
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)
    rules: str | None = Field(default=None, min_length=1, max_length=4000)


class RulesetApply(BaseModel):
    """POST /adventures/{id}/rulesets/apply — copia rules para character_rules."""
    ruleset_id: str = Field(min_length=1)
    mode: Literal["replace", "append"] = "replace"


