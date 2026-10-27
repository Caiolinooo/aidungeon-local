"""Router /api/publications — catálogo, versões, like e remix.

Owner, em main.py, junto dos outros routers protegidos:

    from app.routes import publications as publication_routes
    publication_routes.register(app)

Ou, no mesmo estilo de scenarios:

    app.include_router(publication_routes.router, prefix="/api/publications",
                       tags=["publications"], dependencies=_protected)

As rotas já exigem Bearer. Models ficam neste arquivo, não em schemas.py.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, Field

from app import auth, db, publications

log = logging.getLogger("app.publications")

router = APIRouter()


class PublishRequest(BaseModel):
    kind: str = Field(min_length=1, max_length=20)
    source_id: str = Field(min_length=1, max_length=80)
    cover_image: str | None = Field(default=None, max_length=2000)


class RemixRequest(BaseModel):
    version: int | None = Field(default=None, ge=1)
    title: str | None = Field(default=None, max_length=200)


class VersionMeta(BaseModel):
    version: int
    content_hash: str
    cover_image: str = ""
    created_at: str


class PublicationSummary(BaseModel):
    id: str
    kind: str
    source_id: str
    owner: str
    title: str
    description: str = ""
    cover_image: str = ""
    tags: list[str] = []
    latest_version: int
    selected_version: int
    like_count: int = 0
    liked: bool = False
    content_hash: str = ""
    created_at: str
    updated_at: str


class PublicationDetail(PublicationSummary):
    created: bool = False
    snapshot: dict[str, Any]
    versions: list[VersionMeta]


class LikeResult(BaseModel):
    publication_id: str
    liked: bool
    likes: int


class RemixResult(BaseModel):
    scenario_id: str
    parent_publication_id: str
    parent_version: int
    title: str
    scenario: dict[str, Any]


class DeleteResult(BaseModel):
    deleted: bool
    id: str


def register(app) -> None:
    """Monta o router uma vez. O owner pode chamar isto em main.py."""
    for route in app.routes:
        path = getattr(route, "path", "") or ""
        if path.startswith("/api/publications"):
            return
    app.include_router(router, prefix="/api/publications", tags=["publications"])


@router.get("", response_model=list[PublicationSummary])
def list_publications(q: str | None = None, tag: str | None = None, sort: str = "new",
                      limit: int = Query(default=100, ge=1, le=200),
                      info: auth.AuthInfo = Depends(auth.require_auth)) -> list[dict]:
    with db.session() as conn:
        return publications.list_publications(
            conn, q=q, tag=tag, sort=sort, viewer=info.user, limit=limit,
        )


@router.post("", response_model=PublicationDetail)
def publish(body: PublishRequest, response: Response,
            info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    log.info("publications: %s publica %s %s", info.user, body.kind, body.source_id)
    with db.session() as conn:
        result = publications.publish(
            conn,
            actor=info.user,
            kind=body.kind,
            source_id=body.source_id,
            cover_image=body.cover_image,
        )
    response.status_code = 201 if result.get("created") else 200
    return result


@router.get("/{publication_id}", response_model=PublicationDetail)
def get_publication(publication_id: str,
                    info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    with db.session() as conn:
        return publications.get_publication(conn, publication_id, viewer=info.user)


@router.get("/{publication_id}/versions/{version}", response_model=PublicationDetail)
def get_publication_version(publication_id: str, version: int,
                            info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    with db.session() as conn:
        return publications.get_publication(
            conn, publication_id, viewer=info.user, version=version,
        )


@router.delete("/{publication_id}", response_model=DeleteResult)
def delete_publication(publication_id: str,
                       info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    with db.session() as conn:
        publications.delete_publication(conn, publication_id, actor=info.user)
    return {"deleted": True, "id": publication_id}


@router.post("/{publication_id}/like", response_model=LikeResult)
def like_publication(publication_id: str,
                     info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    with db.session() as conn:
        return publications.like_publication(conn, publication_id, actor=info.user)


@router.delete("/{publication_id}/like", response_model=LikeResult)
def unlike_publication(publication_id: str,
                       info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    with db.session() as conn:
        return publications.unlike_publication(conn, publication_id, actor=info.user)


@router.post("/{publication_id}/remix", response_model=RemixResult, status_code=201)
def remix_publication(publication_id: str, body: RemixRequest | None = None,
                      info: auth.AuthInfo = Depends(auth.require_auth)) -> dict:
    payload = body or RemixRequest()
    with db.session() as conn:
        return publications.remix(
            conn,
            publication_id,
            actor=info.user,
            version=payload.version,
            title=payload.title,
        )
