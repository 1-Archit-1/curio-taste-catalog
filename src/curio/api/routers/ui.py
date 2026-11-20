"""Control-plane web UI shell (login screen + Settings tab). No CORS."""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(include_in_schema=False)

_INDEX_HTML = (Path(__file__).resolve().parents[2] / "ui" / "index.html").read_text(
    encoding="utf-8"
)


@router.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    return HTMLResponse(content=_INDEX_HTML)
