"""Public Stremio/Nuvio addon routes (manifest, catalogs, assets). Unauthenticated; CORS `*`.

Stremio/Nuvio cannot send auth headers, so users are addressed by a random,
revocable `addon_token` in the URL path.
"""

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from curio import ADDON_NAME, __version__
from curio.api.deps import DbSession
from curio.services import catalogs as catalogs_service
from curio.services import users as users_service

_ADDON_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "*",
}

_NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
}

_POSTER_FALLBACK_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 300 450" '
    'width="300" height="450" role="img" aria-label="Curio poster fallback">'
    '<rect width="300" height="450" fill="#141418"/>'
    '<rect x="16" y="16" width="268" height="418" rx="12" fill="none" '
    'stroke="#2e2e38" stroke-width="2"/>'
    '<text x="150" y="220" text-anchor="middle" fill="#e4e4ec" '
    'font-family="system-ui, -apple-system, sans-serif" font-size="28" '
    'font-weight="700" letter-spacing="2">CURIO</text>'
    '<text x="150" y="250" text-anchor="middle" fill="#8e8e9e" '
    'font-family="system-ui, -apple-system, sans-serif" font-size="13">'
    "Taste Catalog</text>"
    "</svg>"
)


class AddonCorsRoute(APIRoute):
    """Attach permissive CORS headers to every addon route response (and only addon routes)."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        original_handler = super().get_route_handler()

        async def handler(request: Request) -> Response:
            try:
                response = await original_handler(request)
            except HTTPException as exc:
                response = JSONResponse(
                    {"detail": exc.detail},
                    status_code=exc.status_code,
                    headers=exc.headers,
                )
            for k, v in _ADDON_CORS_HEADERS.items():
                response.headers[k] = v
            return response

        return handler


router = APIRouter(tags=["addon"], route_class=AddonCorsRoute)


@router.get("/addon-assets/poster-fallback.svg")
async def addon_poster_fallback() -> Response:
    return Response(
        content=_POSTER_FALLBACK_SVG,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@router.options("/{addon_token}/{rest:path}")
async def addon_cors_preflight(addon_token: str, rest: str) -> Response:
    return Response(status_code=204)


@router.get("/{addon_token}/manifest.json")
async def addon_manifest(
    addon_token: str,
    session: DbSession,
) -> JSONResponse:
    user = await users_service.resolve_user_by_addon_token(session, addon_token=addon_token)
    if user is None:
        return JSONResponse({"detail": "Addon not found."}, status_code=404)

    catalogs = [
        {
            "type": slot.media_type,
            "id": slot.slot_id,
            "name": slot.name,
            "extra": [{"name": "skip"}],
        }
        for slot in catalogs_service.FIXED_SLOTS
    ]

    return JSONResponse(
        {
            "id": "org.curio.taste-catalog",
            "version": __version__,
            "name": ADDON_NAME,
            "description": "AI-curated movie and series catalogs built from your own taste.",
            "idPrefixes": ["tt"],
            "types": ["movie", "series"],
            "resources": ["catalog"],
            "catalogs": catalogs,
        },
        headers=_NO_CACHE_HEADERS,
    )


async def _serve_catalog(
    request: Request,
    session: DbSession,
    *,
    addon_token: str,
    media_type: str,
    catalog_id: str,
    extra: str | None = None,
    query_skip: int | None = None,
) -> JSONResponse:
    user = await users_service.resolve_user_by_addon_token(session, addon_token=addon_token)
    if user is None:
        return JSONResponse({"detail": "Addon not found."}, status_code=404)

    slot_def = catalogs_service.FIXED_SLOTS_BY_ID.get(catalog_id)
    if slot_def is None or slot_def.media_type != media_type:
        return JSONResponse({"detail": "Catalog not found."}, status_code=404)

    try:
        resolved_skip = catalogs_service.parse_skip_extra(extra, query_skip=query_skip)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    items = await catalogs_service.query_slot_catalog(
        session,
        user_id=user.id,
        slot_id=slot_def.slot_id,
        skip=resolved_skip,
        limit=20,
    )
    base_url = str(request.base_url)
    metas = [
        {
            "id": item.imdb_id,
            "type": item.media_type,
            "name": item.name,
            "poster": catalogs_service.resolve_poster_url(item.poster_url, base_url=base_url),
            "description": item.blurb or item.description or "",
        }
        for item in items
    ]
    return JSONResponse({"metas": metas}, headers=_NO_CACHE_HEADERS)


@router.get("/{addon_token}/catalog/{media_type}/{catalog_id}.json")
async def addon_catalog(
    request: Request,
    addon_token: str,
    media_type: str,
    catalog_id: str,
    session: DbSession,
    skip: int | None = None,
) -> JSONResponse:
    return await _serve_catalog(
        request,
        session,
        addon_token=addon_token,
        media_type=media_type,
        catalog_id=catalog_id,
        extra=None,
        query_skip=skip,
    )


@router.get("/{addon_token}/catalog/{media_type}/{catalog_id}/{extra}.json")
async def addon_catalog_extra(
    request: Request,
    addon_token: str,
    media_type: str,
    catalog_id: str,
    extra: str,
    session: DbSession,
    skip: int | None = None,
) -> JSONResponse:
    return await _serve_catalog(
        request,
        session,
        addon_token=addon_token,
        media_type=media_type,
        catalog_id=catalog_id,
        extra=extra,
        query_skip=skip,
    )
