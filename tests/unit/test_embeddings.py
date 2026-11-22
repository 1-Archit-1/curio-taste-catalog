import json
import logging
import math

import httpx
import pytest

from curio.clients.embeddings import (
    BASE_URL,
    EMBEDDING_DIMENSIONS,
    EmbeddingError,
    GeminiEmbedder,
    l2_normalize,
)

KEY = "gemini-secret-key-abcdef"
URL = f"{BASE_URL}/models/gemini-embedding-001:batchEmbedContents"


async def no_sleep(_: float) -> None:
    return None


def vector(seed: float) -> list[float]:
    return [seed] * EMBEDDING_DIMENSIONS


def reply(request: httpx.Request) -> httpx.Response:
    n = len(json.loads(request.content)["requests"])
    return httpx.Response(200, json={"embeddings": [{"values": vector(3.0)} for _ in range(n)]})


def test_l2_normalize():
    assert l2_normalize([3.0, 4.0]) == pytest.approx([0.6, 0.8])
    with pytest.raises(EmbeddingError):
        l2_normalize([0.0, 0.0])


async def test_embeddings_are_normalized_and_use_configured_model(respx_mock):
    route = respx_mock.post(URL).mock(side_effect=reply)
    embedder = GeminiEmbedder(KEY, "gemini-embedding-001")
    vectors = await embedder.embed(["a", "b"])
    await embedder.aclose()
    assert len(vectors) == 2
    assert all(len(v) == EMBEDDING_DIMENSIONS for v in vectors)
    assert math.isclose(math.sqrt(sum(x * x for x in vectors[0])), 1.0)
    request = route.calls.last.request
    assert request.headers["x-goog-api-key"] == KEY
    assert KEY not in str(request.url)
    body = json.loads(request.content)
    assert body["requests"][0]["outputDimensionality"] == EMBEDDING_DIMENSIONS
    assert body["requests"][0]["model"] == "models/gemini-embedding-001"


async def test_embeddings_are_batched(respx_mock):
    route = respx_mock.post(URL).mock(side_effect=reply)
    embedder = GeminiEmbedder(KEY, "gemini-embedding-001", batch_size=2)
    vectors = await embedder.embed(["a", "b", "c", "d", "e"])
    await embedder.aclose()
    assert len(vectors) == 5
    assert route.call_count == 3


async def test_model_id_comes_from_argument(respx_mock):
    route = respx_mock.post(f"{BASE_URL}/models/other-model:batchEmbedContents").mock(
        side_effect=reply
    )
    embedder = GeminiEmbedder(KEY, "other-model")
    await embedder.embed(["a"])
    await embedder.aclose()
    assert route.called


async def test_error_status_raises_without_key(respx_mock):
    respx_mock.post(URL).mock(return_value=httpx.Response(429))
    embedder = GeminiEmbedder(KEY, "gemini-embedding-001", sleep=no_sleep)
    with pytest.raises(EmbeddingError) as exc:
        await embedder.embed(["a"])
    await embedder.aclose()
    assert exc.value.status_code == 429
    assert KEY not in str(exc.value)


async def test_logs_contain_no_key(respx_mock, caplog):
    caplog.set_level(logging.DEBUG)
    respx_mock.post(URL).mock(side_effect=reply)
    embedder = GeminiEmbedder(KEY, "gemini-embedding-001")
    await embedder.embed(["a"])
    await embedder.aclose()
    logging.getLogger("httpx").debug("h: %s", [("x-goog-api-key", KEY)])
    assert KEY not in "\n".join(r.getMessage() for r in caplog.records)


async def test_retries_429_then_succeeds(respx_mock):
    sleeps: list[float] = []

    async def record(seconds: float) -> None:
        sleeps.append(seconds)

    route = respx_mock.post(URL).mock(
        side_effect=[
            httpx.Response(429, headers={"Retry-After": "2"}),
            httpx.Response(200, json={"embeddings": [{"values": vector(3.0)}]}),
        ]
    )
    async with GeminiEmbedder(KEY, "gemini-embedding-001", sleep=record) as embedder:
        vectors = await embedder.embed(["a"])
    assert len(vectors) == 1
    assert route.call_count == 2
    assert sleeps == [2.0]


async def test_malformed_response_raises_embedding_error(respx_mock):
    respx_mock.post(URL).mock(return_value=httpx.Response(200, json={"embeddings": [{}]}))
    async with GeminiEmbedder(KEY, "gemini-embedding-001") as embedder:
        with pytest.raises(EmbeddingError):
            await embedder.embed(["a"])
