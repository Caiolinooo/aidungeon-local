"""Colab ligado direto pelo jogo, pela API oficial (colaboratory.googleapis.com).

O admin conecta a conta Google uma vez. Daí em diante o jogo lista as GPUs que
a conta pode pedir, cria a sessão, roda nela as células do mesmo notebook que
o botão de download gera e acompanha a saída. Quem liga o jogo à GPU continua
sendo o notebook: túnel, registro e batimento.

A API só cria e apaga a sessão. Executar código é pelo Jupyter que ela
devolve em `connectionInfo`, com o token no cabeçalho
`X-Colab-Runtime-Proxy-Token`.
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
import urllib.parse
import uuid
from typing import Any

import httpx

from app import auth, db, model_library, settings_store
from app.errors import GameError

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://colaboratory.googleapis.com"
SCOPE = "https://www.googleapis.com/auth/colaboratory"
# Cliente "app para computador": o Google devolve o código para o navegador de
# quem autorizou, em localhost. O jogo roda em outra máquina, então a página
# não abre e o admin cola aqui o endereço em que ela parou.
REDIRECT = "http://localhost:8765"
PROXY_HEADER = "X-Colab-Runtime-Proxy-Token"
_TIMEOUT = 30.0
_LOG_LINES = 60

_lock = threading.Lock()
_state: dict = {"state": "off"}
_token_cache: dict = {}


# ------------------------------------------------------------- credenciais
def _secret(conn: sqlite3.Connection, key: str) -> str:
    return settings_store._usable_secret(settings_store._get_override(conn, key)) or ""


def save_client(conn: sqlite3.Connection, client_id: str | None, client_secret: str | None) -> None:
    settings_store._apply_key(conn, "colab.client_id", client_id)
    settings_store._apply_key(conn, "colab.client_secret", client_secret)
    _token_cache.clear()


def disconnect(conn: sqlite3.Connection) -> None:
    db.delete_setting(conn, "colab.refresh_token")
    _token_cache.clear()


def auth_url(conn: sqlite3.Connection) -> str:
    client_id = _secret(conn, "colab.client_id")
    if not client_id or not _secret(conn, "colab.client_secret"):
        raise GameError.bad_request(
            "colab_client_missing", "salve antes o client ID e o client secret do Google")
    state = secrets.token_urlsafe(16)
    settings_store._set_override(conn, "colab.oauth_state", state)
    return AUTH_URL + "?" + urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": REDIRECT, "response_type": "code",
        "scope": SCOPE, "access_type": "offline", "prompt": "consent", "state": state,
    })


def _token_request(data: dict, transport: Any) -> dict:
    try:
        with httpx.Client(transport=transport, timeout=_TIMEOUT) as client:
            resp = client.post(TOKEN_URL, data=data)
    except httpx.HTTPError as exc:
        raise GameError(502, "google_unreachable",
                        f"o Google não respondeu: {type(exc).__name__}") from exc
    if resp.status_code != 200:
        raise GameError.bad_request(
            "google_auth_failed", "o Google recusou a autorização; conecte a conta de novo")
    return resp.json()


def finish_auth(conn: sqlite3.Connection, pasted: str, *, transport: Any = None) -> None:
    """Recebe o endereço (ou só o código) em que o navegador parou depois do consentimento."""
    text = (pasted or "").strip()
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(text).query) if "code=" in text else {}
    code = query.get("code", [text])[0]
    expected = settings_store._get_override(conn, "colab.oauth_state")
    if query and query.get("state", [""])[0] != expected:
        raise GameError.bad_request(
            "google_auth_failed", "este endereço é de outra tentativa; gere o link de novo")
    if not code:
        raise GameError.bad_request("google_auth_failed", "cole o endereço completo da página")
    tokens = _token_request({
        "code": code, "client_id": _secret(conn, "colab.client_id"),
        "client_secret": _secret(conn, "colab.client_secret"),
        "redirect_uri": REDIRECT, "grant_type": "authorization_code",
    }, transport)
    if not tokens.get("refresh_token"):
        raise GameError.bad_request(
            "google_auth_failed", "o Google não devolveu um token permanente; gere o link de novo")
    save_refresh_token(conn, tokens["refresh_token"])


def save_refresh_token(conn: sqlite3.Connection, refresh_token: str) -> None:
    settings_store._apply_key(conn, "colab.refresh_token", refresh_token)
    db.delete_setting(conn, "colab.oauth_state")
    _token_cache.clear()


def _access_token(conn: sqlite3.Connection, transport: Any) -> str:
    refresh = _secret(conn, "colab.refresh_token")
    if not refresh:
        raise GameError.bad_request("colab_not_connected", "conecte a conta Google primeiro")
    if _token_cache.get("refresh") == refresh and _token_cache.get("until", 0) > time.time() + 60:
        return _token_cache["token"]
    tokens = _token_request({
        "refresh_token": refresh, "client_id": _secret(conn, "colab.client_id"),
        "client_secret": _secret(conn, "colab.client_secret"), "grant_type": "refresh_token",
    }, transport)
    _token_cache.update(refresh=refresh, token=tokens["access_token"],
                        until=time.time() + int(tokens.get("expires_in") or 3000))
    return tokens["access_token"]


# -------------------------------------------------------------------- API
def _call(conn: sqlite3.Connection, method: str, path: str, *, body: Any = None,
          params: dict | None = None, transport: Any = None) -> dict:
    token = _access_token(conn, transport)
    try:
        with httpx.Client(transport=transport, timeout=_TIMEOUT) as client:
            resp = client.request(method, f"{API}/{path}", json=body, params=params,
                                  headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        raise GameError(502, "colab_unreachable",
                        f"a API do Colab não respondeu: {type(exc).__name__}") from exc
    if resp.status_code >= 400:
        try:
            message = str((resp.json().get("error") or {}).get("message") or "")
        except ValueError:
            message = ""
        # A mensagem do Google já diz o que falta (API desativada, sem cota, sem plano).
        raise GameError(502, "colab_api_error",
                        message[:400] or f"a API do Colab respondeu HTTP {resp.status_code}")
    return resp.json() if resp.content else {}


def overview(conn: sqlite3.Connection, *, transport: Any = None) -> dict:
    """O que a tela mostra: conta conectada, plano, GPUs que dá para pedir e a sessão."""
    session = status()
    if session.get("state") == "starting" and settings_store.colab_online(conn):
        # O acompanhamento pode ter caído; o batimento do notebook é a prova de que ligou.
        session["state"] = "on"
    out = {
        "client_saved": bool(_secret(conn, "colab.client_id")
                             and _secret(conn, "colab.client_secret")),
        "connected": bool(_secret(conn, "colab.refresh_token")),
        "redirect": REDIRECT,
        "session": session,
        "tier": None, "specs": [], "runtimes": [], "error": None,
    }
    if not out["connected"]:
        return out
    try:
        out["tier"] = _call(conn, "GET", "v1beta/subscription", transport=transport).get("tier")
        specs = _call(conn, "GET", "v1beta/runtimespecs", transport=transport)
        out["specs"] = [
            {**(spec.get("key") or {}), "eligible": bool(spec.get("eligible"))}
            for spec in specs.get("runtimeSpecs") or []
        ]
        runtimes = _call(conn, "GET", "v1beta/runtimes", transport=transport)
        out["runtimes"] = [
            {"name": item.get("name"), **(item.get("runtimeSpec") or {})}
            for item in runtimes.get("runtimes") or []
        ]
    except GameError as exc:
        out["error"] = exc.message
    return out


# ------------------------------------------------------- notebook do runtime
def runtime_notebook(conn: sqlite3.Connection, project_url: str) -> dict:
    """O notebook do runtime com o token de registro e as chaves de download.

    É o mesmo do botão de download: o que roda pela API e o que o admin baixa
    não podem divergir.
    """
    prov = settings_store._provisioning()
    config = settings_store.effective_runtime_settings(conn)
    if not config.get("project_url"):
        # Sem URL salva, o Colab chama de volta o endereço de onde o admin está.
        config = {**config, "project_url": project_url}
    catalog = settings_store.strip_secrets(settings_store.runtime_catalog(conn))
    try:
        normalized = prov.validate_runtime(config, catalog, require_colab=True)
    except ValueError as exc:
        raise GameError.bad_request("validation_error", str(exc)) from exc
    if not isinstance(normalized, dict):
        raise GameError.bad_request("validation_error", "runtime config was rejected")
    public = settings_store.scrub_public(normalized, conn)
    if not public.get("project_url"):
        raise GameError.bad_request(
            "validation_error", "project_url is required so Colab can reach this server")
    # As chaves de download entram depois da limpeza: o notebook precisa delas.
    return prov.generate_notebook(
        public, catalog,
        register_token=auth.make_runtime_token(auth.auth_config()["secret"]),
        download_keys=model_library.download_keys(conn))


# -------------------------------------------------------------- execução
def status() -> dict:
    with _lock:
        return {key: value for key, value in _state.items() if not key.startswith("_")}


def _set(**fields: Any) -> None:
    with _lock:
        _state.update(fields)


def _log(text: str) -> None:
    lines = [line for line in str(text).replace("\r", "\n").split("\n") if line.strip()]
    with _lock:
        _state["log"] = (_state.get("log", []) + lines)[-_LOG_LINES:]


def _frame(session: str, kind: str, content: dict) -> tuple[str, str]:
    msg_id = uuid.uuid4().hex
    return msg_id, json.dumps({
        "header": {"msg_id": msg_id, "username": "aidungeon", "session": session,
                   "msg_type": kind, "version": "5.3"},
        "parent_header": {}, "metadata": {}, "channel": "shell", "content": content,
    })


def _execute(connection: Any, cells: list[tuple[str, str]], *, connect: Any = None,
             transport: Any = None) -> None:
    """Cria um kernel, enfileira as células e acompanha a saída.

    `connection()` devolve `(url, token)` atuais: o token do proxy vence em
    cerca de uma hora e cada reconexão pede um novo. O kernel roda a fila
    sozinho e para na primeira célula que falhar, então uma conexão que cai
    só interrompe o acompanhamento; ela é reaberta no mesmo kernel.
    """
    if connect is None:
        from websockets.sync.client import connect
    url, token = connection()
    base = url.rstrip("/")
    with httpx.Client(transport=transport, timeout=60.0,
                      headers={PROXY_HEADER: token}) as client:
        resp = client.post(f"{base}/api/kernels", json={})
        if resp.status_code >= 400:
            raise RuntimeError(f"o Jupyter do Colab recusou o kernel: HTTP {resp.status_code}")
        kernel = resp.json()["id"]
    session = uuid.uuid4().hex
    order = [role for role, _source in cells]
    pending: dict[str, str] = {}
    queued = False
    drops = 0
    while True:
        # Sem `session_id` o Jupyter do Colab aceita a conexão e não entrega nada.
        ws_url = ("ws" + base[4:] + f"/api/kernels/{kernel}/channels?"
                  + urllib.parse.urlencode({"session_id": session}))
        try:
            # ping_interval=None: o proxy não responde ao ping e a conexão cairia em 40 s.
            with connect(ws_url, additional_headers={PROXY_HEADER: token}, max_size=None,
                         open_timeout=60, ping_interval=None) as ws:
                if not queued:
                    # O que sai antes de o kernel estar pronto não chega aqui:
                    # espera a primeira resposta para não perder o começo do log.
                    ws.send(_frame(session, "kernel_info_request", {})[1])
                    deadline = time.time() + 90
                    while time.time() < deadline:
                        try:
                            first = json.loads(ws.recv(timeout=5))
                        except TimeoutError:
                            continue
                        if first.get("msg_type") in ("kernel_info_reply", "status"):
                            break
                    for role, source in cells:
                        msg_id, frame = _frame(session, "execute_request", {
                            "code": source, "silent": False, "store_history": False,
                            "user_expressions": {}, "allow_stdin": False, "stop_on_error": True})
                        pending[msg_id] = role
                        ws.send(frame)
                    queued = True
                for raw in ws:
                    msg = json.loads(raw)
                    role = pending.get((msg.get("parent_header") or {}).get("msg_id"))
                    if role is None:
                        continue
                    drops = 0
                    kind, content = msg.get("msg_type"), msg.get("content") or {}
                    if kind == "status" and content.get("execution_state") == "busy":
                        # A última célula segura a sessão e nunca termina: chegar nela é estar ligado.
                        _set(state="on" if role == order[-1] else "starting", cell=role,
                             step=order.index(role) + 1, steps=len(order))
                    elif kind == "stream":
                        _log(content.get("text", ""))
                    elif kind == "error":
                        _log(f"{content.get('ename')}: {content.get('evalue')}")
                        raise RuntimeError(f"a célula {role} falhou: {content.get('evalue')}")
                    elif kind == "execute_reply" and content.get("status") == "aborted":
                        raise RuntimeError(f"a célula {role} não rodou")
        except RuntimeError:
            raise
        except Exception:  # queda de rede ou do proxy: o kernel segue com a fila
            pass
        drops += 1
        if drops > 20 or status().get("state") not in ("creating", "starting", "on"):
            return
        time.sleep(min(30, 3 * drops))
        url, token = connection()
        base = url.rstrip("/")


def _failure(op: dict) -> str:
    error = op.get("error") or {}
    reasons = {str(item.get("reason")) for item in error.get("details") or []}
    if "NO_RUNTIMES" in reasons:
        return ("o Colab não tem máquina livre com essa GPU agora; "
                "tente de novo em alguns minutos ou escolha outra")
    return str(error.get("message") or "o Colab recusou a sessão")[:300]


def _run(spec: dict, cells: list[tuple[str, str]], transport: Any, connect: Any) -> None:
    try:
        with db.session() as conn:
            op = _call(conn, "POST", "v1beta/runtimes", body={"runtimeSpec": spec},
                       params={"requestId": str(uuid.uuid4())}, transport=transport)
            deadline = time.time() + 600
            while not op.get("done"):
                if time.time() > deadline:
                    raise RuntimeError("o Colab não entregou a sessão em 10 minutos")
                time.sleep(3)
                op = _call(conn, "GET", f"v1/{op['name']}", transport=transport)
        if op.get("error"):
            raise RuntimeError(_failure(op))
        name = (op.get("response") or {}).get("name") or ""
        _set(runtime=name, state="starting", cell="", step=0, steps=len(cells))

        def connection() -> tuple[str, str]:
            with db.session() as conn:
                info = _call(conn, "GET", f"v1beta/{name}",
                             transport=transport).get("connectionInfo") or {}
            if not info.get("url") or not info.get("token"):
                raise RuntimeError("o Colab criou a sessão sem endereço de conexão")
            return info["url"], info["token"]

        _execute(connection, cells, connect=connect, transport=transport)
    except Exception as exc:  # a thread não tem quem receba a exceção
        _set(state="error", error=str(getattr(exc, "message", exc))[:400])


def start(conn: sqlite3.Connection, spec: dict, project_url: str,
          *, transport: Any = None, connect: Any = None) -> dict:
    """Cria a sessão com a GPU pedida e roda nela as células do notebook."""
    notebook = runtime_notebook(conn, project_url)
    cells = [(cell["id"], "".join(cell["source"]))
             for cell in notebook["cells"] if cell["cell_type"] == "code"]
    _access_token(conn, transport)
    with _lock:
        if _state.get("state") in ("creating", "starting", "on"):
            raise GameError.conflict("colab_running", "o Colab já está ligado ou ligando")
        _state.clear()
        _state.update({"state": "creating", "spec": spec, "log": []})
    threading.Thread(target=_run, daemon=True, args=(spec, cells, transport, connect)).start()
    return status()


def stop(conn: sqlite3.Connection, name: str = "", *, transport: Any = None) -> dict:
    """Apaga a sessão. O batimento para e a máquina local assume.

    `name` serve para a sessão que ficou de antes de o jogo reiniciar: o estado
    daqui é só memória, mas a API lista o que continua ligado na conta.
    """
    name = name or status().get("runtime") or ""
    if name and not name.startswith("runtimes/"):
        raise GameError.bad_request("validation_error", "sessão inválida")
    if name:
        _call(conn, "DELETE", f"v1beta/{name}", params={"allowMissing": "true"},
              transport=transport)
    with _lock:
        _state.clear()
        _state.update({"state": "off"})
    return status()
