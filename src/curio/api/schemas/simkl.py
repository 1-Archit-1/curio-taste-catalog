"""Simkl connection schemas. Nothing here carries a token, a client secret or ciphertext."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel


class SimklConnectResponse(BaseModel):
    # Where to send the browser: Simkl's consent page.
    authorize_url: str


class SimklStatusResponse(BaseModel):
    # False when the operator hasn't set up a Simkl app, so connecting is impossible.
    configured: bool
    connected: bool
    source_id: UUID | None = None
    account_name: str | None = None
    last_synced_at: datetime | None = None
    last_error: str | None = None
    # Simkl rejected the stored grant. Nothing syncs until the user connects again.
    needs_reconnect: bool = False
    # The sync that is queued or running now, so the page can follow it (`GET /api/jobs/{id}`).
    sync_job_id: UUID | None = None
