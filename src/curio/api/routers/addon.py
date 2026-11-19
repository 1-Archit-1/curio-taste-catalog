"""Public Stremio/Nuvio addon routes (manifest, catalogs). Unauthenticated; CORS `*`.

Stremio/Nuvio cannot send auth headers, so users are addressed by a random,
revocable `addon_token` in the URL path. Full catalog slots arrive in #3.
"""

from collections.abc import Callable, Coroutine
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from curio import ADDON_NAME, __version__
from curio.api.deps import DbConn
from curio.services import users as users_service

_ADDON_CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, OPTIONS",
    "Access-Control-Allow-Headers": "*",
}


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


@router.options("/{addon_token}/{rest:path}")
async def addon_cors_preflight(addon_token: str, rest: str) -> Response:
    return Response(status_code=204)


@router.get("/{addon_token}/manifest.json")
async def addon_manifest(
    addon_token: str,
    conn: DbConn,
) -> JSONResponse:
    user = await users_service.resolve_user_by_addon_token(conn, addon_token=addon_token)
    if user is None:
        return JSONResponse({"detail": "Addon not found."}, status_code=404)

    return JSONResponse(
        {
            "id": "org.curio.taste-catalog",
            "version": __version__,
            "name": ADDON_NAME,
            "description": "AI-curated movie and series catalogs built from your own taste.",
            "resources": ["catalog"],
            "types": ["movie", "series"],
            "catalogs": [],
        }
    )
