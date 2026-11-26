"""Runtime local / hybrid / Colab.

GET /api/settings/runtime devolve a config, o catálogo, o hardware local e
a última inscrição. PUT (admin) aplica na hora a URL escolhida — inclusive
vazia, para o health fechar — e o modelo, sem mexer em sampling ou resolução.
GET /api/runtime/notebook devolve o notebook gerado, com o token de registro
dentro. POST /api/runtime/register (admin ou esse token) só aceita os modelos
já selecionados.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app import access, auth, colab_api, db, llm_client, notify, settings_store
from app.deps import get_http_transport
from app.errors import GameError
from app.schemas import RuntimeRegistration, RuntimeSettingsUpdate

router = APIRouter()
# Fora do Bearer de sessão do main: o Colab se registra com o token do notebook.
register_router = APIRouter()

_NOTEBOOK_NAME = "aidungeon-runtime.ipynb"


def _provisioning():
    from app import runtime_provisioning
    return runtime_provisioning


def _catalog(conn) -> dict:
    catalog = settings_store.runtime_catalog(conn)
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
        "catalog": _catalog(conn),
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
def get_runtime_notebook(project_url: str = "",
                         info: auth.AuthInfo = Depends(access.require_admin)) -> Response:
    prov = _provisioning()
    with db.session() as conn:
        raw = prov.notebook_bytes(colab_api.runtime_notebook(conn, project_url))
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        payload = settings_store.scrub_bytes(bytes(raw), conn)
    headers = {
        "Content-Disposition": f'attachment; filename="{_NOTEBOOK_NAME}"',
    }
    return Response(content=payload, media_type="application/x-ipynb+json",
                    headers=headers)


class ColabClient(BaseModel):
    """String vazia apaga. Campo omitido fica como está."""
    client_id: str | None = Field(default=None, max_length=200)
    client_secret: str | None = Field(default=None, max_length=200)


class ColabAuth(BaseModel):
    pasted: str = Field(min_length=4, max_length=2000)


class ColabStart(BaseModel):
    variant: str = Field(max_length=40)
    accelerator: str = Field(max_length=40)
    shape: str = Field(max_length=40)
    project_url: str = Field(default="", max_length=500)


class ColabStop(BaseModel):
    name: str = Field(default="", max_length=120)


@router.get("/colab")
def get_colab(info: auth.AuthInfo = Depends(access.require_admin),
              transport: Any = Depends(get_http_transport)) -> dict:
    with db.session() as conn:
        return colab_api.overview(conn, transport=transport)


@router.put("/colab/client")
def put_colab_client(body: ColabClient,
                     info: auth.AuthInfo = Depends(access.require_admin),
                     transport: Any = Depends(get_http_transport)) -> dict:
    with db.session() as conn:
        colab_api.save_client(conn, body.client_id, body.client_secret)
        return colab_api.overview(conn, transport=transport)


@router.get("/colab/auth-url")
def get_colab_auth_url(info: auth.AuthInfo = Depends(access.require_admin)) -> dict:
    with db.session() as conn:
        return {"url": colab_api.auth_url(conn), "redirect": colab_api.REDIRECT}


@router.post("/colab/auth")
def post_colab_auth(body: ColabAuth,
                    info: auth.AuthInfo = Depends(access.require_admin),
                    transport: Any = Depends(get_http_transport)) -> dict:
    with db.session() as conn:
        colab_api.finish_auth(conn, body.pasted, transport=transport)
        return colab_api.overview(conn, transport=transport)


@router.delete("/colab/auth")
def delete_colab_auth(info: auth.AuthInfo = Depends(access.require_admin)) -> dict:
    with db.session() as conn:
        colab_api.disconnect(conn)
        return colab_api.overview(conn)


@router.post("/colab/start")
def post_colab_start(body: ColabStart,
                     info: auth.AuthInfo = Depends(access.require_admin),
                     transport: Any = Depends(get_http_transport)) -> dict:
    spec = {"variant": body.variant, "accelerator": body.accelerator, "shape": body.shape}
    with db.session() as conn:
        return colab_api.start(conn, spec, body.project_url, transport=transport)


@router.post("/colab/stop")
def post_colab_stop(body: ColabStop,
                    info: auth.AuthInfo = Depends(access.require_admin),
                    transport: Any = Depends(get_http_transport)) -> dict:
    with db.session() as conn:
        return colab_api.stop(conn, body.name, transport=transport)


class NotifyTopic(BaseModel):
    """Vazio desliga os avisos. `generate` sorteia um tópico novo."""
    topic: str = Field(default="", max_length=64)
    generate: bool = False


@router.put("/colab/notify")
def put_colab_notify(body: NotifyTopic,
                     info: auth.AuthInfo = Depends(access.require_admin)) -> dict:
    with db.session() as conn:
        topic = notify.set_topic(conn, notify.new_topic() if body.generate else body.topic)
        return {"notify_topic": topic}


@router.post("/colab/notify/test")
def post_colab_notify_test(info: auth.AuthInfo = Depends(access.require_admin),
                           transport: Any = Depends(get_http_transport)) -> dict:
    with db.session() as conn:
        sent = notify.send("Aviso de teste: é aqui que o jogo avisa quando o Colab liga.",
                           tags="bell", transport=transport, conn=conn)
    if not sent:
        raise GameError.bad_request(
            "notify_failed", "o ntfy não aceitou o aviso; confira o tópico e a internet do servidor")
    return {"ok": True}


@router.get("/colab/status")
def get_colab_status(info: auth.AuthInfo = Depends(access.require_admin)) -> dict:
    with db.session() as conn:
        return colab_api.session_view(conn)


def _require_registrar(request: Request) -> None:
    """Admin logado ou o token de registro que vai dentro do notebook."""
    cfg = auth.auth_config()
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if cfg and scheme.lower() == "bearer" and auth.runtime_token_ok(token, cfg["secret"]):
        return
    access.require_admin(auth.require_auth(request))


@register_router.post("/runtime/register", dependencies=[Depends(_require_registrar)])
def post_runtime_register(body: RuntimeRegistration,
                          transport: Any = Depends(get_http_transport)) -> dict:
    report = body.model_dump()
    with db.session() as conn:
        before = settings_store.effective_llm_settings(conn)
        settings_store.apply_runtime_registration(conn, report)
        _rediscover_if_auto(conn, before, transport)
        return _runtime_response(conn)
