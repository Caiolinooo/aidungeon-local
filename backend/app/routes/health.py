"""Router /api/health/llm — saúde do LLM em tempo real (sempre 200).

Além da sonda de /models: estado do llm_scheduler (breaker, fila, slots
ocupados), slot de cada role, o status.json do slot_watchdog, fila de jobs,
RAM/swap, VRAM e TTFT/tok/s das últimas narrativas. Com o servidor
respondendo, `status` vira "degraded" quando o breaker está aberto, quando o
watchdog vê /slots sem resposta ou um slot ocupado sem progresso há
`_STALLED_SLOT_S`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends

from app import db, llm_client, llm_scheduler, settings_store
from app.deps import get_http_transport

router = APIRouter()

MEMINFO_PATH = Path("/proc/meminfo")
_STALLED_SLOT_S = 30.0
# O timer do watchdog roda a cada 10 s; sem leitura em 60 s ele parou.
_WATCHDOG_STALE_S = 60.0
_GPU_TTL_S = 5.0

_gpu_lock = threading.Lock()
_gpu_cache: tuple[float, dict | None] = (0.0, None)


def _meminfo() -> dict | None:
    try:
        lines = MEMINFO_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    kb: dict[str, int] = {}
    for line in lines:
        key, _, rest = line.partition(":")
        fields = rest.split()
        if fields and fields[0].isdigit():
            kb[key] = int(fields[0])
    if "MemTotal" not in kb:
        return None
    mb = {key: value // 1024 for key, value in kb.items()}
    return {
        "ram_total_mb": mb["MemTotal"],
        "ram_available_mb": mb.get("MemAvailable", 0),
        "swap_total_mb": mb.get("SwapTotal", 0),
        "swap_used_mb": mb.get("SwapTotal", 0) - mb.get("SwapFree", 0),
    }


def _query_gpu() -> dict | None:
    exe = shutil.which("nvidia-smi")
    if exe is None:
        return None
    try:
        out = subprocess.run(
            [exe, "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=3, check=True).stdout
        used, total = (int(float(value)) for value in out.splitlines()[0].split(","))
    except (OSError, subprocess.SubprocessError, ValueError, IndexError):
        return None
    return {"vram_used_mb": used, "vram_total_mb": total}


def _gpu() -> dict | None:
    global _gpu_cache
    with _gpu_lock:
        expires, value = _gpu_cache
        now = time.monotonic()
        if now >= expires:
            value = _query_gpu()
            _gpu_cache = (now + _GPU_TTL_S, value)
        return value


def _memory() -> dict:
    return {**(_meminfo() or {}), **(_gpu() or {})}


def _age_s(iso: Any) -> float | None:
    """Segundos desde um timestamp ISO (UTC quando sem fuso); None se ilegível."""
    try:
        moment = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return round(time.time() - moment.timestamp(), 1)


def _seconds(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _watchdog(cfg: dict) -> dict | None:
    scheduler = cfg.get("scheduler") if isinstance(cfg.get("scheduler"), dict) else {}
    raw = str(scheduler.get("watchdog_status_path") or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(Path(raw).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    data["age_s"] = _age_s(data.get("checked_at"))
    data["stale"] = data["age_s"] is None or data["age_s"] > _WATCHDOG_STALE_S
    return data


def _jobs(conn: Any) -> dict:
    summary = db.job_queue_summary(conn)
    oldest = summary.pop("oldest_pending_at")
    summary["oldest_pending_s"] = _age_s(oldest) if oldest else None
    return summary


def _degradation(scheduler: dict, watchdog: dict | None) -> str | None:
    breaker = scheduler["breaker"]
    if breaker["state"] == "open":
        return (f"circuit breaker aberto até {breaker['until']} "
                f"({breaker['failures']} timeouts seguidos): auxiliares respondem llm_degraded")
    if watchdog is None or watchdog["stale"]:
        return None
    unreachable_s = _seconds(watchdog.get("unreachable_s"))
    if unreachable_s > 0:
        return f"/slots sem resposta há {unreachable_s:.0f}s"
    slots = watchdog.get("slots") if isinstance(watchdog.get("slots"), list) else []
    stalled = [(slot.get("id"), _seconds(slot.get("stalled_s")))
               for slot in slots if isinstance(slot, dict)]
    worst = max(stalled, key=lambda item: item[1], default=(None, 0.0))
    if worst[1] >= _STALLED_SLOT_S:
        return f"slot {worst[0]} sem progresso há {worst[1]:.0f}s"
    return None


@router.get("/health/llm")
def health_llm(transport: Any = Depends(get_http_transport)) -> dict:
    with db.session() as conn:
        cfg = settings_store.effective_llm_settings(conn)
        jobs = _jobs(conn)
    result = llm_client.health(cfg, transport=transport)
    scheduler = llm_scheduler.snapshot()
    watchdog = _watchdog(cfg)
    if result["status"] == "ok":
        slots = llm_client.slot_roles(cfg, transport)
        reason = _degradation(scheduler, watchdog)
        if reason:
            result["status"] = "degraded"
            result["details"] = reason
    else:
        slots = {"total": None, "roles": {}}
    result.update(
        scheduler=scheduler,
        slots={**slots, "watchdog": watchdog},
        jobs=jobs,
        memory=_memory(),
        narrative=llm_client.narrative_stats(),
    )
    return result
