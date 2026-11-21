"""User settings and addon manifest URL schemas."""

from uuid import UUID

from pydantic import BaseModel, Field

from curio.api.schemas.keys import KeyStatusResponse
from curio.clients.providers import KeyType


class UpdateSettingsRequest(BaseModel):
    llm_provider: KeyType
    monthly_token_budget: int | None = Field(default=None, gt=0)


class SettingsResponse(BaseModel):
    user_id: UUID
    username: str
    addon_manifest_url: str
    llm_provider: KeyType
    supported_llm_providers: list[KeyType]
    monthly_token_budget: int | None = None
    jobs_paused_reason: str | None = None
    keys: dict[KeyType, KeyStatusResponse]


class RotateAddonTokenResponse(BaseModel):
    addon_manifest_url: str
