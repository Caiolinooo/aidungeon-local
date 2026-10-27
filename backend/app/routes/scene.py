"""Router POST /api/adventures/{id}/scene — fundo e elenco da cena atual.

Lê as últimas ações e os cards, devolve lugar/clima/elenco e enfileira a
pintura do fundo (e retratos que faltam). A imagem em si sai pelo worker
de jobs; a UI faz polling neste mesmo endpoint.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app import auth, db, scene
from app.errors import GameError
from app.schemas import SceneSyncRequest

router = APIRouter()


@router.post("/{adventure_id}/scene")
def post_scene(adventure_id: str, body: SceneSyncRequest | None = None,
               info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    retry = bool(body and body.retry)
    with db.session() as conn:
        if not db.get_adventure(conn, adventure_id):
            raise GameError.not_found(f"adventure {adventure_id} not found")
        return scene.sync_scene(
            conn, adventure_id, retry=retry, viewer=info.user)
