"""Example of mocking outbound HTTP with respx. External clients (#4, #5) follow this pattern,
with responses loaded from recorded fixtures under tests/fixtures/."""

import httpx
import pytest
from pytest_socket import SocketConnectBlockedError


async def test_respx_mocks_outbound_http(respx_mock):
    route = respx_mock.get("https://api.example.com/thing").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    async with httpx.AsyncClient() as client:
        response = await client.get("https://api.example.com/thing")
    assert route.called
    assert response.json() == {"ok": True}


@pytest.mark.filterwarnings("ignore:A test tried to use socket")
def test_real_network_is_blocked():
    # 203.0.113.0/24 is a documentation range; the IP literal avoids a DNS lookup.
    with pytest.raises(SocketConnectBlockedError):
        httpx.get("http://203.0.113.1")
