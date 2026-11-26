"""Estimativa de memória e velocidade de cada modelo numa GPU do Colab.

Serve para escolher a placa antes de gastar créditos. São contas aproximadas
com números públicos das placas, não medições: a tela diz "estimativa". A
VRAM de cada placa é trocada pelo valor real na primeira sessão que sobe nela.
"""

from __future__ import annotations

import re

# VRAM em GiB, banda de memória em GB/s (dita a velocidade de um LLM) e
# velocidade de difusão em relação à T4. Placa fora da tabela fica sem estimativa.
GPUS = {
    "T4": {"vram_gb": 15.0, "bandwidth": 320, "speed": 1.0},
    "L4": {"vram_gb": 22.5, "bandwidth": 300, "speed": 1.8},
    "A100": {"vram_gb": 40.0, "bandwidth": 1555, "speed": 5.5},
    "H100": {"vram_gb": 80.0, "bandwidth": 3350, "speed": 9.0},
}
# Bits por peso de cada quantização, pelo nome do arquivo.
_BITS = (("bf16", 16.0), ("f16", 16.0), ("q8", 8.5), ("fp8", 8.0), ("int8", 8.0),
         ("q6", 6.6), ("q5", 5.7), ("q4", 4.85), ("q3", 3.9), ("iq2", 2.7), ("q2", 3.0))
# KV em q8, por token e por bilhão de parâmetros. Varia com a arquitetura
# (de uns 4 KB a 8 KB); 5 KB acerta a ordem de grandeza.
_KV_KB_PER_TOKEN_PER_B = 5.0
# Segundos numa T4, com o codificador de texto na GPU. Sem medição própria.
_IMAGE_SECONDS_T4 = {"flux2_klein": 14, "z_image_turbo": 40, "qwen": 60, "checkpoint": 26}
_VIDEO_SECONDS_T4 = {"ltxv": 80, "wan": 420, "h3": 260}
RATINGS = ("excelente", "bom", "ruim", "pessimo")
# Voz. O OmniVoice carrega o modelo a cada fala: o tempo é carregar (por GB)
# mais gerar (que escala com a placa). Calibrado numa L4.
_TTS_LOAD_S_PER_GB = 1.5
_TTS_SPEECH_S_T4 = 9.0


def _bits(filename: str) -> float:
    name = filename.lower()
    for token, bits in _BITS:
        if re.search(rf"(?<![a-z0-9]){token}(?![0-9])", name):
            return bits
    return 8.0


def _main_bytes(entry: dict, roles: tuple[str, ...]) -> tuple[int, str]:
    for art in entry.get("artifacts") or []:
        if art.get("role") in roles and art.get("bytes"):
            return int(art["bytes"]), str(art.get("filename") or "")
    return 0, ""


def _rate(value: float, steps: tuple[float, float, float], *, higher_is_better: bool) -> str:
    for rating, limit in zip(RATINGS, steps):
        if (value >= limit) if higher_is_better else (value <= limit):
            return rating
    return "pessimo"


def narrator(entry: dict, context: int, gpu: dict) -> dict:
    size, filename = _main_bytes(entry, ("weights",))
    if not size:
        return {"rating": None, "note": "arquivo sem tamanho publicado: sem estimativa"}
    bits = _bits(filename)
    params_b = size * 8 / bits / 1e9
    kv_gb = _KV_KB_PER_TOKEN_PER_B * params_b * context / 1e6
    need = size / 1e9 * 1.03 + kv_gb + 1.0
    tok_s = 0.55 * gpu["bandwidth"] / (size / 1e9)
    rating = _rate(tok_s, (35, 18, 8), higher_is_better=True)
    quality = ("qualidade máxima" if bits >= 6.5 else "boa qualidade" if bits >= 4.5
               else "qualidade reduzida pela quantização")
    if bits < 4.5 and rating in ("excelente", "bom"):
        rating = RATINGS[RATINGS.index(rating) + 1]
    return {"need_gb": round(need, 1), "speed": f"cerca de {tok_s:.0f} tokens por segundo",
            "rating": rating, "note": quality}


def _diffusion(entry: dict, gpu: dict, table: dict, unit: str,
               steps: tuple[float, float, float]) -> dict:
    size, _name = _main_bytes(entry, ("unet", "checkpoint"))
    floor = entry.get("min_vram_mib")
    need = float(floor) / 1024 if floor else (size / 1e9 * 1.1 + 2.5 if size else 0.0)
    base = table.get(str(entry.get("engine")))
    out = {"need_gb": round(need, 1) if need else None, "note": ""}
    if base is None:
        return {**out, "rating": None, "speed": "", "note": "fluxo próprio: sem estimativa de tempo"}
    seconds = base / gpu["speed"]
    return {**out, "speed": f"cerca de {seconds:.0f} s por {unit}",
            "rating": _rate(seconds, steps, higher_is_better=False)}


def _worse(*ratings: str) -> str:
    return max(ratings, key=RATINGS.index)


def voice_tts(entry: dict, gpu: dict) -> dict:
    """Fala e clonagem: o mesmo modelo faz as duas."""
    size = sum(int(art.get("bytes") or 0) for art in entry.get("artifacts") or []) / 1e9
    seconds = size * _TTS_LOAD_S_PER_GB + _TTS_SPEECH_S_T4 / gpu["speed"]
    rating = _worse(_rate(seconds, (5, 9, 16), higher_is_better=False),
                    str(entry.get("quality") or "bom"))
    return {"need_gb": round(size * 1.15 + 0.5, 1),
            "speed": f"cerca de {seconds:.0f} s por fala curta",
            "rating": rating, "note": str(entry.get("note") or "")}


def voice_stt(entry: dict, gpu: dict) -> dict:
    seconds = float(entry.get("t4_seconds") or 1.0) / gpu["speed"]
    rating = _worse(_rate(seconds, (1.0, 2.5, 5.0), higher_is_better=False),
                    str(entry.get("quality") or "bom"))
    return {"need_gb": float(entry.get("vram_gb") or 0),
            "speed": f"cerca de {seconds:.1f} s para ouvir 10 s de fala".replace(".", ","),
            "rating": rating, "note": str(entry.get("note") or "")}


def for_gpu(accelerator: str, vram_gb: float | None, config: dict, catalog: dict,
            context: int, voice: dict | None = None) -> dict:
    """O que cada modelo escolhido pede desta placa e como deve se sair nela.

    Só entram os serviços que a configuração põe no Colab. Narrador, ComfyUI
    e voz dividem a mesma placa; imagem e vídeo não rodam juntos. `voice`
    traz as entradas de TTS e STT quando a voz vai para o Colab.
    """
    known = GPUS.get(accelerator)
    measured = vram_gb is not None
    if known is None and not measured:
        return {"accelerator": accelerator, "vram_gb": None, "items": [], "fits": None,
                "rating": None, "note": "placa sem dados para estimar"}
    gpu = {**(known or {"bandwidth": 0, "speed": 0}), **({"vram_gb": vram_gb} if measured else {})}
    models, placements = catalog["models"], config["placements"]
    items, total, diffusion = [], 0.0, 0.0
    if placements.get("llm") == "colab":
        entry = models["llm"].get(config["models"]["llm"]) or {}
        item = narrator(entry, context, gpu) if known else {"rating": None, "note": "sem estimativa"}
        items.append({"kind": "llm", **item})
        total += item.get("need_gb") or 0
    if placements.get("comfyui") == "colab":
        for kind, table, unit, steps in (("image", _IMAGE_SECONDS_T4, "imagem", (10, 25, 60)),
                                         ("video", _VIDEO_SECONDS_T4, "clipe", (90, 240, 600))):
            entry = models[kind].get(config["models"][kind]) or {}
            item = (_diffusion(entry, gpu, table, unit, steps) if known
                    else {"rating": None, "note": "sem estimativa"})
            items.append({"kind": kind, **item})
            diffusion = max(diffusion, item.get("need_gb") or 0)
    total += diffusion
    if voice:
        for kind, work in (("tts", voice_tts), ("stt", voice_stt)):
            item = (work(voice[kind], gpu) if known
                    else {"rating": None, "note": "sem estimativa"})
            items.append({"kind": kind, **item})
            total += item.get("need_gb") or 0
    fits = total <= gpu["vram_gb"] if items else None
    rated = [item["rating"] for item in items if item.get("rating")]
    worst = max(rated, key=RATINGS.index) if rated else None
    return {
        "accelerator": accelerator, "vram_gb": round(gpu["vram_gb"], 1), "vram_measured": measured,
        "items": items, "total_gb": round(total, 1), "fits": fits,
        # Não cabendo, nada do resto importa.
        "rating": "pessimo" if fits is False else worst,
        "note": "não cabe na memória desta placa" if fits is False else "",
    }
