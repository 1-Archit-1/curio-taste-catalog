from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from psycopg import AsyncConnection

from curio.api.deps import get_conn
from curio.services import health as health_service

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz(conn: Annotated[AsyncConnection, Depends(get_conn)]) -> JSONResponse:
    if await health_service.database_ok(conn):
        return JSONResponse({"status": "ok", "database": "ok"})
    return JSONResponse({"status": "unavailable", "database": "error"}, status_code=503)
