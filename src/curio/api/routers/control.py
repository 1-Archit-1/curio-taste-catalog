"""Authenticated control-plane routes (auth, settings, API keys, audit). No CORS."""

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from curio.api.deps import AppSettings, CurrentUser, DbSession
from curio.clients.providers import LLM_PROVIDERS, KeyType
from curio.crypto import PublicKeyNotConfiguredError
from curio.db.models.users import User
from curio.services import auth as auth_service
from curio.services import keys as keys_service
from curio.services import users as users_service
from curio.settings import Settings

router = APIRouter(prefix="/api", tags=["control"])


class LoginRequest(BaseModel):
    token: str = Field(min_length=1)


class ApiKeyRequest(BaseModel):
    api_key: str = Field(min_length=1)


class UpdateSettingsRequest(BaseModel):
    llm_provider: KeyType
    monthly_token_budget: int | None = Field(default=None, gt=0)


class KeyStatusResponse(BaseModel):
    configured: bool
    last4: str | None = None
    updated_at: datetime | None = None


def _manifest_url(request: Request, settings: Settings, addon_token: str) -> str:
    base = (settings.public_base_url or str(request.base_url)).rstrip("/")
    return f"{base}/{addon_token}/manifest.json"


async def _build_settings_response(
    request: Request,
    session: AsyncSession,
    settings: Settings,
    user: User,
) -> dict[str, Any]:
    metadata = await keys_service.list_user_key_metadata(session, user_id=user.id)
    keys_payload: dict[str, dict[str, Any]] = {}
    for kt in KeyType:
        row = metadata.get(kt)
        keys_payload[kt.value] = KeyStatusResponse(
            configured=row is not None,
            last4=row.last4 if row else None,
            updated_at=row.updated_at if row else None,
        ).model_dump(mode="json")

    return {
        "user_id": str(user.id),
        "username": user.username,
        "addon_manifest_url": _manifest_url(request, settings, user.addon_token),
        "llm_provider": user.llm_provider,
        "supported_llm_providers": [p.value for p in LLM_PROVIDERS],
        "monthly_token_budget": user.monthly_token_budget,
        "jobs_paused_reason": user.jobs_paused_reason,
        "keys": keys_payload,
    }


@router.post("/auth/login")
async def login(
    payload: LoginRequest,
    response: Response,
    session: DbSession,
    settings: AppSettings,
) -> dict[str, Any]:
    result = await auth_service.exchange_login_token(
        session,
        login_token=payload.token,
        ttl_days=settings.session_ttl_days,
    )
    if result is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid login token.",
        )

    user, session_token = result
    response.set_cookie(
        key=settings.session_cookie_name,
        value=session_token,
        httponly=True,
        samesite="lax",
        secure=settings.session_cookie_secure,
        max_age=settings.session_ttl_days * 86400,
        path="/",
    )
    return {
        "user": {
            "id": str(user.id),
            "username": user.username,
        }
    }


@router.post("/auth/logout")
async def logout(
    request: Request,
    response: Response,
    user: CurrentUser,
    session: DbSession,
    settings: AppSettings,
) -> dict[str, bool]:
    session_token = request.cookies.get(settings.session_cookie_name)
    if session_token:
        await auth_service.revoke_session(session, user_id=user.id, session_token=session_token)
    response.delete_cookie(key=settings.session_cookie_name, path="/")
    return {"ok": True}


@router.get("/auth/me")
async def me(user: CurrentUser) -> dict[str, str]:
    return {
        "id": str(user.id),
        "username": user.username,
    }


@router.get("/settings")
async def get_settings_view(
    request: Request,
    user: CurrentUser,
    session: DbSession,
    settings: AppSettings,
) -> dict[str, Any]:
    return await _build_settings_response(request, session, settings, user)


@router.patch("/settings")
async def update_settings_view(
    payload: UpdateSettingsRequest,
    request: Request,
    user: CurrentUser,
    session: DbSession,
    settings: AppSettings,
) -> dict[str, Any]:
    try:
        updated = await users_service.update_preferences(
            session,
            user_id=user.id,
            llm_provider=payload.llm_provider,
            monthly_token_budget=payload.monthly_token_budget,
        )
    except (users_service.InvalidProviderError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    return await _build_settings_response(request, session, settings, updated)


@router.post("/settings/addon-token/rotate")
async def rotate_addon_token(
    request: Request,
    user: CurrentUser,
    session: DbSession,
    settings: AppSettings,
) -> dict[str, str]:
    updated = await users_service.rotate_addon_token_for_user(session, user_id=user.id)
    return {
        "addon_manifest_url": _manifest_url(request, settings, updated.addon_token),
    }


@router.post("/settings/keys/{key_type}/test")
async def test_api_key(
    key_type: KeyType,
    payload: ApiKeyRequest,
    _: CurrentUser,
) -> dict[str, Any]:
    result = await keys_service.test_api_key(key_type, payload.api_key)
    return {
        "ok": result.ok,
        "status_code": result.status_code,
        "detail": result.detail,
    }


@router.put("/settings/keys/{key_type}")
async def save_api_key(
    key_type: KeyType,
    payload: ApiKeyRequest,
    user: CurrentUser,
    session: DbSession,
    settings: AppSettings,
) -> dict[str, Any]:
    try:
        row = await keys_service.set_user_api_key(
            session,
            user_id=user.id,
            key_type=key_type,
            api_key=payload.api_key,
            public_key=settings.key_public_key,
        )
    except PublicKeyNotConfiguredError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Encryption public key is not configured on this server.",
        ) from exc
    except keys_service.KeyValidationFailedError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=str(exc),
        ) from exc

    return {
        "key_type": row.key_type,
        "configured": True,
        "last4": row.last4,
        "updated_at": row.updated_at.isoformat(),
    }


@router.delete("/settings/keys/{key_type}")
async def delete_api_key(
    key_type: KeyType,
    user: CurrentUser,
    session: DbSession,
) -> dict[str, bool]:
    deleted = await keys_service.delete_user_api_key(session, user_id=user.id, key_type=key_type)
    return {"deleted": deleted}


@router.get("/settings/audit")
async def get_audit_log(
    user: CurrentUser,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    entries = await keys_service.list_user_audit_entries(session, user_id=user.id, limit=limit)
    return {
        "entries": [
            {
                "id": entry.id,
                "key_type": entry.key_type,
                "job_id": entry.job_id,
                "reason": entry.reason,
                "created_at": entry.created_at.isoformat(),
            }
            for entry in entries
        ]
    }
