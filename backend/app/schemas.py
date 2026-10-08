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


class ScenarioCreate(BaseModel):
    title: str
    description: str = ""
    ai_instructions: str = ""
    plot_essentials: str = ""
    authors_note: str = ""
    opening_text: str = ""
    tags: list[str] = Field(default_factory=list)
    third_person: bool = False


class AdventureCreate(BaseModel):
    scenario_id: str | None = None
    title: str | None = None
    tags: list[str] | None = None


class ActionRequest(BaseModel):
    type: Literal["do", "say", "story"]
    text: str = Field(min_length=1)


class ActionPatch(BaseModel):
    text: str


class StoryCardUpsert(BaseModel):
    title: str | None = None
    type: str | None = None
    triggers: list[str] | None = None
    entry: str | None = None
    pinned: bool | None = None
    use_for_character_creation: bool | None = None


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
