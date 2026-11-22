"""Gemini embeddings client (batched, reduced to 768 dimensions, L2-normalized).

Gemini returns unnormalized vectors when `outputDimensionality` is below the native size, so
normalization happens here. The key goes in a header (never the URL) and is never logged.
"""
import asyncio
import math
from collections.abc import Awaitable, Callable, Sequence

import httpx

from curio.clients.redaction import install_log_redaction

EMBEDDING_DIMENSIONS = 768
BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
DEFAULT_TIMEOUT_SECONDS = 30.0


class EmbeddingError(Exception):
    """Embedding request failed. Messages never contain the key or URL."""

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def l2_normalize(vector: Sequence[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    if norm == 0:
        raise EmbeddingError("embedding has zero norm")
    return [x / norm for x in vector]


class GeminiEmbedder:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        batch_size: int = 100,
        http: httpx.AsyncClient | None = None,
        max_retries: int = 3,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        install_log_redaction()
        self._headers = {"x-goog-api-key": api_key.strip()}
        self._model = model
        self._batch_size = batch_size
        self._http = http or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT_SECONDS)
        self._owns_http = http is None
        self._max_retries = max_retries
        self._sleep = sleep

    @property
    def batch_size(self) -> int:
        return self._batch_size

    async def __aenter__(self) -> "GeminiEmbedder":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """One normalized 768-d vector per input text, in order."""
        out: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            out.extend(await self._embed_batch(texts[start : start + self._batch_size]))
        return out

    async def _embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        model = f"models/{self._model}"
        body = {
            "requests": [
                {
                    "model": model,
                    "content": {"parts": [{"text": t}]},
                    "taskType": "RETRIEVAL_DOCUMENT",
                    "outputDimensionality": EMBEDDING_DIMENSIONS,
                }
                for t in texts
            ]
        }
        for attempt in range(self._max_retries + 1):
            try:
                resp = await self._http.post(
                    f"{BASE_URL}/{model}:batchEmbedContents", json=body, headers=self._headers
                )
            except httpx.TimeoutException:
                raise EmbeddingError("timed out contacting Gemini") from None
            except httpx.HTTPError:
                raise EmbeddingError("network error contacting Gemini") from None
            if resp.status_code == 429 and attempt < self._max_retries:
                try:
                    delay = max(0.0, float(resp.headers["Retry-After"]))
                except (KeyError, ValueError):
                    delay = float(2**attempt)
                await self._sleep(delay)
                continue
            break
        if resp.status_code != 200:
            raise EmbeddingError(f"Gemini returned HTTP {resp.status_code}", resp.status_code)
        try:
            embeddings = resp.json().get("embeddings", [])
            vectors = [e["values"] for e in embeddings]
        except (ValueError, KeyError, TypeError, AttributeError):
            raise EmbeddingError("Gemini returned an unexpected response") from None
        if len(vectors) != len(texts):
            raise EmbeddingError("Gemini returned a different number of embeddings than inputs")
        if any(len(v) != EMBEDDING_DIMENSIONS for v in vectors):
            raise EmbeddingError(f"expected {EMBEDDING_DIMENSIONS}-dimension embeddings")
        return [l2_normalize(v) for v in vectors]
