"""Servidor de voz do jogo: TTS, clonagem e STT, compatível com a API da OpenAI.

Um arquivo só, sem depender do resto do jogo: sobe igual nesta máquina e no
Colab. Os modelos vêm do ambiente, e é isso que a tela de Runtime escolhe.

- STT: faster-whisper, qualquer tamanho (`VOICE_STT_MODEL`: tiny, base, small,
  medium, large-v3, large-v3-turbo ou um repositório CTranslate2 do Hub).
- TTS e clonagem: OmniVoice pelo omnivoice.cpp, em qualquer quantização
  (`OMNI_MODEL` e `OMNI_CODEC` apontam os GGUF).

Rotas que o jogo usa: POST /v1/audio/speech, POST /v1/audio/transcriptions,
POST|PUT|DELETE /v1/audio/voices, GET /v1/models e GET /health.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import wave
from pathlib import Path
from typing import Optional

import uvicorn
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

HERE = Path(__file__).resolve().parent
API_KEY = os.environ.get("VOICE_API_KEY", "")
STT_MODEL = os.environ.get("VOICE_STT_MODEL", "base")
OMNI_BIN = os.environ.get("OMNI_BIN", "omnivoice-tts")
OMNI_CODEC_BIN = os.environ.get("OMNI_CODEC_BIN", "omnivoice-codec")
OMNI_MODEL = os.environ.get("OMNI_MODEL", "")
OMNI_CODEC = os.environ.get("OMNI_CODEC", "")
VOICES = Path(os.environ.get("VOICE_DIR", HERE / "voices"))
CUSTOM = Path(os.environ.get("VOICE_CUSTOM_DIR", VOICES / "custom"))
FIXED = ("M1", "M2", "M3", "F1", "F2", "F3")
TIMEOUT_S = float(os.environ.get("OMNI_TIMEOUT_S", "120"))
_CV_ID = re.compile(r"^cv_[0-9a-f]{8}$")

app = FastAPI(title="aidungeon-voice")
# O omnivoice.cpp carrega o modelo a cada chamada: uma por vez na placa.
_omni_lock = threading.Lock()
_stt_lock = threading.Lock()
_stt = None
_stt_device = ""


def _cuda_lib_dirs() -> list[str]:
    """Pastas do cuBLAS e do cuDNN instalados pelo pip: o Whisper na GPU precisa delas."""
    import importlib.util
    found = []
    for name in ("nvidia.cublas", "nvidia.cudnn"):
        try:
            spec = importlib.util.find_spec(name)
        except (ImportError, ValueError):
            spec = None
        for base in (spec.submodule_search_locations or []) if spec else []:
            lib = os.path.join(base, "lib")
            if os.path.isdir(lib):
                found.append(lib)
    return found


def _child_env(**extra: str) -> dict:
    """O omnivoice.cpp foi compilado com o CUDA do sistema: roda sem as
    bibliotecas do pip no caminho."""
    env = dict(os.environ)
    if "VOICE_ORIG_LD" in env:
        env["LD_LIBRARY_PATH"] = env["VOICE_ORIG_LD"]
    env.update(extra)
    return env


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": {"code": code, "message": message}})


@app.middleware("http")
async def require_key(request: Request, call_next):
    if API_KEY and request.url.path.startswith("/v1"):
        if request.headers.get("authorization", "") != f"Bearer {API_KEY}":
            return _error(401, "unauthorized", "chave de voz ausente ou errada")
    return await call_next(request)


def _whisper():
    """Carrega na primeira transcrição: GPU em float16 e, sem GPU, CPU em int8."""
    global _stt, _stt_device
    with _stt_lock:
        if _stt is None:
            from faster_whisper import WhisperModel
            try:
                _stt = WhisperModel(STT_MODEL, device="cuda", compute_type="float16")
                _stt_device = "cuda"
            except Exception:
                _stt = WhisperModel(STT_MODEL, device="cpu", compute_type="int8")
                _stt_device = "cpu"
        return _stt


@app.on_event("startup")
async def _preload():
    # Carrega (e baixa, na primeira vez) o Whisper sem segurar a subida:
    # a primeira transcrição não paga essa espera.
    threading.Thread(target=lambda: _quiet(_whisper), daemon=True).start()


def _quiet(work) -> None:
    try:
        work()
    except Exception:
        pass


def _voice_files(voice: str) -> tuple[Path, Path]:
    if voice in FIXED:
        return VOICES / f"{voice}.rvq", VOICES / "ref.txt"
    if not _CV_ID.match(voice or ""):
        raise LookupError(f"voz inválida: {voice}")
    return CUSTOM / f"{voice}.rvq", CUSTOM / f"{voice}.txt"


def _omni_args(voice: str, out: str) -> list[str]:
    rvq, ref = _voice_files(voice)
    if not rvq.is_file() or not ref.is_file():
        # 404: o jogo reenvia a voz clonada e tenta de novo.
        raise LookupError(f"referencia ausente: {voice}")
    return [OMNI_BIN, "--model", OMNI_MODEL, "--codec", OMNI_CODEC, "--lang", "Portuguese",
            "--ref-rvq", str(rvq), "--ref-text", str(ref), "--seed", "42", "-o", out]


def _synthesize(text: str, voice: str) -> bytes:
    """WAV inteiro (24 kHz mono, 16 bits)."""
    with tempfile.TemporaryDirectory() as folder:
        out = os.path.join(folder, "out.wav")
        args = _omni_args(voice, out)
        with _omni_lock:
            proc = subprocess.run(args, input=text.encode(), capture_output=True,
                                  timeout=TIMEOUT_S, env=_child_env())
        if proc.returncode != 0 or not os.path.isfile(out) or not os.path.getsize(out):
            raise RuntimeError("omnivoice falhou: " + proc.stderr.decode(errors="replace")[-300:])
        return Path(out).read_bytes()


async def _stream(text: str, voice: str):
    """WAV progressivo: o primeiro áudio sai antes de a frase toda ficar pronta."""
    args = _omni_args(voice, "-")
    loop = asyncio.get_running_loop()
    deadline = loop.time() + TIMEOUT_S
    proc = None
    try:
        with _omni_lock:
            proc = subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, env=_child_env())
            proc.stdin.write(text.encode())
            proc.stdin.close()
            while True:
                left = deadline - loop.time()
                if left <= 0:
                    raise RuntimeError("omnivoice demorou demais")
                # read1 entrega o que já existe; read(n) seguraria o áudio até encher o buffer.
                chunk = await asyncio.wait_for(asyncio.to_thread(proc.stdout.read1, 65536), left)
                if not chunk:
                    break
                yield chunk
    finally:
        # Um filho abandonado segura a placa e a trava: sempre morre aqui.
        if proc is not None and proc.poll() is None:
            proc.kill()
            await asyncio.to_thread(proc.wait)


def _pcm(wav: bytes) -> bytes:
    with tempfile.NamedTemporaryFile(suffix=".wav") as handle:
        handle.write(wav)
        handle.flush()
        with wave.open(handle.name, "rb") as reader:
            return reader.readframes(reader.getnframes())


def _mp3(wav: bytes) -> bytes:
    proc = subprocess.run(["ffmpeg", "-y", "-i", "-", "-codec:a", "libmp3lame", "-b:a", "64k",
                           "-f", "mp3", "-"], input=wav, capture_output=True, timeout=60)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg não converteu para mp3")
    return proc.stdout


class Speech(BaseModel):
    input: str
    model: Optional[str] = "omnivoice"
    voice: Optional[str] = "M1"
    response_format: Optional[str] = "mp3"
    instructions: Optional[str] = None


@app.get("/health")
async def health():
    # stt_device vazio: o Whisper ainda está carregando (ou baixando).
    return {"status": "ok", "stt": STT_MODEL, "stt_device": _stt_device,
            "tts": Path(OMNI_MODEL).name, "codec": Path(OMNI_CODEC).name,
            "voices": list(FIXED)}


@app.get("/v1/models")
async def models():
    return {"object": "list", "data": [{"id": "omnivoice", "object": "model"},
                                       {"id": "whisper-1", "object": "model"}]}


@app.post("/v1/audio/speech")
async def speech(req: Speech):
    text = (req.input or "").strip()
    if not text:
        return _error(400, "empty_input", "texto vazio")
    voice = (req.voice or "M1").strip() or "M1"
    try:
        if req.response_format == "wav":
            _omni_args(voice, "-")  # voz ausente vira 404 antes de abrir o stream
            return StreamingResponse(_stream(text, voice), media_type="audio/wav")
        wav = await asyncio.to_thread(_synthesize, text, voice)
        if req.response_format == "mp3":
            return Response(await asyncio.to_thread(_mp3, wav), media_type="audio/mpeg")
        return Response(_pcm(wav), media_type="audio/pcm")
    except LookupError as exc:
        return _error(404, "voice_not_found", str(exc))
    except Exception as exc:
        return _error(500, "tts_failed", str(exc)[:300])


@app.post("/v1/audio/transcriptions")
async def transcribe(file: UploadFile = File(...), model: Optional[str] = Form(None),
                     language: Optional[str] = Form(None)):
    raw = await file.read()
    if not raw or len(raw) > 25 * 1024 * 1024:
        return _error(400, "invalid_audio", "áudio vazio ou maior que 25 MB")
    suffix = os.path.splitext(file.filename or "audio.wav")[1] or ".wav"

    def work() -> str:
        with tempfile.NamedTemporaryFile(suffix=suffix) as handle:
            handle.write(raw)
            handle.flush()
            segments, _info = _whisper().transcribe(
                handle.name, language=language or "pt", beam_size=5, vad_filter=True)
            return " ".join(segment.text.strip() for segment in segments)

    try:
        return {"text": await asyncio.to_thread(work)}
    except Exception as exc:
        return _error(500, "stt_failed", str(exc)[:300])


@app.post("/v1/audio/voices")
async def clone(voice_id: str = Form(...), name: str = Form(...), ref_text: str = Form(...),
                file: UploadFile = File(...)):
    """Grava uma voz clonada: o áudio vira os códigos que o OmniVoice usa de referência."""
    ref_text = (ref_text or "").strip()
    raw = await file.read()
    if not _CV_ID.match(voice_id or "") or not raw or not ref_text or len(ref_text) > 2000:
        return _error(400, "invalid_audio", "voz, áudio ou texto de referência inválido")

    def work():
        with tempfile.TemporaryDirectory() as folder:
            source, clip = os.path.join(folder, "in.bin"), os.path.join(folder, "clip.wav")
            Path(source).write_bytes(raw)
            conv = subprocess.run(["ffmpeg", "-y", "-i", source, "-ar", "24000", "-ac", "1", clip],
                                  capture_output=True, timeout=60)
            if conv.returncode != 0 or not os.path.isfile(clip):
                return _error(400, "invalid_audio", "ffmpeg não leu o áudio")
            with wave.open(clip, "rb") as reader:
                seconds = reader.getnframes() / float(reader.getframerate() or 24000)
            if seconds < 1.0:
                return _error(400, "audio_too_short", "áudio menor que 1 segundo")
            if seconds > 30.0:
                return _error(400, "audio_too_long", "áudio maior que 30 segundos")
            # O codificador aborta na GPU (visto nesta build, com Q8 e BF16) e
            # leva 1 s na CPU: roda sempre com a placa escondida.
            with _omni_lock:
                enc = subprocess.run([OMNI_CODEC_BIN, "--model", OMNI_CODEC, "-i", clip],
                                     capture_output=True, timeout=180,
                                     env=_child_env(CUDA_VISIBLE_DEVICES=""))
            rvq = os.path.join(folder, "clip.rvq")
            if enc.returncode != 0 or not os.path.isfile(rvq):
                return _error(500, "encode_failed", "omnivoice-codec falhou")
            CUSTOM.mkdir(parents=True, exist_ok=True)
            shutil.move(rvq, CUSTOM / f"{voice_id}.rvq")
            (CUSTOM / f"{voice_id}.txt").write_text(ref_text, encoding="utf-8")
            codes = (CUSTOM / f"{voice_id}.rvq").read_bytes()
            return {"id": voice_id, "name": name, "ref_text": ref_text,
                    "seconds": round(seconds, 3),
                    "rvq_b64": base64.b64encode(codes).decode("ascii")}

    return await asyncio.to_thread(work)


class Codes(BaseModel):
    ref_text: str
    rvq_b64: str


@app.put("/v1/audio/voices/{voice_id}")
async def put_voice(voice_id: str, body: Codes):
    """O jogo guarda os códigos de cada voz clonada e os repõe num servidor novo."""
    try:
        codes = base64.b64decode(body.rvq_b64, validate=True)
    except Exception:
        codes = b""
    if not _CV_ID.match(voice_id or "") or not codes or not body.ref_text.strip():
        return _error(400, "invalid_audio", "voz ou códigos inválidos")
    CUSTOM.mkdir(parents=True, exist_ok=True)
    (CUSTOM / f"{voice_id}.rvq").write_bytes(codes)
    (CUSTOM / f"{voice_id}.txt").write_text(body.ref_text.strip(), encoding="utf-8")
    return {"id": voice_id}


@app.delete("/v1/audio/voices/{voice_id}")
async def delete_voice(voice_id: str):
    if not _CV_ID.match(voice_id or ""):
        return _error(400, "invalid_audio", "voz inválida")
    for ext in (".rvq", ".txt"):
        (CUSTOM / f"{voice_id}{ext}").unlink(missing_ok=True)
    return {"id": voice_id}


if __name__ == "__main__":
    # O carregador só lê LD_LIBRARY_PATH na largada: com cuBLAS e cuDNN do pip
    # fora do caminho, o processo se relança uma vez já com eles.
    current = [part for part in os.environ.get("LD_LIBRARY_PATH", "").split(":") if part]
    extra = [lib for lib in _cuda_lib_dirs() if lib not in current]
    if extra and "VOICE_ORIG_LD" not in os.environ:
        os.environ["VOICE_ORIG_LD"] = ":".join(current)
        os.environ["LD_LIBRARY_PATH"] = ":".join(extra + current)
        os.execv(sys.executable, [sys.executable, os.path.abspath(__file__)])
    uvicorn.run(app, host=os.environ.get("VOICE_HOST", "127.0.0.1"),
                port=int(os.environ.get("VOICE_PORT", "8011")))
