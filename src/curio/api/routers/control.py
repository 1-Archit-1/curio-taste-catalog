"""Authenticated control-plane routes (auth, settings, API keys, audit). No CORS."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from curio.api.deps import AppSettings, CurrentUser, DbSession
from curio.api.schemas.auth import (
    LoginRequest,
    LoginResponse,
    LogoutResponse,
    UserSummaryResponse,
)
from curio.api.schemas.keys import (
    ApiKeyRequest,
    AuditEntryResponse,
    AuditLogResponse,
    KeyDeletedResponse,
    KeySavedResponse,
    KeyStatusResponse,
    KeyTestResponse,
)
from curio.api.schemas.settings import (
    RotateAddonTokenResponse,
    SettingsResponse,
    UpdateSettingsRequest,
)
from curio.clients.providers import LLM_PROVIDERS, KeyType
from curio.crypto import PublicKeyNotConfiguredError
from curio.db.models.users import User
from curio.services import auth as auth_service
from curio.services import keys as keys_service
from curio.services import users as users_service
from curio.settings import Settings

router = APIRouter(prefix="/api", tags=["control"])


def _manifest_url(request: Request, settings: Settings, addon_token: str) -> str:
    base = (settings.public_base_url or str(request.base_url)).rstrip("/")
    return f"{base}/{addon_token}/manifest.json"


async def _build_settings_response(
    request: Request,
    session: AsyncSession,
    settings: Settings,
    user: User,
) -> SettingsResponse:
    metadata = await keys_service.list_user_key_metadata(session, user_id=user.id)
    keys_payload: dict[KeyType, KeyStatusResponse] = {}
    for kt in KeyType:
        row = metadata.get(kt)
        keys_payload[kt] = KeyStatusResponse(
            configured=row is not None,
            last4=row.last4 if row else None,
            updated_at=row.updated_at if row else None,
        )

    return SettingsResponse(
        user_id=user.id,
        username=user.username,
        addon_manifest_url=_manifest_url(request, settings, user.addon_token),
        llm_provider=KeyType(user.llm_provider),
        supported_llm_providers=list(LLM_PROVIDERS),
        monthly_token_budget=user.monthly_token_budget,
        jobs_paused_reason=user.jobs_paused_reason,
        keys=keys_payload,
    )


@router.post("/auth/login")
async def login(
    payload: LoginRequest,
    response: Response,
    session: DbSession,
    settings: AppSettings,
) -> LoginResponse:
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
    return LoginResponse(user=UserSummaryResponse(id=user.id, username=user.username))


@router.post("/auth/logout")
async def logout(
    request: Request,
    response: Response,
    user: CurrentUser,
    session: DbSession,
    settings: AppSettings,
) -> LogoutResponse:
    session_token = request.cookies.get(settings.session_cookie_name)
    if session_token:
        await auth_service.revoke_session(session, user_id=user.id, session_token=session_token)
    response.delete_cookie(key=settings.session_cookie_name, path="/")
    return LogoutResponse(ok=True)


@router.get("/auth/me")
async def me(user: CurrentUser) -> UserSummaryResponse:
    return UserSummaryResponse(id=user.id, username=user.username)


@router.get("/settings")
async def get_settings_view(
    request: Request,
    user: CurrentUser,
    session: DbSession,
    settings: AppSettings,
) -> SettingsResponse:
    return await _build_settings_response(request, session, settings, user)


@router.patch("/settings")
async def update_settings_view(
    payload: UpdateSettingsRequest,
    request: Request,
    user: CurrentUser,
    session: DbSession,
    settings: AppSettings,
) -> SettingsResponse:
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
) -> RotateAddonTokenResponse:
    updated = await users_service.rotate_addon_token_for_user(session, user_id=user.id)
    return RotateAddonTokenResponse(
        addon_manifest_url=_manifest_url(request, settings, updated.addon_token),
    )


@router.post("/settings/keys/{key_type}/test")
async def test_api_key(
    key_type: KeyType,
    payload: ApiKeyRequest,
    _: CurrentUser,
) -> KeyTestResponse:
    result = await keys_service.test_api_key(key_type, payload.api_key)
    return KeyTestResponse(
        ok=result.ok,
        status_code=result.status_code,
        detail=result.detail,
    )


@router.put("/settings/keys/{key_type}")
async def save_api_key(
    key_type: KeyType,
    payload: ApiKeyRequest,
    user: CurrentUser,
    session: DbSession,
    settings: AppSettings,
) -> KeySavedResponse:
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

    return KeySavedResponse(
        key_type=KeyType(row.key_type),
        configured=True,
        last4=row.last4,
        updated_at=row.updated_at,
    )


@router.delete("/settings/keys/{key_type}")
async def delete_api_key(
    key_type: KeyType,
    user: CurrentUser,
    session: DbSession,
) -> KeyDeletedResponse:
    deleted = await keys_service.delete_user_api_key(session, user_id=user.id, key_type=key_type)
    return KeyDeletedResponse(deleted=deleted)


@router.get("/settings/audit")
async def get_audit_log(
    user: CurrentUser,
    session: DbSession,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> AuditLogResponse:
    entries = await keys_service.list_user_audit_entries(session, user_id=user.id, limit=limit)
    return AuditLogResponse(
        entries=[
            AuditEntryResponse(
                id=entry.id,
                key_type=KeyType(entry.key_type),
                job_id=entry.job_id,
                reason=entry.reason,
                created_at=entry.created_at,
            )
            for entry in entries
        ]
    )
