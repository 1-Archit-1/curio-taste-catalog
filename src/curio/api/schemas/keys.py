"""API key management and decrypt audit schemas."""

from datetime import datetime

from pydantic import BaseModel, Field

from curio.clients.providers import KeyType


class ApiKeyRequest(BaseModel):
    api_key: str = Field(min_length=1)


class KeyStatusResponse(BaseModel):
    configured: bool
    last4: str | None = None
    updated_at: datetime | None = None


class KeyTestResponse(BaseModel):
    ok: bool
    status_code: int | None = None
    detail: str


class KeySavedResponse(BaseModel):
    key_type: KeyType
    configured: bool = True
    last4: str
    updated_at: datetime


class KeyDeletedResponse(BaseModel):
    deleted: bool


class AuditEntryResponse(BaseModel):
    id: int
    key_type: KeyType
    job_id: str | None = None
    reason: str
    created_at: datetime


class AuditLogResponse(BaseModel):
    entries: list[AuditEntryResponse]
