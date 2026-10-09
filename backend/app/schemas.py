"""Modelos pydantic dos REQUESTS (validação → 422 conforme contrato).

As respostas são dicts produzidos pelos helpers de `app.db` (que já emitem
exatamente os campos do openapi.yaml: tags/triggers como arrays, booleans
como true/false, timestamps ISO-8601).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    username: str
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


class ScenarioCreate(BaseModel):
    title: str
    description: str = ""
    ai_instructions: str = ""
    plot_essentials: str = ""
    authors_note: str = ""
    opening_text: str = ""
    tags: list[str] = Field(default_factory=list)
    # cards iniciais do rascunho do assistente (v2) — herdados pela aventura
    story_cards: list[StoryCardUpsert] = Field(default_factory=list)
    third_person: bool = False


class AdventureCreate(BaseModel):
    scenario_id: str | None = None
    title: str | None = None
    tags: list[str] | None = None


class AdventureUpdate(BaseModel):
    """PATCH /adventures/{id} — campos ausentes não são alterados."""
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    tags: list[str] | None = None


class ActionRequest(BaseModel):
    type: Literal["do", "say", "story"]
    text: str = Field(min_length=1)


class ActionPatch(BaseModel):
    text: str


class MemoryUpdate(BaseModel):
    plot_essentials: str | None = None
    authors_note: str | None = None


class LlmSettingsUpdate(BaseModel):
    """Todos os campos opcionais; omitidos permanecem (api_key="" remove a chave)."""
    base_url: str | None = None
    api_key: str | None = None
    model_alias: str | None = None
    context_size: int | None = Field(default=None, ge=4096, le=131072)
    max_tokens: int | None = Field(default=None, ge=100, le=600)
    streaming: bool | None = None
    disable_thinking: bool | None = None
    timeout_seconds: int | None = None
    # sampler (persistidos como overrides sampler.*)
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    top_p: float | None = Field(default=None, ge=0.0, le=1.0)
    repetition_penalty: float | None = Field(default=None, ge=1.0, le=2.0)


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
    """POST /api/tts — voice omitida (null) usa narrator_voice da config."""
    text: str
    voice: str | None = None


class VoiceSettingsUpdate(BaseModel):
    """Todos os campos opcionais; omitidos permanecem (api_key="" remove a chave)."""
    enabled: bool | None = None
    base_url: str | None = None
    tts_model: str | None = None
    stt_model: str | None = None
    narrator_voice: str | None = None
    timeout_seconds: int | None = Field(default=None, ge=1, le=600)
    api_key: str | None = None


# ------------------------------------------------------------ v2: assistant
class AssistantScenarioRequest(BaseModel):
    brief: str = Field(min_length=3)
    language: str = "en"


class AssistantFieldRequest(BaseModel):
    field: Literal["title", "description", "ai_instructions", "plot_essentials",
                   "authors_note", "opening_text", "tags", "story_cards"]
    brief: str | None = None
    current: str | None = None
    draft: dict | None = None
