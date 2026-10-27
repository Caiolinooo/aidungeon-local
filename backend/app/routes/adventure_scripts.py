"""Scripts por aventura (input, context, output).

GET devolve o texto salvo na aventura e de qual fonte o turno vai ler
(adventure, global, empty). PUT valida no sandbox atual e grava; não executa.
String vazia apaga a chave da aventura para `script:global:*` voltar a valer.

O owner monta este router em `app.main`:

    app.include_router(adventure_scripts.router, prefix="/api/adventures",
                       tags=["scripts"], dependencies=_protected)
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app import auth, db, scripting
from app.errors import GameError
from app.routes.adventures import _managed_adventure
from app.schemas import AdventureScriptsUpdate

router = APIRouter()


def _reject(exc: Exception) -> GameError:
    if isinstance(exc, scripting.SecurityViolation):
        return GameError(422, "script_rejected", str(exc))
    if isinstance(exc, SyntaxError):
        detail = exc.msg or "invalid syntax"
        return GameError(422, "script_rejected", f"syntax error: {detail}")
    return GameError(422, "script_rejected", str(exc))


@router.get("/{adventure_id}/scripts")
def get_scripts(adventure_id: str,
                info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    with db.session() as conn:
        _managed_adventure(conn, adventure_id, info.user)
    return scripting.read_adventure_scripts(adventure_id)


@router.put("/{adventure_id}/scripts")
def put_scripts(adventure_id: str, body: AdventureScriptsUpdate,
                info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    update = body.model_dump(exclude_unset=True)
    with db.session() as conn:
        _managed_adventure(conn, adventure_id, info.user)
    try:
        return scripting.save_adventure_scripts(adventure_id, update)
    except (scripting.SecurityViolation, SyntaxError, ValueError) as exc:
        raise _reject(exc) from exc
