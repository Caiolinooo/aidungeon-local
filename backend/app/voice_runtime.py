"""Voz no mesmo esquema do narrador e da imagem: escolher modelo e onde roda.

O jogo fala com um servidor de voz compatível com a OpenAI. Aqui se escolhe
qual servidor atende:

- `server`: o que está digitado na aba Voz (como sempre foi);
- `local`: o servidor de voz do próprio jogo (`voice_server/`), nesta máquina;
- `colab`: o mesmo servidor, na GPU do Colab, ligado pelo notebook.

Os modelos saem de `config/voice_models.json`: OmniVoice em qualquer
quantização para falar e clonar, Whisper de qualquer tamanho para ouvir, e
pacotes que escolhem os dois de uma vez.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import httpx

from app import db, settings_store
from app.errors import GameError

CATALOG_PATH = db.REPO_ROOT / "config" / "voice_models.json"
SERVER_SRC = db.REPO_ROOT / "voice_server"
PORT = 8011
LOCAL_URL = f"http://127.0.0.1:{PORT}/v1"
UNIT = "aidungeon-voice.service"
PLACEMENTS = ("server", "local", "colab")
DEFAULTS = {"tts": "omnivoice-q8", "stt": "whisper-small"}
# Nomes que o servidor do jogo atende, seja qual for o arquivo carregado.
TTS_NAME, STT_NAME = "omnivoice", "whisper-1"

_catalog: dict | None = None
_install_lock = threading.Lock()
_install: dict = {"state": "idle"}


def home() -> Path:
    """Pasta da voz nesta máquina. É da máquina, não de uma instalação do jogo."""
    return Path(os.environ.get("AIDUNGEON_VOICE_HOME") or Path.home() / "models" / "voice")


def catalog() -> dict:
    global _catalog
    if _catalog is None:
        _catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    return _catalog


# ------------------------------------------------------------------ escolha
def selection(conn: sqlite3.Connection) -> dict:
    book = catalog()

    def pick(name: str) -> str:
        value = settings_store._get_override(conn, f"voice_runtime.{name}")
        return value if value in book[name] else DEFAULTS[name]

    placement = settings_store._get_override(conn, "voice_runtime.placement")
    fallback = settings_store._get_override(conn, "voice_runtime.fallback")
    return {
        "placement": placement if placement in PLACEMENTS else "server",
        "fallback": fallback if fallback in ("server", "local") else "server",
        "tts": pick("tts"),
        "stt": pick("stt"),
    }


def bundle_of(tts: str, stt: str) -> str | None:
    for name, item in catalog()["bundles"].items():
        if item["tts"] == tts and item["stt"] == stt:
            return name
    return None


def local_key() -> str:
    """Chave do servidor de voz desta máquina. Fica no arquivo que o serviço
    lê: é da máquina, e toda instalação do jogo nela usa a mesma."""
    try:
        for line in (home() / "voice.env").read_text().splitlines():
            if line.startswith("VOICE_API_KEY="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def key(conn: sqlite3.Connection) -> str:
    """Chave do servidor de voz que este jogo sobe no Colab. Nasce no primeiro uso."""
    value = settings_store._usable_secret(settings_store._get_override(conn, "voice_runtime.key"))
    if not value:
        value = secrets.token_urlsafe(24)
        settings_store._set_override(conn, "voice_runtime.key", value)
    return value


def save(conn: sqlite3.Connection, update: dict) -> dict:
    book = catalog()
    current = selection(conn)
    bundle = update.get("bundle")
    if bundle:
        if bundle not in book["bundles"]:
            raise GameError.bad_request("validation_error", "pacote de voz desconhecido")
        update = {**update, "tts": book["bundles"][bundle]["tts"],
                  "stt": book["bundles"][bundle]["stt"]}
    for name in ("tts", "stt"):
        value = update.get(name)
        if value is None:
            continue
        if value not in book[name]:
            raise GameError.bad_request("validation_error", f"modelo de voz desconhecido: {value}")
        settings_store._set_override(conn, f"voice_runtime.{name}", value)
    placement = update.get("placement")
    if placement is not None and placement != current["placement"]:
        if placement not in PLACEMENTS:
            raise GameError.bad_request("validation_error", "onde a voz roda: server, local ou colab")
        if placement == "local" and local_health() is None:
            raise GameError.bad_request(
                "voice_not_installed",
                "o servidor de voz do jogo ainda não está ligado nesta máquina: instale primeiro")
        if placement == "colab":
            # Sem o Colab, a voz volta para onde estava.
            settings_store._set_override(conn, "voice_runtime.fallback", current["placement"]
                                         if current["placement"] != "colab" else current["fallback"])
        settings_store._set_override(conn, "voice_runtime.placement", placement)
        _reset_clients()
    return view(conn)


def _reset_clients() -> None:
    from app import voice_client
    voice_client.reset_voice_clients()


# ---------------------------------------------------------------- roteamento
def colab_serving(conn: sqlite3.Connection) -> bool:
    return (selection(conn)["placement"] == "colab"
            and bool(settings_store._get_override(conn, "voice_runtime.colab_url"))
            and settings_store.colab_online(conn))


def route(conn: sqlite3.Connection, settings: dict) -> dict:
    """As configurações de voz com o endereço de quem atende agora."""
    chosen = selection(conn)
    placement = chosen["placement"]
    if placement == "colab":
        if colab_serving(conn):
            return {**settings, "base_url": settings_store._get_override(conn, "voice_runtime.colab_url"),
                    "api_key": key(conn), "tts_model": TTS_NAME, "stt_model": STT_NAME}
        # Colab que ainda não chegou ou parou de bater não derruba a voz.
        placement = chosen["fallback"]
    if placement == "local":
        return {**settings, "base_url": LOCAL_URL, "api_key": local_key() or None,
                "tts_model": TTS_NAME, "stt_model": STT_NAME}
    return settings


def register(conn: sqlite3.Connection, url: Any, models: Any) -> bool:
    """O notebook informou o servidor de voz. Devolve se o endereço é novo."""
    url = str(url or "").strip()
    if not url.startswith("https://"):
        return False
    if isinstance(models, dict):
        settings_store._set_override(conn, "voice_runtime.colab_models", {
            name: str(models.get(name) or "")[:80] for name in ("tts", "stt")})
    if url == settings_store._get_override(conn, "voice_runtime.colab_url"):
        return False
    settings_store._set_override(conn, "voice_runtime.colab_url", url)
    _reset_clients()
    return True


# ------------------------------------------------------------------- Colab
def colab_plan(conn: sqlite3.Connection) -> dict | None:
    """O que o notebook precisa para subir a voz no Colab, ou None se ela não vai para lá."""
    chosen = selection(conn)
    if chosen["placement"] != "colab":
        return None
    book = catalog()
    voices = SERVER_SRC / "voices"
    return {
        "key": key(conn),
        "port": PORT,
        "tts": chosen["tts"],
        "stt": chosen["stt"],
        "stt_model": book["stt"][chosen["stt"]]["model"],
        "git_url": book["engine"]["git_url"],
        "git_ref": book["engine"]["git_ref"],
        "artifacts": [dict(art) for art in book["tts"][chosen["tts"]]["artifacts"]],
        "server_b64": base64.b64encode((SERVER_SRC / "server.py").read_bytes()).decode("ascii"),
        "voices_b64": {path.name: base64.b64encode(path.read_bytes()).decode("ascii")
                       for path in sorted(voices.iterdir()) if path.is_file()},
    }


def estimate_entries(conn: sqlite3.Connection, draft: dict | None = None) -> dict | None:
    """Modelos de voz que entram na conta da GPU do Colab (escolha salva ou a da tela)."""
    chosen = {**selection(conn), **{name: value for name, value in (draft or {}).items() if value}}
    if chosen["placement"] != "colab":
        return None
    book = catalog()
    tts, stt = book["tts"].get(chosen["tts"]), book["stt"].get(chosen["stt"])
    return {"tts": tts, "stt": stt} if tts and stt else None


# ------------------------------------------------------------- esta máquina
def local_health() -> dict | None:
    try:
        resp = httpx.get(f"http://127.0.0.1:{PORT}/health", timeout=1.5)
        data = resp.json() if resp.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("status") == "ok" else None


def _running_ids(health: dict | None) -> dict:
    """Ids do catálogo que o servidor desta máquina está servindo."""
    if not health:
        return {"tts": None, "stt": None}
    book = catalog()
    tts = next((name for name, item in book["tts"].items()
                if item["artifacts"][0]["filename"] == health.get("tts")), None)
    stt = next((name for name, item in book["stt"].items()
                if item["model"] == health.get("stt")), None)
    return {"tts": tts, "stt": stt}


def install_status() -> dict:
    with _install_lock:
        status = dict(_install)
    part = status.pop("_part", None)
    if status.get("state") == "running" and part:
        try:
            status["file_bytes_done"] = Path(part).stat().st_size
        except OSError:
            status["file_bytes_done"] = 0
    return status


def _step(text: str, **fields: Any) -> None:
    with _install_lock:
        _install.update({"step": text, "_part": None, "file_bytes": None, **fields})


def _run(cmd: list[str], *, cwd: Path | None = None, timeout: int = 3600) -> None:
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-6:]
        raise RuntimeError(f"{Path(cmd[0]).name} falhou: " + " | ".join(tail)[-400:])


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    """systemctl do usuário. O jogo roda como serviço do sistema, sem a sessão
    do usuário no ambiente: o endereço dela vai explícito."""
    runtime = f"/run/user/{os.getuid()}"
    env = {**os.environ, "XDG_RUNTIME_DIR": runtime,
           "DBUS_SESSION_BUS_ADDRESS": f"unix:path={runtime}/bus"}
    return subprocess.run(["systemctl", "--user", *args], env=env, capture_output=True,
                          text=True, timeout=120)


def _prepare_python(root: Path) -> Path:
    python = root / "venv" / "bin" / "python"
    if not python.is_file():
        _step("criando o ambiente Python da voz")
        _run([sys.executable, "-m", "venv", str(root / "venv")])
    wanted = (SERVER_SRC / "requirements.txt").read_bytes()
    marker = root / "venv" / ".requirements.sha256"
    digest = hashlib.sha256(wanted).hexdigest()
    if not marker.is_file() or marker.read_text().strip() != digest:
        _step("instalando o Whisper e as bibliotecas da placa (uns minutos)")
        _run([str(python), "-m", "pip", "install", "-q", "--upgrade", "pip"])
        _run([str(python), "-m", "pip", "install", "-q", "-r", str(SERVER_SRC / "requirements.txt")])
        marker.write_text(digest)
    return python


def _prepare_omnivoice(root: Path) -> Path:
    engine = catalog()["engine"]
    src = root / "omnivoice.cpp"
    build = src / "build"
    stamp = build / ".built"
    if stamp.is_file() and stamp.read_text().strip() == engine["git_ref"] \
            and (build / "omnivoice-tts").is_file() and (build / "omnivoice-codec").is_file():
        return build
    _step("compilando o OmniVoice (uns minutos)")
    if not (src / ".git").is_dir():
        _run(["git", "clone", engine["git_url"], str(src)], timeout=900)
    _run(["git", "fetch", "origin", engine["git_ref"]], cwd=src, timeout=900)
    _run(["git", "checkout", "--detach", engine["git_ref"]], cwd=src)
    _run(["git", "submodule", "update", "--init", "--recursive"], cwd=src, timeout=900)
    cuda = "ON" if shutil.which("nvidia-smi") and shutil.which("nvcc") else "OFF"
    _run(["cmake", "-S", str(src), "-B", str(build), f"-DGGML_CUDA={cuda}",
          "-DCMAKE_BUILD_TYPE=Release"])
    _run(["cmake", "--build", str(build), "--target", "omnivoice-tts", "omnivoice-codec",
          "-j", str(max(2, (os.cpu_count() or 4) // 2))], timeout=7200)
    stamp.write_text(engine["git_ref"])
    return build


def _write_service(root: Path, python: Path, build: Path, tts: dict, stt_model: str,
                   voice_key: str) -> None:
    models = root / "models"
    env = {
        "VOICE_API_KEY": voice_key,
        "VOICE_STT_MODEL": stt_model,
        "VOICE_HOST": "127.0.0.1",
        "VOICE_PORT": str(PORT),
        "OMNI_BIN": str(build / "omnivoice-tts"),
        "OMNI_CODEC_BIN": str(build / "omnivoice-codec"),
        "OMNI_MODEL": str(models / tts["artifacts"][0]["filename"]),
        "OMNI_CODEC": str(models / tts["artifacts"][1]["filename"]),
        "VOICE_DIR": str(root / "server" / "voices"),
        # Vozes clonadas ficam fora da pasta do servidor: reinstalar não as apaga.
        "VOICE_CUSTOM_DIR": str(root / "custom"),
    }
    env_file = root / "voice.env"
    env_file.touch(mode=0o600)
    env_file.chmod(0o600)
    env_file.write_text("".join(f"{name}={value}\n" for name, value in env.items()))
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / UNIT).write_text(
        "[Unit]\nDescription=AI Dungeon: servidor de voz (TTS, clonagem e STT)\n\n"
        "[Service]\n"
        f"EnvironmentFile={env_file}\n"
        f"ExecStart={python} {root / 'server' / 'server.py'}\n"
        "Restart=always\nRestartSec=3\n\n"
        "[Install]\nWantedBy=default.target\n")


def _run_install(tts_id: str, stt_id: str, voice_key: str, hf_token: str, activate: bool) -> None:
    from app import runtime_provisioning
    book = catalog()
    tts, stt = book["tts"][tts_id], book["stt"][stt_id]
    root = home()
    try:
        (root / "models").mkdir(parents=True, exist_ok=True)
        _step("copiando o servidor de voz")
        shutil.copytree(SERVER_SRC, root / "server", dirs_exist_ok=True)
        python = _prepare_python(root)
        build = _prepare_omnivoice(root)
        for art in tts["artifacts"]:
            dest = root / "models" / art["filename"]
            if dest.is_file() and dest.stat().st_size == int(art["bytes"]):
                continue
            _step(f"baixando {art['filename']}", _part=f"{dest}.part", file_bytes=art["bytes"])
            runtime_provisioning.fetch_pinned(
                art["url"], str(dest), art["sha256"], retries=3, expected_bytes=art["bytes"],
                auth_header=f"Bearer {hf_token}" if hf_token else None)
        _step(f"baixando o {stt['label']}")
        _run([str(python), "-c",
              "import sys; from faster_whisper import download_model; download_model(sys.argv[1])",
              stt["model"]], timeout=3600)
        _step("ligando o servidor de voz")
        _write_service(root, python, build, tts, stt["model"], voice_key)
        for args in (("daemon-reload",), ("enable", UNIT), ("restart", UNIT)):
            done = _systemctl(*args)
            if done.returncode != 0:
                raise RuntimeError(
                    "esta máquina não deixou o jogo manter o serviço de voz ("
                    + (done.stderr or done.stdout).strip()[-200:]
                    + "). Ative com: sudo loginctl enable-linger " + (os.environ.get("USER") or ""))
        wanted = tts["artifacts"][0]["filename"]
        deadline = time.monotonic() + 240
        health = None
        while time.monotonic() < deadline:
            health = local_health()
            # stt_device vazio: o Whisper ainda está entrando na placa.
            if health and health.get("tts") == wanted and health.get("stt") == stt["model"] \
                    and health.get("stt_device"):
                break
            time.sleep(2)
        else:
            raise RuntimeError("o servidor de voz não respondeu depois de ligado")
        if activate:
            with db.session() as conn:
                settings_store._set_override(conn, "voice_runtime.placement", "local")
            _reset_clients()
        with _install_lock:
            _install.clear()
            _install.update({"state": "done", "tts": tts_id, "stt": stt_id,
                             "stt_device": health.get("stt_device")})
    except Exception as exc:  # a thread não tem quem receba a exceção
        with _install_lock:
            _install.clear()
            _install.update({"state": "error", "error": str(exc)[:400]})


def start_install(conn: sqlite3.Connection, activate: bool = False) -> dict:
    """Instala (ou troca os modelos de) o servidor de voz do jogo nesta máquina."""
    from app import model_library
    chosen = selection(conn)
    book = catalog()
    root = home()
    root.mkdir(parents=True, exist_ok=True)
    need = sum(int(art["bytes"]) for art in book["tts"][chosen["tts"]]["artifacts"]
               if not (root / "models" / art["filename"]).is_file())
    need += int(book["stt"][chosen["stt"]]["disk_gb"] * 1e9)
    if not (root / "venv").is_dir():
        need += 3 * 1024 ** 3
    if shutil.disk_usage(root).free < need + 2 * 1024 ** 3:
        raise GameError.bad_request("disk_full", "não há espaço em disco para a voz nesta máquina")
    for tool in ("git", "cmake", "ffmpeg"):
        if not shutil.which(tool):
            raise GameError.bad_request("tool_missing", f"falta o {tool} nesta máquina")
    with _install_lock:
        if _install.get("state") == "running":
            raise GameError.conflict("install_running", "a instalação da voz já está em andamento")
        _install.clear()
        _install.update({"state": "running", "step": "começando",
                         "tts": chosen["tts"], "stt": chosen["stt"]})
    threading.Thread(
        target=_run_install, daemon=True,
        args=(chosen["tts"], chosen["stt"], local_key() or secrets.token_urlsafe(24),
              model_library.download_keys(conn).get("HF_TOKEN") or "", activate)).start()
    return install_status()


# --------------------------------------------------------------------- tela
def view(conn: sqlite3.Connection) -> dict:
    book = catalog()
    chosen = selection(conn)
    health = local_health()
    configured = settings_store.configured_voice_settings(conn)
    colab_models = settings_store._get_override(conn, "voice_runtime.colab_models")
    return {
        **chosen,
        "bundle": bundle_of(chosen["tts"], chosen["stt"]),
        "bundles": book["bundles"],
        "tts_models": {name: {"label": item["label"], "note": item["note"],
                              "gb": round(sum(int(a["bytes"]) for a in item["artifacts"]) / 1e9, 1)}
                       for name, item in book["tts"].items()},
        "stt_models": {name: {"label": item["label"], "note": item["note"], "gb": item["disk_gb"]}
                       for name, item in book["stt"].items()},
        "server_url": str(configured.get("base_url") or ""),
        "local": {"running": bool(health), **_running_ids(health),
                  "stt_device": (health or {}).get("stt_device") or ""},
        "colab": {"serving": colab_serving(conn),
                  "models": colab_models if isinstance(colab_models, dict) else None},
        "install": install_status(),
    }
