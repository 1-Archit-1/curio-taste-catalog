"""External provider API key validation clients.

Rules (docs/architecture.md & ADR 0002):
- `clients` only use httpx and never touch the database.
- Never log API keys, auth headers, or URLs containing key query parameters.
"""

from dataclasses import dataclass
from enum import StrEnum

import httpx

DEFAULT_TIMEOUT_SECONDS = 10.0


class KeyType(StrEnum):
    TMDB = "tmdb"
    GEMINI = "gemini"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    OPENROUTER = "openrouter"


LLM_PROVIDERS: tuple[KeyType, ...] = (
    KeyType.GEMINI,
    KeyType.OPENAI,
    KeyType.ANTHROPIC,
    KeyType.OPENROUTER,
)


@dataclass(frozen=True)
class KeyValidationResult:
    ok: bool
    status_code: int | None = None
    detail: str = ""


def _interpret_status(provider: KeyType, status_code: int) -> KeyValidationResult:
    if 200 <= status_code < 300:
        return KeyValidationResult(ok=True, status_code=status_code, detail="Key is valid.")
    if status_code in (401, 403):
        return KeyValidationResult(
            ok=False,
            status_code=status_code,
            detail=f"{provider.value} rejected the API key (HTTP {status_code}).",
        )
    if status_code == 429:
        return KeyValidationResult(
            ok=False,
            status_code=status_code,
            detail=f"{provider.value} rate-limited or quota-exhausted (HTTP 429).",
        )
    return KeyValidationResult(
        ok=False,
        status_code=status_code,
        detail=f"{provider.value} returned unexpected HTTP {status_code}.",
    )


async def validate_provider_key(
    key_type: KeyType,
    api_key: str,
    *,
    request_timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> KeyValidationResult:
    """Make a lightweight authenticated call to verify an API key before storing it."""
    cleaned = api_key.strip()
    if not cleaned:
        return KeyValidationResult(ok=False, detail="API key cannot be empty.")

    try:
        async with httpx.AsyncClient(timeout=request_timeout) as client:
            if key_type is KeyType.TMDB:
                # TMDB supports both v4 Read Access Tokens (Bearer JWT) and v3 API keys.
                if len(cleaned) > 64 or "." in cleaned:
                    resp = await client.get(
                        "https://api.themoviedb.org/3/configuration",
                        headers={
                            "Authorization": f"Bearer {cleaned}",
                            "Accept": "application/json",
                        },
                    )
                else:
                    resp = await client.get(
                        "https://api.themoviedb.org/3/configuration",
                        params={"api_key": cleaned},
                        headers={"Accept": "application/json"},
                    )
            elif key_type is KeyType.GEMINI:
                resp = await client.get(
                    "https://generativelanguage.googleapis.com/v1beta/models",
                    headers={"x-goog-api-key": cleaned},
                )
            elif key_type is KeyType.OPENAI:
                resp = await client.get(
                    "https://api.openai.com/v1/models",
                    headers={"Authorization": f"Bearer {cleaned}"},
                )
            elif key_type is KeyType.ANTHROPIC:
                resp = await client.get(
                    "https://api.anthropic.com/v1/models",
                    headers={
                        "x-api-key": cleaned,
                        "anthropic-version": "2023-06-01",
                    },
                )
            elif key_type is KeyType.OPENROUTER:
                resp = await client.get(
                    "https://openrouter.ai/api/v1/auth/key",
                    headers={"Authorization": f"Bearer {cleaned}"},
                )
            else:  # pragma: no cover - exhaustive enum
                return KeyValidationResult(ok=False, detail=f"Unsupported key type: {key_type}")
    except httpx.TimeoutException:
        return KeyValidationResult(
            ok=False,
            detail=f"Timed out contacting {key_type.value}.",
        )
    except httpx.HTTPError:
        # Intentionally omit exception text so no request URL/headers can leak.
        return KeyValidationResult(
            ok=False,
            detail=f"Network error contacting {key_type.value}.",
        )

    return _interpret_status(key_type, resp.status_code)
