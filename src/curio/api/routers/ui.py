"""Control-plane SPA serving under `/app/` with `/` redirect. No CORS.

The Vite-built React app lives in `frontend/dist` (or `FRONTEND_DIST_DIR`). Mounting it under
`/app/` ensures the SPA fallback never collides with root-level addon routes (`/{addon_token}/...`),
`/api/*`, or `/healthz` (ADR 0006).
"""

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse, Response

router = APIRouter(include_in_schema=False)

# Repo layout: <repo>/src/curio/api/routers/ui.py -> parents[4] is <repo>.
_DEFAULT_DIST_CANDIDATES: tuple[Path, ...] = (
    Path(__file__).resolve().parents[4] / "frontend" / "dist",
    Path("/app/frontend/dist"),
)


def _resolve_dist_dir(request: Request) -> Path | None:
    configured = getattr(request.app.state.settings, "frontend_dist_dir", None)
    if configured:
        candidate = Path(configured).resolve()
        return candidate if candidate.is_dir() else None
    for candidate in _DEFAULT_DIST_CANDIDATES:
        if candidate.is_dir():
            return candidate.resolve()
    return None


def _serve_spa(request: Request, full_path: str = "") -> Response:
    dist_dir = _resolve_dist_dir(request)
    if dist_dir is None or not (dist_dir / "index.html").is_file():
        return PlainTextResponse(
            "Frontend assets are not built. Run `cd frontend && npm run build`, "
            "or use the Vite dev server (`npm run dev`).",
            status_code=503,
        )

    if full_path:
        target = (dist_dir / full_path).resolve()
        if target.is_relative_to(dist_dir) and target.is_file():
            return FileResponse(target)

    return FileResponse(dist_dir / "index.html")


@router.get("/")
async def root_redirect() -> RedirectResponse:
    return RedirectResponse(url="/app/", status_code=307)


@router.get("/app")
async def app_trailing_slash_redirect() -> RedirectResponse:
    return RedirectResponse(url="/app/", status_code=307)


@router.get("/app/")
async def app_index(request: Request) -> Response:
    return _serve_spa(request)


@router.get("/app/{full_path:path}")
async def app_spa_fallback(request: Request, full_path: str) -> Response:
    return _serve_spa(request, full_path)
