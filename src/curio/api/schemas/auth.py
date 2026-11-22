"""Auth request and response schemas."""

from uuid import UUID

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    token: str = Field(min_length=1)


class UserSummaryResponse(BaseModel):
    id: UUID
    username: str


class LoginResponse(BaseModel):
    user: UserSummaryResponse


class LogoutResponse(BaseModel):
    ok: bool
