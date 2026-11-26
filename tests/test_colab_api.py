"""Colab ligado pela API: conta Google, GPUs, sessão e execução das células."""

from __future__ import annotations

import json
import time
import urllib.parse

import httpx
import pytest

from app import colab_api, db, deps


@pytest.fixture()
def admin(client, admin_token):
    client.headers["Authorization"] = f"Bearer {admin_token}"
    colab_api._state.clear()
    colab_api._state.update({"state": "off"})
    colab_api._token_cache.clear()
    yield client
    colab_api._state.clear()
    colab_api._state.update({"state": "off"})


class Google:
    """Google de mentira: troca de token, API do Colab e o Jupyter da sessão."""

    def __init__(self, create_error=None):
        self.calls = []
        self.create_error = create_error

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append((request.method, request.url.host, path))
        if request.url.host == "oauth2.googleapis.com":
            form = dict(urllib.parse.parse_qsl(request.content.decode()))
            if form["grant_type"] == "authorization_code":
                assert form["code"] == "the-code" and form["redirect_uri"] == colab_api.REDIRECT
                return httpx.Response(200, json={"access_token": "at", "refresh_token": "rt-123456",
                                                 "expires_in": 3600})
            assert form["refresh_token"] == "rt-123456"
            return httpx.Response(200, json={"access_token": "at", "expires_in": 3600})
        if request.url.host == "jupyter.example":
            assert request.headers[colab_api.PROXY_HEADER] == "proxy-token"
            return httpx.Response(201, json={"id": "kernel-1"})
        assert request.headers["Authorization"] == "Bearer at"
        if path == "/v1beta/subscription":
            return httpx.Response(200, json={"tier": "SUBSCRIPTION_TIER_PRO"})
        if path == "/v1beta/runtimespecs":
            return httpx.Response(200, json={"runtimeSpecs": [
                {"key": {"variant": "VARIANT_GPU", "accelerator": "T4", "shape": "SHAPE_STANDARD"},
                 "eligible": True},
                {"key": {"variant": "VARIANT_GPU", "accelerator": "H100", "shape": "SHAPE_STANDARD"}},
            ]})
        if path == "/v1beta/runtimes" and request.method == "GET":
            return httpx.Response(200, json={})
        if path == "/v1beta/runtimes":
            assert json.loads(request.content) == {"runtimeSpec": {
                "variant": "VARIANT_GPU", "accelerator": "T4", "shape": "SHAPE_STANDARD"}}
            if self.create_error:
                return httpx.Response(200, json={"name": "operations/1", "done": True,
                                                 "error": self.create_error})
            return httpx.Response(200, json={"name": "operations/1"})
        if path == "/v1/operations/1":
            return httpx.Response(200, json={"name": "operations/1", "done": True,
                                             "response": {"name": "runtimes/r-1"}})
        if path == "/v1beta/runtimes/r-1" and request.method == "DELETE":
            return httpx.Response(200, json={})
        if path == "/v1beta/runtimes/r-1":
            return httpx.Response(200, json={"name": "runtimes/r-1", "connectionInfo": {
                "url": "https://jupyter.example/", "token": "proxy-token"}})
        return httpx.Response(404)


class Kernel:
    """Conexão de mensagens de mentira: cada célula enfileirada roda e imprime."""

    def __init__(self, opened):
        self.opened = opened
        self.out = []
        self.states = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def send(self, frame):
        msg = json.loads(frame)
        parent = {"msg_id": msg["header"]["msg_id"]}
        if msg["header"]["msg_type"] == "kernel_info_request":
            self.out.append({"msg_type": "kernel_info_reply", "parent_header": parent, "content": {}})
            return
        self.out.append({"msg_type": "status", "parent_header": parent,
                         "content": {"execution_state": "busy"}})
        self.out.append({"msg_type": "stream", "parent_header": parent,
                         "content": {"text": "rodou " + msg["content"]["code"][:12] + "\n"}})

    def recv(self, timeout=None):
        return json.dumps(self.out.pop(0))

    def __iter__(self):
        while self.out:
            yield json.dumps(self.out.pop(0))
            self.states.append(colab_api.status().get("state"))
        # A conexão cai aqui. O teste encerra a sessão para o executor não reconectar.
        colab_api._set(state="off")


def _wait(admin, done):
    for _ in range(200):
        state = admin.get("/api/colab/status").json()
        if done(state):
            return state
        time.sleep(0.02)
    return state


def _use_colab(admin):
    admin.put("/api/settings/runtime", json={
        "mode": "hybrid", "placements": {"llm": "local", "comfyui": "colab"}})


def test_connect_the_google_account_and_list_what_it_can_use(admin):
    google = Google()
    admin.app.dependency_overrides[deps.get_http_transport] = lambda: httpx.MockTransport(google)
    assert admin.get("/api/colab/auth-url").status_code == 400
    saved = admin.put("/api/colab/client", json={
        "client_id": "id-123.apps.googleusercontent.com", "client_secret": "secret-123456"}).json()
    assert saved["client_saved"] is True and saved["connected"] is False
    url = admin.get("/api/colab/auth-url").json()["url"]
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    assert query["scope"] == colab_api.SCOPE and query["access_type"] == "offline"
    # Endereço de outra tentativa não serve.
    assert admin.post("/api/colab/auth", json={
        "pasted": "http://localhost:8765/?state=outro&code=the-code"}).status_code == 400
    done = admin.post("/api/colab/auth", json={
        "pasted": f"http://localhost:8765/?state={query['state']}&code=the-code&scope=x"})
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["connected"] is True and body["tier"] == "SUBSCRIPTION_TIER_PRO"
    assert body["specs"] == [
        {"variant": "VARIANT_GPU", "accelerator": "T4", "shape": "SHAPE_STANDARD", "eligible": True},
        {"variant": "VARIANT_GPU", "accelerator": "H100", "shape": "SHAPE_STANDARD", "eligible": False},
    ]
    # Nenhum segredo volta para a tela.
    assert "secret-123456" not in done.text and "rt-123456" not in done.text
    assert admin.delete("/api/colab/auth").json()["connected"] is False


def test_only_the_admin_drives_colab(client):
    assert client.get("/api/colab").status_code == 403
    assert client.post("/api/colab/start", json={
        "variant": "VARIANT_GPU", "accelerator": "T4", "shape": "SHAPE_STANDARD"}).status_code == 403


def test_start_runs_the_notebook_cells_on_the_session_and_stop_deletes_it(admin, monkeypatch):
    google = Google()
    admin.app.dependency_overrides[deps.get_http_transport] = lambda: httpx.MockTransport(google)
    with db.session() as conn:
        colab_api.save_client(conn, "id-123.apps.googleusercontent.com", "secret-123456")
        colab_api.save_refresh_token(conn, "rt-123456")
    spec = {"variant": "VARIANT_GPU", "accelerator": "T4", "shape": "SHAPE_STANDARD"}
    # Tudo local: não há o que subir no Colab.
    assert admin.post("/api/colab/start", json=spec).status_code == 400
    _use_colab(admin)
    opened = []
    kernels = []

    def connect(url, additional_headers=None, **kwargs):
        opened.append((url, additional_headers, kwargs.get("ping_interval", "padrão")))
        kernels.append(Kernel(opened))
        return kernels[-1]

    monkeypatch.setattr("websockets.sync.client.connect", connect)
    started = admin.post("/api/colab/start", json={**spec, "project_url": "https://play.example"})
    assert started.status_code == 200, started.text
    _wait(admin, lambda state: state["state"] == "off")
    url, headers, ping = opened[0]
    assert url.startswith("wss://jupyter.example/api/kernels/kernel-1/channels?session_id=")
    assert headers == {colab_api.PROXY_HEADER: "proxy-token"} and ping is None
    # Passou pelas células na ordem e chegou na última, que é a que segura a sessão.
    assert "starting" in kernels[0].states and kernels[0].states[-1] == "on"
    assert ("POST", "jupyter.example", "/api/kernels") in google.calls

    colab_api._set(state="on", runtime="runtimes/r-1")
    assert admin.post("/api/colab/start", json={
        **spec, "project_url": "https://play.example"}).status_code == 409
    assert admin.post("/api/colab/stop", json={}).json() == {"state": "off"}
    assert ("DELETE", "colaboratory.googleapis.com", "/v1beta/runtimes/r-1") in google.calls
    assert admin.post("/api/colab/stop", json={"name": "../x"}).status_code == 400


def test_no_machine_available_is_said_in_plain_words(admin, monkeypatch):
    google = Google(create_error={"code": 9, "message": "resource_exhausted",
                                  "details": [{"reason": "NO_RUNTIMES"}]})
    admin.app.dependency_overrides[deps.get_http_transport] = lambda: httpx.MockTransport(google)
    with db.session() as conn:
        colab_api.save_client(conn, "id-123.apps.googleusercontent.com", "secret-123456")
        colab_api.save_refresh_token(conn, "rt-123456")
    _use_colab(admin)
    admin.post("/api/colab/start", json={
        "variant": "VARIANT_GPU", "accelerator": "T4", "shape": "SHAPE_STANDARD",
        "project_url": "https://play.example"})
    state = _wait(admin, lambda state: state["state"] == "error")
    assert state["state"] == "error" and "não tem máquina livre" in state["error"]
