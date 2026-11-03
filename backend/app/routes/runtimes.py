"""Runtime local / hybrid / Colab.

GET /api/settings/runtime devolve a config, o catálogo, o hardware local e
a última inscrição. PUT (admin) aplica na hora a URL escolhida — inclusive
vazia, para o health fechar — e o modelo, sem mexer em sampling ou resolução.
GET /api/runtime/notebook devolve o notebook gerado. POST /api/runtime/register
(admin) só aceita os modelos já selecionados.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from app import access, auth, db, llm_client, settings_store
from app.deps import get_http_transport
from app.errors import GameError
from app.schemas import RuntimeRegistration, RuntimeSettingsUpdate

router = APIRouter()

_NOTEBOOK_NAME = "aidungeon-runtime.ipynb"


def _provisioning():
    from app import runtime_provisioning
    return runtime_provisioning


def _catalog() -> dict:
    catalog = _provisioning().load_catalog()
    if not isinstance(catalog, dict):
        raise GameError.bad_request("validation_error", "runtime catalog is unavailable")
    return settings_store.strip_secrets(catalog)


def _local_hardware() -> dict:
    try:
        hardware = _provisioning().local_hardware()
    except Exception:
        return {"status": "unavailable"}
    if not isinstance(hardware, dict):
        return {"status": "unavailable"}
    return hardware


def _runtime_response(conn) -> dict:
    return settings_store.scrub_public({
        "runtime": settings_store.effective_runtime_settings(conn),
        "catalog": _catalog(),
        "local_hardware": _local_hardware(),
        "registration": settings_store.runtime_registration(conn),
    }, conn)


def _rediscover_if_auto(conn, before: dict, transport: Any) -> None:
    after = settings_store.effective_llm_settings(conn)
    if (before.get("base_url") == after.get("base_url")
            and before.get("model_alias") == after.get("model_alias")):
        return
    if (after.get("model_alias") or "auto") != "auto":
        return
    try:
        llm_client.resolve_model(after, transport)
    except llm_client.LlmError:
        pass


@router.get("/settings/runtime")
def get_runtime_settings() -> dict:
    with db.session() as conn:
        return _runtime_response(conn)


@router.put("/settings/runtime")
def put_runtime_settings(body: RuntimeSettingsUpdate,
                         info: auth.AuthInfo = Depends(access.require_admin),
                         transport: Any = Depends(get_http_transport)) -> dict:
    update = body.model_dump(exclude_unset=True)
    with db.session() as conn:
        before = settings_store.effective_llm_settings(conn)
        settings_store.apply_runtime_update(conn, update)
        _rediscover_if_auto(conn, before, transport)
        return _runtime_response(conn)


@router.get("/runtime/notebook")
def get_runtime_notebook(info: auth.AuthInfo = Depends(access.require_admin)) -> Response:
    prov = _provisioning()
    with db.session() as conn:
        config = settings_store.effective_runtime_settings(conn)
        catalog = _catalog()
        try:
            normalized = prov.validate_runtime(config, catalog, require_colab=True)
        except ValueError as exc:
            raise GameError.bad_request("validation_error", str(exc)) from exc
        if not isinstance(normalized, dict):
            raise GameError.bad_request("validation_error", "runtime config was rejected")
        public = settings_store.scrub_public(normalized, conn)
        notebook = prov.generate_notebook(public, catalog)
        raw = prov.notebook_bytes(notebook)
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        payload = settings_store.scrub_bytes(bytes(raw), conn)
    headers = {
        "Content-Disposition": f'attachment; filename="{_NOTEBOOK_NAME}"',
    }
    return Response(content=payload, media_type="application/x-ipynb+json",
                    headers=headers)


@router.post("/runtime/register")
def post_runtime_register(body: RuntimeRegistration,
                          info: auth.AuthInfo = Depends(access.require_admin),
                          transport: Any = Depends(get_http_transport)) -> dict:
    report = body.model_dump()
    with db.session() as conn:
        before = settings_store.effective_llm_settings(conn)
        settings_store.apply_runtime_registration(conn, report)
        _rediscover_if_auto(conn, before, transport)
        return _runtime_response(conn)
