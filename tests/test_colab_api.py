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

    def __init__(self, create_error=None, listed=()):
        self.calls = []
        self.create_error = create_error
        self.listed = list(listed)

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
        if request.url.host != "colaboratory.googleapis.com":
            return httpx.Response(404)  # o jogo também consulta o narrador por este transporte
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
            return httpx.Response(200, json={"runtimes": [
                {"name": name, "runtimeSpec": {"accelerator": "T4"}} for name in self.listed]})
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
        if path.startswith("/v1beta/runtimes/") and request.method == "DELETE":
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
    # Desligar uma sessão solta da conta não derruba a que o jogo acompanha.
    assert admin.post("/api/colab/stop", json={"name": "runtimes/r-outra"}).json()["state"] == "on"
    assert ("DELETE", "colaboratory.googleapis.com", "/v1beta/runtimes/r-outra") in google.calls
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
    monkeypatch.setattr(colab_api, "_RETRY_WAIT_S", 0)
    admin.post("/api/colab/start", json={
        "variant": "VARIANT_GPU", "accelerator": "T4", "shape": "SHAPE_STANDARD",
        "project_url": "https://play.example"})
    state = _wait(admin, lambda state: state["state"] == "error")
    assert state["state"] == "error" and "não tem máquina livre" in state["error"]
    # Insistiu antes de desistir.
    creates = [call for call in google.calls if call == ("POST", "colaboratory.googleapis.com", "/v1beta/runtimes")]
    assert len(creates) == colab_api._CREATE_TRIES


@pytest.mark.parametrize("machine_arrives", [False, True])
def test_turning_off_while_colab_is_still_creating(admin, monkeypatch, machine_arrives):
    """Visto em produção: desligar durante o pedido deixava a thread antiga
    viva. Ela seguia pedindo máquina, e a que chegasse rodava sem aparecer na tela."""

    class StoppedMidway(Google):
        def __call__(self, request):
            response = super().__call__(request)
            if request.url.path == "/v1beta/runtimes" and request.method == "POST":
                # O admin desliga enquanto o Colab ainda responde ao pedido.
                colab_api._state.clear()
                colab_api._state.update({"state": "off"})
            return response

    google = StoppedMidway(create_error=None if machine_arrives else {
        "code": 9, "message": "x", "details": [{"reason": "NO_RUNTIMES"}]})
    admin.app.dependency_overrides[deps.get_http_transport] = lambda: httpx.MockTransport(google)
    with db.session() as conn:
        colab_api.save_client(conn, "id-123.apps.googleusercontent.com", "secret-123456")
        colab_api.save_refresh_token(conn, "rt-123456")
    _use_colab(admin)
    monkeypatch.setattr(colab_api, "_RETRY_WAIT_S", 0.01)
    opened = []
    monkeypatch.setattr("websockets.sync.client.connect",
                        lambda *args, **kwargs: opened.append(args) or pytest.fail("abriu o kernel"))
    admin.post("/api/colab/start", json={
        "variant": "VARIANT_GPU", "accelerator": "T4", "shape": "SHAPE_STANDARD",
        "project_url": "https://play.example"})
    deleted = ("DELETE", "colaboratory.googleapis.com", "/v1beta/runtimes/r-1")
    for _ in range(200):
        if (deleted in google.calls) == machine_arrives and not any(
                getattr(thread, "_target", None) is colab_api._run and thread.is_alive()
                for thread in colab_api.threading.enumerate()):
            break
        time.sleep(0.02)
    assert admin.get("/api/colab/status").json() == {"state": "off"}
    creates = [call for call in google.calls
               if call == ("POST", "colaboratory.googleapis.com", "/v1beta/runtimes")]
    assert len(creates) == 1 and opened == []
    # A máquina que chegou depois do desligar é apagada; sem máquina, nada a apagar.
    assert (deleted in google.calls) == machine_arrives


def _connected(admin, google):
    admin.app.dependency_overrides[deps.get_http_transport] = lambda: httpx.MockTransport(google)
    with db.session() as conn:
        colab_api.save_client(conn, "id-123.apps.googleusercontent.com", "secret-123456")
        colab_api.save_refresh_token(conn, "rt-123456")
    _use_colab(admin)


def test_session_is_remembered_across_a_game_restart(admin):
    """Visto em produção: um deploy no meio da subida fazia a tela esquecer a sessão."""
    google = Google(listed=["runtimes/r-1"])
    _connected(admin, google)
    spec = {"variant": "VARIANT_GPU", "accelerator": "T4", "shape": "SHAPE_STANDARD"}
    with db.session() as conn:
        db.set_setting(conn, "colab.session", json.dumps({"runtime": "runtimes/r-1", "spec": spec}))
    # A memória do processo está vazia, como depois de um reinício.
    session = admin.get("/api/colab").json()["session"]
    assert (session["state"], session["runtime"], session["resumed"]) == ("starting", "runtimes/r-1", True)
    assert admin.get("/api/colab/status").json()["state"] == "starting"
    assert admin.post("/api/colab/start", json={**spec, "project_url": "https://play.example"}).status_code == 409
    assert admin.post("/api/colab/stop", json={}).json() == {"state": "off"}
    assert ("DELETE", "colaboratory.googleapis.com", "/v1beta/runtimes/r-1") in google.calls

    # Sessão gravada que já não existe na conta: some sozinha.
    google.listed = []
    with db.session() as conn:
        db.set_setting(conn, "colab.session", json.dumps({"runtime": "runtimes/r-9", "spec": spec}))
    assert admin.get("/api/colab").json()["session"] == {"state": "off"}
    with db.session() as conn:
        assert db.get_setting(conn, "colab.session") is None


def test_a_failed_startup_turns_the_machine_off(admin, monkeypatch):
    """Sem isto a sessão que falhou numa célula seguia ligada, gastando sem aparecer."""
    google = Google()
    _connected(admin, google)

    class Failing(Kernel):
        def send(self, frame):
            msg = json.loads(frame)
            parent = {"msg_id": msg["header"]["msg_id"]}
            if msg["header"]["msg_type"] == "kernel_info_request":
                self.out.append({"msg_type": "kernel_info_reply", "parent_header": parent, "content": {}})
            elif not any(item["msg_type"] == "error" for item in self.out):
                self.out.append({"msg_type": "error", "parent_header": parent,
                                 "content": {"ename": "SystemExit", "evalue": "VRAM insuficiente"}})

    monkeypatch.setattr("websockets.sync.client.connect", lambda *args, **kwargs: Failing([]))
    admin.post("/api/colab/start", json={
        "variant": "VARIANT_GPU", "accelerator": "T4", "shape": "SHAPE_STANDARD",
        "project_url": "https://play.example"})
    state = _wait(admin, lambda state: state["state"] == "error")
    assert "VRAM insuficiente" in state["error"]
    for _ in range(100):
        if ("DELETE", "colaboratory.googleapis.com", "/v1beta/runtimes/r-1") in google.calls:
            break
        time.sleep(0.02)
    assert ("DELETE", "colaboratory.googleapis.com", "/v1beta/runtimes/r-1") in google.calls
    with db.session() as conn:
        assert db.get_setting(conn, "colab.session") is None


def test_estimates_say_what_fits_and_how_fast(admin):
    google = Google()
    _connected(admin, google)
    library = admin.post("/api/models/library", json={
        "kind": "llm", "family": "gguf", "label": "Dolphin 24B",
        "file": {"name": "Dolphin-24B-Q8_0.gguf", "sha256": "ab" * 32, "bytes": 25_000_000_000,
                 "url": "https://huggingface.co/x/y/resolve/main/Dolphin-24B-Q8_0.gguf"}}).json()["id"]
    admin.put("/api/settings/llm", json={"context_size": 65536})
    admin.put("/api/settings/runtime", json={
        "mode": "colab", "placements": {"llm": "colab", "comfyui": "colab"},
        "models": {"llm": library}})
    t4 = admin.get("/api/colab").json()["estimates"]["T4"]
    narrator = next(item for item in t4["items"] if item["kind"] == "llm")
    # 25 GB de pesos mais o KV de 131072 tokens: longe de caber em 15 GB.
    assert narrator["need_gb"] > 35 and "tokens por segundo" in narrator["speed"]
    assert narrator["note"] == "qualidade máxima"
    assert (t4["fits"], t4["rating"], t4["vram_measured"]) == (False, "pessimo", False)
    assert {item["kind"] for item in t4["items"]} == {"llm", "image", "video"}
    # A placa entregue de verdade tinha 80 GB: o medido vale mais que a tabela.
    with db.session() as conn:
        db.set_setting(conn, "colab.gpu_vram", json.dumps({"T4": 80.0}))
    measured = admin.get("/api/colab").json()["estimates"]["T4"]
    assert (measured["vram_gb"], measured["vram_measured"], measured["fits"]) == (80.0, True, True)
    # Só o que vai para o Colab entra na conta.
    assert admin.put("/api/settings/runtime", json={
        "mode": "hybrid", "placements": {"llm": "local", "comfyui": "colab"},
        "models": {"llm": "qwen3.6-35b-a3b"}}).status_code == 200
    only_images = admin.get("/api/colab").json()["estimates"]["T4"]
    assert {item["kind"] for item in only_images["items"]} == {"image", "video"}


def test_phone_notifications_go_through_ntfy(admin):
    """O aviso sai do servidor: chega com o jogo fechado no telefone."""
    sent = []

    def ntfy(request: httpx.Request) -> httpx.Response:
        if request.url.host == "ntfy.sh":
            sent.append((request.url.path, request.content.decode(), request.headers.get("Tags")))
            return httpx.Response(200, json={})
        return httpx.Response(404)

    admin.app.dependency_overrides[deps.get_http_transport] = lambda: httpx.MockTransport(ntfy)
    # Sem tópico o teste diz que não enviou, e nada quebra.
    assert admin.post("/api/colab/notify/test").status_code == 400
    assert admin.put("/api/colab/notify", json={"topic": "x"}).status_code == 400
    topic = admin.put("/api/colab/notify", json={"generate": True}).json()["notify_topic"]
    assert topic.startswith("aidungeon-") and len(topic) >= 20
    assert admin.get("/api/colab").json()["notify_topic"] == topic
    assert admin.post("/api/colab/notify/test").json() == {"ok": True}
    assert sent[0][0] == f"/{topic}" and "teste" in sent[0][1] and sent[0][2] == "bell"
    assert admin.put("/api/colab/notify", json={"topic": ""}).json() == {"notify_topic": ""}


def test_registration_tells_the_phone_the_gpu_is_ready(admin, monkeypatch):
    from app import notify
    said = []
    monkeypatch.setattr(notify, "send", lambda message, **kwargs: said.append(message) or True)
    _use_colab(admin)
    payload = {
        "endpoints": {"llm": "", "comfyui": "https://gpu.trycloudflare.com"},
        "models": {"llm": "qwen3.6-35b-a3b", "image": "flux2_klein", "video": "ltxv"},
        "hardware": {"gpu_name": "NVIDIA A100-SXM4-80GB", "vram_total_mib": 81920},
    }
    assert admin.post("/api/runtime/register", json=payload).status_code == 200
    assert said == ["Colab pronto (NVIDIA A100-SXM4-80GB). O jogo já usa essa placa para imagem e vídeo."]
    # A memória real da placa fica guardada para as estimativas.
    with db.session() as conn:
        assert json.loads(db.get_setting(conn, "colab.gpu_vram")) == {"A100": 80.0}
    # A batida de cada minuto não avisa de novo.
    assert admin.post("/api/runtime/register", json=payload).status_code == 200
    assert len(said) == 1
