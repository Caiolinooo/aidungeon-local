"""Router do pipeline assíncrono de assets (v0.3.0).

- POST /adventures/{aid}/portraits              → um job por NPC (character cards)
- POST /adventures/{aid}/cards/{cid}/portrait   → (re)gera retrato de um NPC → 202 Job
- GET  /adventures/{aid}/jobs[?status=]         → fila (polling da UI)
- DELETE /adventures/{aid}/jobs/{jid}           → cancela job que ainda não roda → Job
- GET  /jobs/{jid}                              → polling do See assíncrono (C6), `by_id`

A geração em si roda no worker (app/jobs.py); estes endpoints só enfileiram.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query

from app import access, auth, db, jobs
from app.errors import GameError

router = APIRouter()
by_id = APIRouter()


def _job_or_404(conn, adventure_id: str, job_id: str) -> dict:
    job = db.get_job(conn, job_id)
    if not job or job["adventure_id"] != adventure_id:
        raise GameError.not_found(f"job {job_id} not found")
    return job


@router.post("/{adventure_id}/portraits", status_code=202)
def enqueue_all_portraits(adventure_id: str) -> dict:
    with db.session() as conn:
        if not db.get_adventure(conn, adventure_id):
            raise GameError.not_found(f"adventure {adventure_id} not found")
        return {"jobs": jobs.enqueue_all_portraits(conn, adventure_id)}


@router.post("/{adventure_id}/cards/{card_id}/portrait", status_code=202)
def enqueue_portrait(adventure_id: str, card_id: str) -> dict:
    with db.session() as conn:
        card = db.get_card(conn, adventure_id, card_id)
        if not card:
            raise GameError.not_found(f"card {card_id} not found")
        return jobs.enqueue_portrait(conn, adventure_id, card_id, replace=True)


@router.get("/{adventure_id}/jobs")
def list_jobs(adventure_id: str,
              status: str | None = Query(default=None)) -> list[dict]:
    with db.session() as conn:
        if not db.get_adventure(conn, adventure_id):
            raise GameError.not_found(f"adventure {adventure_id} not found")
        return db.list_jobs(conn, adventure_id, status=status)


@router.get("/{adventure_id}/jobs/{job_id}")
def get_job(adventure_id: str, job_id: str) -> dict:
    with db.session() as conn:
        return _job_or_404(conn, adventure_id, job_id)


@router.delete("/{adventure_id}/jobs/{job_id}")
def cancel_job(adventure_id: str, job_id: str) -> dict:
    with db.session() as conn:
        job = _job_or_404(conn, adventure_id, job_id)
        if job["status"] in ("preparing", "pending"):
            job = jobs.cancel(conn, job_id) or db.get_job(conn, job_id)
        elif job["status"] in ("done", "error", "canceled"):
            raise GameError(409, "job_not_cancelable",
                            f"job already finished with status '{job['status']}'")
        # running: devolve como está (cancelamento cooperativo — v1)
        return job


@by_id.get("/jobs/{job_id}")
def get_job_by_id(job_id: str,
                  info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    """Job de história de outra pessoa responde 404, como um id que não existe."""
    with db.session() as conn:
        job = db.get_job(conn, job_id)
        if not job or not access.adventure_visible(conn, info.user, job["adventure_id"]):
            raise GameError.not_found(f"job {job_id} not found")
        return job
