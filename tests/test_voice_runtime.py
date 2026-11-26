"""Voz na tela de Runtime: modelos, onde roda, Colab e estimativas."""

from __future__ import annotations

import json
import subprocess

import pytest

from app import db, notify, settings_store, voice_runtime

MODELS = {"llm": "qwen3.6-35b-a3b", "image": "flux2_klein", "video": "ltxv"}
ABZ = "https://voz.example/v1"


@pytest.fixture()
def admin(client, admin_token, monkeypatch, tmp_path):
    client.headers["Authorization"] = f"Bearer {admin_token}"
    monkeypatch.setenv("AIDUNGEON_VOICE_HOME", str(tmp_path / "voice"))
    # Nenhum teste fala com um servidor de voz de verdade nesta máquina.
    monkeypatch.setattr(voice_runtime, "local_health", lambda: None)
    monkeypatch.setattr(notify, "send", lambda *args, **kwargs: True)
    voice_runtime._install.clear()
    voice_runtime._install.update({"state": "idle"})
    client.put("/api/settings/voice", json={"base_url": ABZ, "api_key": "chave-do-servidor-abz"})
    return client


def _effective() -> dict:
    with db.session() as conn:
        return settings_store.effective_voice_settings(conn)


def _running(monkeypatch, tts="omnivoice-base-Q8_0.gguf", stt="small"):
    env = voice_runtime.home() / "voice.env"
    if not env.is_file():
        env.parent.mkdir(parents=True, exist_ok=True)
        env.write_text("VOICE_API_KEY=chave-desta-maquina\nVOICE_STT_MODEL=small\n")
    monkeypatch.setattr(voice_runtime, "local_health", lambda: {
        "status": "ok", "tts": tts, "stt": stt, "stt_device": "cuda"})


def _register(client, voice="https://voz-gpu.trycloudflare.com/v1"):
    return client.post("/api/runtime/register", json={
        "endpoints": {"llm": "", "comfyui": "", "voice": voice},
        "models": MODELS, "hardware": {"gpu_name": "Tesla T4", "vram_total_mib": 15360},
        "voice_models": {"tts": "omnivoice-q8", "stt": "whisper-small"}})


def test_default_keeps_the_configured_server(admin):
    view = admin.get("/api/voice-runtime").json()
    assert (view["placement"], view["tts"], view["stt"]) == ("server", "omnivoice-q8", "whisper-small")
    assert view["server_url"] == ABZ and view["local"]["running"] is False
    assert set(view["bundles"]) == {"leve", "equilibrado", "maximo"}
    assert len(view["tts_models"]) == 4 and len(view["stt_models"]) == 6
    assert _effective()["base_url"] == ABZ
    assert admin.get("/api/settings/voice").json()["serving"] == "server"


def test_a_bundle_picks_both_models_and_each_can_change_alone(admin):
    view = admin.put("/api/voice-runtime", json={"bundle": "maximo"}).json()
    assert (view["tts"], view["stt"], view["bundle"]) == ("omnivoice-bf16", "whisper-large-v3", "maximo")
    view = admin.put("/api/voice-runtime", json={"stt": "whisper-medium"}).json()
    assert (view["tts"], view["stt"], view["bundle"]) == ("omnivoice-bf16", "whisper-medium", None)
    assert admin.put("/api/voice-runtime", json={"tts": "inventado"}).status_code == 400
    assert admin.put("/api/voice-runtime", json={"bundle": "inventado"}).status_code == 400
    assert admin.put("/api/voice-runtime", json={"placement": "lua"}).status_code == 422


def test_this_machine_only_after_its_server_answers(admin, monkeypatch):
    refused = admin.put("/api/voice-runtime", json={"placement": "local"})
    assert refused.status_code == 400 and refused.json()["error"]["code"] == "voice_not_installed"
    _running(monkeypatch)
    view = admin.put("/api/voice-runtime", json={"placement": "local"}).json()
    assert view["local"] == {"running": True, "tts": "omnivoice-q8", "stt": "whisper-small",
                             "stt_device": "cuda"}
    cfg = _effective()
    assert cfg["base_url"] == voice_runtime.LOCAL_URL
    assert (cfg["tts_model"], cfg["stt_model"]) == ("omnivoice", "whisper-1")
    assert cfg["api_key"] == "chave-desta-maquina"
    # O formulário da aba Voz continua mostrando (e gravando) o servidor configurado.
    shown = admin.get("/api/settings/voice").json()
    assert (shown["base_url"], shown["serving"]) == (ABZ, "local")
    with db.session() as conn:
        # A chave do servidor do jogo vai escrita no notebook: não pode ser apagada dele.
        assert cfg["api_key"] not in settings_store.secret_values(conn)
        assert "chave-do-servidor-abz" in settings_store.secret_values(conn)


def test_colab_takes_the_voice_and_gives_it_back_when_it_stops(admin, monkeypatch):
    said = []
    monkeypatch.setattr(notify, "send", lambda message, **kwargs: said.append(message) or True)
    view = admin.put("/api/voice-runtime", json={"placement": "colab"}).json()
    assert (view["placement"], view["fallback"], view["colab"]["serving"]) == ("colab", "server", False)
    assert _effective()["base_url"] == ABZ  # o Colab ainda não chegou
    assert _register(admin).status_code == 200
    cfg = _effective()
    assert cfg["base_url"] == "https://voz-gpu.trycloudflare.com/v1"
    assert (cfg["tts_model"], cfg["stt_model"]) == ("omnivoice", "whisper-1")
    assert said == ["Colab pronto (Tesla T4). O jogo já usa essa placa para voz."]
    view = admin.get("/api/voice-runtime").json()
    assert view["colab"] == {"serving": True,
                             "models": {"tts": "omnivoice-q8", "stt": "whisper-small"}}
    assert admin.get("/api/settings/voice").json()["serving"] == "colab"
    # A batida de cada minuto não avisa de novo; sessão nova, com outro túnel, avisa.
    assert _register(admin).status_code == 200 and len(said) == 1
    assert _register(admin, "https://outro.trycloudflare.com/v1").status_code == 200
    assert len(said) == 2 and _effective()["base_url"] == "https://outro.trycloudflare.com/v1"
    # Três batidas perdidas: a voz volta para onde estava.
    with db.session() as conn:
        settings_store._set_override(conn, "runtime.registration.last_seen", "2020-01-01T00:00:00Z")
    assert _effective()["base_url"] == ABZ
    assert admin.get("/api/settings/voice").json()["serving"] == "server"


def test_colab_falls_back_to_this_machine_when_the_voice_was_here(admin, monkeypatch):
    _running(monkeypatch)
    admin.put("/api/voice-runtime", json={"placement": "local"})
    view = admin.put("/api/voice-runtime", json={"placement": "colab"}).json()
    assert view["fallback"] == "local"
    assert _effective()["base_url"] == voice_runtime.LOCAL_URL


def test_registration_ignores_a_voice_address_that_is_not_https(admin):
    admin.put("/api/voice-runtime", json={"placement": "colab"})
    assert _register(admin, "http://127.0.0.1:1/v1").status_code == 200
    assert _effective()["base_url"] == ABZ


def test_notebook_brings_the_voice_server_even_with_everything_else_here(admin):
    # Tudo nesta máquina e a voz também: não há o que rodar no Colab.
    assert admin.get("/api/runtime/notebook?project_url=https://jogo.example").status_code == 400
    admin.put("/api/voice-runtime", json={"placement": "colab", "bundle": "leve"})
    resp = admin.get("/api/runtime/notebook?project_url=https://jogo.example")
    assert resp.status_code == 200, resp.text
    notebook = json.loads(resp.content)
    cells = {cell["id"]: "".join(cell["source"]) for cell in notebook["cells"]}
    assert list(cells) == ["overview", "variables", "secrets", "helpers", "hardware", "provision",
                           "health", "tunnel", "register", "keepalive"]
    with db.session() as conn:
        key = voice_runtime.key(conn)
    assert f"VOICE_KEY = {key!r}" in cells["secrets"]
    assert key not in cells["variables"]
    assert "omnivoice-base-Q4_K_M.gguf" in cells["provision"]
    assert "omnivoice-tokenizer-Q4_K_M.gguf" in cells["provision"]
    assert '"-DGGML_CUDA=ON"' in cells["provision"] and "faster-whisper" in cells["provision"]
    assert '"git", "apply", str(voice_dir / "omnivoice-ref-rvq.patch")' in cells["provision"]
    assert "PUBLIC_VOICE = open_tunnel" in cells["tunnel"]
    assert "PUBLIC_LLM" not in cells["tunnel"] and "PUBLIC_COMFY" not in cells["tunnel"]
    assert 'payload["endpoints"]["voice"] = PUBLIC_VOICE + "/v1"' in cells["register"]
    assert "voz (omnivoice-q4 + whisper-small)" in cells["overview"]
    for name, source in cells.items():
        if name != "overview":
            compile(source, name, "exec")


def test_notebook_without_voice_is_unchanged(admin):
    admin.put("/api/settings/runtime", json={
        "mode": "hybrid", "placements": {"llm": "local", "comfyui": "colab"}})
    notebook = json.loads(admin.get("/api/runtime/notebook?project_url=https://jogo.example").content)
    text = json.dumps(notebook)
    assert "VOICE_KEY" not in text and "PUBLIC_VOICE" not in text and "omnivoice" not in text


def test_estimates_count_the_voice_on_the_colab_gpu(admin):
    body = {"accelerators": ["T4", "H100"], "placements": {"llm": "local", "comfyui": "local"},
            "models": {}, "context": 32768}
    assert admin.post("/api/colab/estimate", json=body).json()["estimates"]["T4"]["items"] == []
    # A voz ainda está no servidor configurado: a tela pergunta "e se fosse para o Colab?".
    draft = {**body, "voice": {"placement": "colab", "tts": "omnivoice-bf16", "stt": "whisper-large-v3"}}
    t4 = admin.post("/api/colab/estimate", json=draft).json()["estimates"]["T4"]
    rows = {row["kind"]: row for row in t4["items"]}
    assert set(rows) == {"tts", "stt"}
    assert rows["tts"]["need_gb"] > 1.5 and "por fala curta" in rows["tts"]["speed"]
    assert rows["stt"]["need_gb"] == 4.4 and rows["stt"]["rating"] in ("excelente", "bom")
    assert t4["total_gb"] == pytest.approx(rows["tts"]["need_gb"] + rows["stt"]["need_gb"], abs=0.11)
    assert t4["fits"] is True
    lighter = admin.post("/api/colab/estimate", json={
        **draft, "voice": {"placement": "colab", "tts": "omnivoice-q4", "stt": "whisper-tiny"}},
    ).json()["estimates"]["T4"]
    assert lighter["total_gb"] < t4["total_gb"]
    # Um Whisper que erra muito é péssimo em qualquer placa, por mais rápido que seja.
    assert next(row for row in lighter["items"] if row["kind"] == "stt")["rating"] == "pessimo"
    assert admin.get("/api/voice-runtime").json()["placement"] == "server"


def test_install_on_this_machine_writes_the_service_and_takes_the_voice(admin, monkeypatch, tmp_path):
    home = tmp_path / "voice"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(voice_runtime.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(voice_runtime.shutil, "which", lambda tool: f"/usr/bin/{tool}")
    ran = []

    def fake_run(cmd, **kwargs):
        ran.append([str(part) for part in cmd])
        if cmd[1:3] == ["-m", "venv"]:
            (home / "venv" / "bin").mkdir(parents=True)
            (home / "venv" / "bin" / "python").write_text("")
        if cmd[0] == "cmake" and "--build" in cmd:
            for name in ("omnivoice-tts", "omnivoice-codec"):
                (home / "omnivoice.cpp" / "build" / name).parent.mkdir(parents=True, exist_ok=True)
                (home / "omnivoice.cpp" / "build" / name).write_text("")

    def fake_fetch(url, dest, sha256, **kwargs):
        assert url.startswith("https://huggingface.co/Serveurperso/OmniVoice-GGUF/") and len(sha256) == 64
        open(dest, "w").write("gguf")

    from app import runtime_provisioning
    monkeypatch.setattr(voice_runtime, "_run", fake_run)
    monkeypatch.setattr(runtime_provisioning, "fetch_pinned", fake_fetch)
    monkeypatch.setattr(voice_runtime, "_systemctl", lambda *args: (
        ran.append(["systemctl", *args]) or subprocess.CompletedProcess(args, 0, "", "")))
    # A instalação roda depois de o pedido responder, como a thread de verdade.
    queued = []
    monkeypatch.setattr(voice_runtime.threading, "Thread", lambda target, args, daemon: type(
        "Later", (), {"start": lambda self: queued.append(lambda: target(*args))})())
    admin.put("/api/voice-runtime", json={"bundle": "equilibrado"})
    monkeypatch.setattr(voice_runtime, "local_health", lambda: {
        "status": "ok", "tts": "omnivoice-base-Q8_0.gguf", "stt": "large-v3-turbo",
        "stt_device": "cuda"})

    started = admin.post("/api/voice-runtime/install", json={"activate": True}).json()
    assert (started["install"]["state"], started["placement"]) == ("running", "server")
    assert admin.post("/api/voice-runtime/install", json={}).status_code == 409
    queued.pop()()
    view = admin.get("/api/voice-runtime").json()
    assert view["install"]["state"] == "done", view["install"]
    assert view["placement"] == "local"
    env = dict(line.split("=", 1) for line in (home / "voice.env").read_text().splitlines())
    assert env["VOICE_STT_MODEL"] == "large-v3-turbo"
    assert env["OMNI_MODEL"] == str(home / "models" / "omnivoice-base-Q8_0.gguf")
    assert env["OMNI_CODEC"] == str(home / "models" / "omnivoice-tokenizer-Q8_0.gguf")
    first_key = env["VOICE_API_KEY"]
    assert len(first_key) >= 24 and first_key == _effective()["api_key"]
    assert (home / "voice.env").stat().st_mode & 0o777 == 0o600
    unit = (tmp_path / ".config" / "systemd" / "user" / voice_runtime.UNIT).read_text()
    assert f"ExecStart={home / 'venv' / 'bin' / 'python'} {home / 'server' / 'server.py'}" in unit
    assert (home / "server" / "voices" / "M1.rvq").is_file()
    assert ["systemctl", "restart", voice_runtime.UNIT] in ran
    assert any(cmd[0] == "git" and "submodule" in cmd for cmd in ran)
    assert ["git", "apply", str(voice_runtime.PATCH)] in ran
    # Trocar só o Whisper não recompila nem baixa de novo o OmniVoice.
    ran.clear()
    admin.put("/api/voice-runtime", json={"stt": "whisper-small"})
    _running(monkeypatch, stt="small")
    admin.post("/api/voice-runtime/install", json={})
    queued.pop()()
    assert admin.get("/api/voice-runtime").json()["install"]["state"] == "done"
    assert not any(cmd[0] in ("git", "cmake") for cmd in ran)
    assert "VOICE_STT_MODEL=small" in (home / "voice.env").read_text()
    # A chave é da máquina: reinstalar não a troca debaixo de quem já usa.
    assert f"VOICE_API_KEY={first_key}" in (home / "voice.env").read_text()


def test_voice_routes_are_for_the_admin(client, auth_token):
    client.headers["Authorization"] = f"Bearer {auth_token}"
    codes = {client.get("/api/voice-runtime").status_code,
             client.put("/api/voice-runtime", json={"bundle": "leve"}).status_code,
             client.post("/api/voice-runtime/install", json={}).status_code}
    assert codes <= {401, 403}
