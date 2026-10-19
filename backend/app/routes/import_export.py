"""Router import/export — backup do AI Dungeon (.zip) com round-trip."""

from __future__ import annotations

import json
import shutil
import tempfile
import urllib.parse
from pathlib import Path

from fastapi import APIRouter, Depends, File, Response, UploadFile

import exporter
import importer
from app import auth
from app.errors import GameError

router = APIRouter()


@router.post("/import/aidungeon", status_code=201)
def import_aidungeon(file: UploadFile = File(...),
                     cards_file: UploadFile | None = File(default=None),
                     info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    """Importa o .zip do backup oficial (metadata.json + actions-*.json).

    Um JSON solto de story cards pode acompanhar o zip (`cards_file`,
    opcional) para merge/validação.
    """
    if not file.filename or not file.filename.lower().endswith(".zip"):
        raise GameError.bad_request("invalid_backup", "expected a .zip backup file")

    tmp = tempfile.mkdtemp(prefix="aidungeon_upload_")
    try:
        zip_path = Path(tmp) / "backup.zip"
        with open(zip_path, "wb") as fh:
            shutil.copyfileobj(file.file, fh)

        cards_payload = None
        if cards_file is not None and cards_file.filename:
            try:
                cards_payload = json.loads(cards_file.file.read().decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                raise GameError.bad_request("invalid_backup",
                                            "cards_file is not valid JSON") from exc
        try:
            return importer.import_zip(zip_path, cards_payload=cards_payload,
                                        owner=info.user)
        except importer.BackupFormatError as exc:
            raise GameError.bad_request("invalid_backup", str(exc)) from exc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@router.get("/adventures/{adventure_id}/export")
def export_adventure(adventure_id: str) -> Response:
    """Exporta a aventura NO MESMO FORMATO do backup (metadata + actions-001)."""
    data, title = exporter.export_zip(adventure_id)
    quoted = urllib.parse.quote(f"{title}-backup.zip")
    headers = {
        "Content-Disposition": (
            f"attachment; filename=\"adventure-backup.zip\"; filename*=UTF-8''{quoted}"
        )
    }
    return Response(content=data, media_type="application/zip", headers=headers)
