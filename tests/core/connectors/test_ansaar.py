"""The ansaar-data client (spec §12)."""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest
import respx
from aegis.connectors.ansaar import AnsaarClient, AnsaarError

BASE = "http://ansaar.test"


def _token(status=200):
    body = {"success": True, "token": "t0k", "expiresIn": 900} if status == 200 else {"success": False}
    return respx.post(f"{BASE}/api/auth/client-token").mock(return_value=httpx.Response(status, json=body))


@respx.mock
async def test_decisions_fetch_a_token_once_then_the_day():
    tok = _token()
    dec = respx.get(f"{BASE}/api/execution/trade-decisions").mock(
        return_value=httpx.Response(200, json={"data": [{"symbol": "TCS"}], "meta": {"date": "2026-09-11"}})
    )
    client = AnsaarClient(BASE, "s3cret")
    rows, meta = await client.decisions(date(2026, 9, 11))
    assert rows == [{"symbol": "TCS"}] and meta == {"date": "2026-09-11"}
    assert json.loads(tok.calls.last.request.content) == {"serviceSecret": "s3cret"}
    assert dec.calls.last.request.headers["Authorization"] == "Bearer t0k"
    assert dec.calls.last.request.url.params["date"] == "2026-09-11"
    await client.decisions(date(2026, 9, 11))
    assert tok.call_count == 1
    await client.close()


@respx.mock
async def test_a_refused_token_is_an_ansaar_error_that_never_carries_the_secret():
    _token(401)
    with pytest.raises(AnsaarError) as exc:
        await AnsaarClient(BASE, "s3cret").decisions(date(2026, 9, 11))
    assert "401" in str(exc.value) and "s3cret" not in str(exc.value)


@respx.mock
async def test_a_network_error_is_an_ansaar_error():
    respx.post(f"{BASE}/api/auth/client-token").mock(side_effect=httpx.ConnectError("boom"))
    with pytest.raises(AnsaarError):
        await AnsaarClient(BASE, "s").decisions(date(2026, 9, 11))


@respx.mock
async def test_a_bad_status_on_the_data_call_is_an_ansaar_error():
    _token()
    respx.get(f"{BASE}/api/execution/trade-decisions").mock(return_value=httpx.Response(503))
    with pytest.raises(AnsaarError, match="503"):
        await AnsaarClient(BASE, "s").decisions(date(2026, 9, 11))


@respx.mock
async def test_prices_come_back_oldest_first_from_the_right_path():
    _token()
    eq = respx.get(f"{BASE}/api/equities/prices/M%26M").mock(
        return_value=httpx.Response(
            200,
            json={
                "data": [
                    {"date": "2026-09-11", "close": 3100.5, "volume": "123"},
                    {"date": "2026-09-10", "close": 3050.0, "volume": "456"},
                    {"date": "2026-09-09", "close": None, "volume": "0"},
                ]
            },
        )
    )
    respx.get(f"{BASE}/api/etfs/GOLDBEES/prices").mock(return_value=httpx.Response(200, json={"data": [], "meta": {}}))
    client = AnsaarClient(BASE, "s")
    bars = await client.prices("M&M", "equity", date(2026, 9, 9), date(2026, 9, 11))
    assert bars == [
        {"day": date(2026, 9, 10), "close": 3050.0, "split_ratio": None, "dividend": None},
        {"day": date(2026, 9, 11), "close": 3100.5, "split_ratio": None, "dividend": None},
    ]
    assert eq.calls.last.request.url.params["from"] == "2026-09-09"
    assert eq.calls.last.request.url.params["to"] == "2026-09-11"
    assert await client.prices("GOLDBEES", "etf", date(2026, 9, 9), date(2026, 9, 11)) == []
