from fastapi import APIRouter
from fastapi.responses import JSONResponse

from curio.api.deps import DbSession
from curio.services import health as health_service

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz(session: DbSession) -> JSONResponse:
    if await health_service.database_ok(session):
        return JSONResponse({"status": "ok", "database": "ok"})
    return JSONResponse({"status": "unavailable", "database": "error"}, status_code=503)
