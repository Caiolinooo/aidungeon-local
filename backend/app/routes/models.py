"""Biblioteca dinâmica de modelos (admin).

Busca nos catálogos do CivitAI, do CivitAI Red e do Hugging Face, guarda o
arquivo escolhido como entrada do catálogo do runtime e baixa para o ComfyUI
desta máquina. O Colab baixa pelo notebook, com as chaves salvas aqui.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app import access, auth, db, model_library
from app.deps import get_http_transport

router = APIRouter(dependencies=[Depends(access.require_admin)])

Kind = Literal["llm", "image", "video"]


class ModelFile(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    url: str = Field(min_length=12, max_length=2000)
    sha256: str = Field(min_length=64, max_length=64)
    bytes: int | None = None
    folder: str | None = Field(default=None, max_length=40)


class ModelNode(BaseModel):
    git_url: str = Field(max_length=300)
    git_ref: str = Field(max_length=80)


class ModelAdd(BaseModel):
    kind: Kind
    family: str = Field(max_length=40)
    label: str = Field(default="", max_length=120)
    base_model: str = Field(default="", max_length=80)
    page_url: str = Field(default="", max_length=300)
    file: ModelFile | None = None
    # Só no fluxo próprio: o grafo, os arquivos com a pasta e os nodes extras.
    graph: dict[str, Any] | None = None
    files: list[ModelFile] = Field(default_factory=list, max_length=12)
    nodes: list[ModelNode] = Field(default_factory=list, max_length=8)


class LoraAdd(BaseModel):
    file: ModelFile
    strength: float = 0.8


class ModelUpdate(BaseModel):
    label: str | None = Field(default=None, max_length=120)
    loras: list[dict[str, Any]] | None = None
    checkpoint: dict[str, Any] | None = None
    graph: dict[str, Any] | None = None


class DownloadKeys(BaseModel):
    """String vazia apaga a chave. Campo omitido fica como está."""
    civitai: str | None = Field(default=None, max_length=200)
    huggingface: str | None = Field(default=None, max_length=200)


class InstallRequest(BaseModel):
    kind: Literal["image", "video"]
    id: str = Field(min_length=1, max_length=200)


def _state(conn) -> dict:
    keys = model_library.download_keys(conn)
    root = model_library.comfy_root(conn)
    return {
        "families": model_library.FAMILIES,
        "sources": list(model_library.SOURCES),
        "keys": {"civitai": bool(keys["CIVITAI_API_KEY"]),
                 "huggingface": bool(keys["HF_TOKEN"])},
        "comfyui_root": str(root) if root else None,
        "install": model_library.install_status(),
    }


@router.get("/models/library")
def get_library() -> dict:
    with db.session() as conn:
        return _state(conn)


@router.get("/models/search")
def search_models(source: str, kind: str, q: str,
                  transport: Any = Depends(get_http_transport)) -> dict:
    with db.session() as conn:
        return {"results": model_library.search(conn, source, kind, q, transport=transport)}


@router.get("/models/files")
def repo_files(repo: str, transport: Any = Depends(get_http_transport)) -> dict:
    with db.session() as conn:
        return {"files": model_library.huggingface_files(conn, repo, transport=transport)}


@router.post("/models/library")
def add_model(body: ModelAdd) -> dict:
    with db.session() as conn:
        model_id = model_library.add_model(
            conn, body.kind, body.family, body.model_dump())
        return {"id": model_id}


@router.patch("/models/library/{kind}/{model_id}")
def update_model(kind: Kind, model_id: str, body: ModelUpdate) -> dict:
    with db.session() as conn:
        return {"id": model_library.update_model(
            conn, kind, model_id, body.model_dump(exclude_unset=True))}


@router.delete("/models/library/{kind}/{model_id}")
def delete_model(kind: Kind, model_id: str) -> dict:
    with db.session() as conn:
        model_library.delete_model(conn, kind, model_id)
        return {"ok": True}


@router.post("/models/library/image/{model_id}/loras")
def add_lora(model_id: str, body: LoraAdd) -> dict:
    with db.session() as conn:
        return {"id": model_library.add_lora(conn, model_id, body.model_dump())}


@router.put("/models/keys")
def put_keys(body: DownloadKeys) -> dict:
    with db.session() as conn:
        model_library.save_keys(conn, body.civitai, body.huggingface)
        return _state(conn)


@router.post("/models/install")
def install(body: InstallRequest) -> dict:
    with db.session() as conn:
        return model_library.start_install(conn, body.kind, body.id)


@router.get("/models/install")
def install_state() -> dict:
    return model_library.install_status()
