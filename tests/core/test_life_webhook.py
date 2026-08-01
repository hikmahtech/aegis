"""B4 — POST /api/webhooks/life/{source}: the signed life-data push.

This is an unauthenticated-by-default, internet-facing endpoint that writes
into the owner's personal data store, so the *negative* tests below are the
deliverable and the happy path is the afterthought:

* an unsigned request is rejected,
* a wrong signature is rejected,
* a signature valid for a different body is rejected,
* a signature valid for a different source is rejected,
* a replayed request outside the timestamp window is rejected,
* a replayed request inside it is idempotent,
* and every one of those still fails closed (503) when no secret is set —
  an empty secret must never mean "skip verification".

Real Postgres (`db_pool`), fake Temporal client, httpx ASGITransport.

Isolation: every row this file writes is namespaced `zzb4-` (observations)
or `life:` (ingest_idempotency / audit_log), and the teardown deletes by
those prefixes only.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
import pytest_asyncio
from aegis.api.app import create_app
from aegis.api.deps import get_settings
from aegis.api.routes import webhooks
from aegis.api.routes.interactions import get_workflow_client
from aegis.config import Settings
from httpx import ASGITransport, AsyncClient

SECRET = "zzb4-test-life-secret"
SOURCE = "observation"

_TEST_SETTINGS = {
    "database_url": "postgresql://test:test@localhost:5432/test",
    "litellm_url": "https://litellm.example.com/v1",
    "temporal_ui_url": "https://temporal.example.com",
    "n8n_ui_url": "https://n8n.example.com",
    "admin_username": "admin",
    "admin_password": "admin",
    "life_webhook_secret": SECRET,
}


def _sign(secret: str, source: str, ts: str, body: bytes) -> str:
    """What a well-behaved client computes: HMAC over "{source}.{ts}." + body."""
    return hmac.new(
        secret.encode(), f"{source}.{ts}.".encode() + body, hashlib.sha256
    ).hexdigest()


def _push(
    body: dict | bytes,
    *,
    source: str = SOURCE,
    secret: str = SECRET,
    ts: str | None = None,
) -> tuple[str, bytes, dict[str, str]]:
    """Build (url, raw_body, headers) for a push signed with the literal scheme."""
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    stamp = ts if ts is not None else str(int(time.time()))
    sig = _sign(secret, source, stamp, raw)
    return (
        f"/api/webhooks/life/{source}",
        raw,
        {"X-Aegis-Timestamp": stamp, "X-Aegis-Signature": sig},
    )


async def _cleanup(pool) -> None:
    await pool.execute("DELETE FROM life.observations WHERE source LIKE 'zzb4-%'")
    await pool.execute("DELETE FROM life.observations WHERE metric LIKE 'zzb4-%'")
    await pool.execute("DELETE FROM ingest_idempotency WHERE source_type LIKE 'life:%'")
    await pool.execute("DELETE FROM audit_log WHERE target_type = 'life_webhook'")


@pytest.fixture
def temporal_stub():
    handle = MagicMock()
    handle.id = "wf-life-1"
    client = MagicMock()
    client.start_workflow = AsyncMock(return_value=handle)
    return client


def _build_app(db_pool, settings, temporal_stub):
    app = create_app(run_lifespan=False)
    app.state.db_pool = db_pool
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_workflow_client] = lambda: temporal_stub
    return app


@pytest_asyncio.fixture(loop_scope="function")
async def client(db_pool, temporal_stub):
    """Endpoint with a configured secret."""
    await _cleanup(db_pool)
    app = _build_app(db_pool, Settings(**_TEST_SETTINGS), temporal_stub)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await _cleanup(db_pool)


@pytest_asyncio.fixture(loop_scope="function")
async def client_no_secret(db_pool, temporal_stub):
    """Identical endpoint with `life_webhook_secret` UNSET."""
    await _cleanup(db_pool)
    settings = Settings(**{**_TEST_SETTINGS, "life_webhook_secret": ""})
    app = _build_app(db_pool, settings, temporal_stub)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    await _cleanup(db_pool)


async def _counts(pool) -> tuple[int, int]:
    obs = await pool.fetchval(
        "SELECT count(*) FROM life.observations WHERE metric LIKE 'zzb4-%'"
    )
    claims = await pool.fetchval(
        "SELECT count(*) FROM ingest_idempotency WHERE source_type LIKE 'life:%'"
    )
    return obs, claims


# ---------------------------------------------------------------------------
# Fail closed: an unset secret must reject EVERYTHING, including valid pushes.
# ---------------------------------------------------------------------------


async def test_unset_secret_rejects_a_correctly_signed_push(client_no_secret, db_pool):
    """The inverse of the `owner_emails` bug: empty config must close the door.

    The request here is signed correctly for SECRET — if the handler ever
    treated an empty secret as "nothing to verify", this would be a 202 and
    an anonymous internet caller would be writing to life.observations.
    """
    url, raw, headers = _push({"metric": "zzb4-weight", "value": 80, "source": "zzb4-scale"})
    resp = await client_no_secret.post(url, content=raw, headers=headers)
    assert resp.status_code == 503
    assert resp.json()["detail"] == "life_webhook_secret_not_configured"
    assert await _counts(db_pool) == (0, 0)


async def test_unset_secret_rejects_an_unsigned_push(client_no_secret, db_pool):
    """The likeliest real attack shape: no headers at all, no secret deployed."""
    resp = await client_no_secret.post(
        f"/api/webhooks/life/{SOURCE}",
        content=json.dumps({"metric": "zzb4-weight", "value": 80}).encode(),
    )
    assert resp.status_code == 503
    assert resp.json()["detail"] == "life_webhook_secret_not_configured"
    assert await _counts(db_pool) == (0, 0)


async def test_unset_secret_rejects_an_empty_signature(client_no_secret, db_pool):
    """`X-Aegis-Signature: ""` vs an empty secret is the classic ""=="" hole."""
    resp = await client_no_secret.post(
        f"/api/webhooks/life/{SOURCE}",
        content=b"{}",
        headers={"X-Aegis-Timestamp": str(int(time.time())), "X-Aegis-Signature": ""},
    )
    assert resp.status_code == 503
    assert await _counts(db_pool) == (0, 0)


# ---------------------------------------------------------------------------
# Signature verification.
# ---------------------------------------------------------------------------


async def test_unsigned_request_rejected(client, db_pool):
    resp = await client.post(
        f"/api/webhooks/life/{SOURCE}",
        content=json.dumps({"metric": "zzb4-weight", "value": 80}).encode(),
        headers={"X-Aegis-Timestamp": str(int(time.time()))},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "unauthorized"
    assert await _counts(db_pool) == (0, 0)


async def test_wrong_signature_rejected(client, db_pool):
    url, raw, headers = _push({"metric": "zzb4-weight", "value": 80}, secret="not-the-secret")
    resp = await client.post(url, content=raw, headers=headers)
    assert resp.status_code == 401
    assert resp.json()["detail"] == "unauthorized"
    assert await _counts(db_pool) == (0, 0)


def test_signing_input_is_exactly_source_timestamp_body():
    """Pin the wire scheme: `"{source}.{timestamp}." + raw_body`, nothing else.

    The two capture-and-replay tests below sign through this same production
    helper (so they model a *real* captured request rather than a
    hand-rolled one), which is only sound because this test pins what the
    helper produces.
    """
    assert webhooks.life_signing_input("loc", "1700000000", b'{"a":1}') == (
        b'loc.1700000000.{"a":1}'
    )
    # The two properties the replay tests depend on, stated separately.
    assert webhooks.life_signing_input("a", "1", b"X") != webhooks.life_signing_input(
        "a", "1", b"Y"
    )
    assert webhooks.life_signing_input("a", "1", b"X") != webhooks.life_signing_input(
        "b", "1", b"X"
    )


def _sign_as_client(source: str, ts: str, body: bytes) -> str:
    """Sign the way a legitimate client would, through the production helper.

    Used only by the capture-and-replay tests: because both sides go through
    `life_signing_input`, weakening the scheme makes the replay *succeed* —
    which is exactly what those tests must catch. Everything else in this file
    signs with the hand-written `_sign`, which pins the literal wire format.
    """
    return hmac.new(
        SECRET.encode(), webhooks.life_signing_input(source, ts, body), hashlib.sha256
    ).hexdigest()


async def test_signature_for_a_different_body_rejected(client, db_pool):
    """Capture a valid push, swap the payload, keep the signature."""
    ts = str(int(time.time()))
    captured = json.dumps({"metric": "zzb4-weight", "value": 80}).encode()
    tampered = json.dumps({"metric": "zzb4-weight", "value": 999}).encode()
    resp = await client.post(
        f"/api/webhooks/life/{SOURCE}",
        content=tampered,
        headers={
            "X-Aegis-Timestamp": ts,
            "X-Aegis-Signature": _sign_as_client(SOURCE, ts, captured),
        },
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "unauthorized"
    assert await _counts(db_pool) == (0, 0)


async def test_signature_is_bound_to_the_source(client, db_pool, monkeypatch):
    """A push captured for one source cannot be re-pointed at another.

    Without the source in the signed input this replay is a 202 into the
    wrong lane — health data landing as a location fix.
    """
    monkeypatch.setitem(webhooks.LIFE_SOURCES, "zzb4-other", None)
    ts = str(int(time.time()))
    raw = json.dumps({"metric": "zzb4-weight", "value": 80}).encode()
    resp = await client.post(
        "/api/webhooks/life/zzb4-other",
        content=raw,
        headers={
            "X-Aegis-Timestamp": ts,
            "X-Aegis-Signature": _sign_as_client(SOURCE, ts, raw),
        },
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "unauthorized"
    assert await _counts(db_pool) == (0, 0)


async def test_garbage_signature_header_is_401_not_500(client):
    """A non-ASCII header must not crash compare_digest into a 500.

    Sent as raw bytes: Starlette latin-1-decodes headers, so these become a
    non-ASCII `str`, which `hmac.compare_digest` refuses with TypeError.
    """
    resp = await client.post(
        f"/api/webhooks/life/{SOURCE}",
        content=b"{}",
        headers=[
            (b"x-aegis-timestamp", str(int(time.time())).encode()),
            (b"x-aegis-signature", b"\xff\xfe not hex"),
        ],
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "unauthorized"


# ---------------------------------------------------------------------------
# Replay protection.
# ---------------------------------------------------------------------------


async def test_stale_timestamp_rejected(client, db_pool):
    """A captured request replayed 10 minutes later, signature and all."""
    stale = str(int(time.time()) - 600)
    url, raw, headers = _push({"metric": "zzb4-weight", "value": 80}, ts=stale)
    resp = await client.post(url, content=raw, headers=headers)
    assert resp.status_code == 401
    assert resp.json()["detail"] == "unauthorized"
    assert await _counts(db_pool) == (0, 0)


async def test_future_timestamp_rejected(client, db_pool):
    future = str(int(time.time()) + 600)
    url, raw, headers = _push({"metric": "zzb4-weight", "value": 80}, ts=future)
    resp = await client.post(url, content=raw, headers=headers)
    assert resp.status_code == 401
    assert await _counts(db_pool) == (0, 0)


async def test_missing_timestamp_rejected(client, db_pool):
    """Signed exactly as the handler would with an absent timestamp.

    The observation doc proposed making the timestamp optional; that is what
    this test forbids — omitting the header must not buy a replay-forever
    signature.
    """
    raw = json.dumps({"metric": "zzb4-weight", "value": 80}).encode()
    resp = await client.post(
        f"/api/webhooks/life/{SOURCE}",
        content=raw,
        headers={"X-Aegis-Signature": _sign(SECRET, SOURCE, "", raw)},
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "unauthorized"
    assert await _counts(db_pool) == (0, 0)


async def test_non_numeric_timestamp_rejected(client):
    raw = b"{}"
    resp = await client.post(
        f"/api/webhooks/life/{SOURCE}",
        content=raw,
        headers={
            "X-Aegis-Timestamp": "not-a-number",
            "X-Aegis-Signature": _sign(SECRET, SOURCE, "not-a-number", raw),
        },
    )
    assert resp.status_code == 401


#: Latin-1 bytes whose decoded `str` is `isdigit()`-True but `isdecimal()`-False.
#: Starlette decodes headers as latin-1, so these arrive as the real superscript
#: characters (httpx would UTF-8 encode a non-ASCII `str` header, which decodes
#: to "Â²" and is `isdigit()`-False — hence raw bytes, as in the garbage-header
#: test above). `int()` rejects every one of them.
_ISDIGIT_NOT_DECIMAL = [
    b"\xb9",  # ¹ U+00B9
    b"\xb2",  # ² U+00B2
    b"\xb3",  # ³ U+00B3
    b"1\xb2",  # mixed: an ASCII digit followed by a superscript
    b"\xb9\xb2\xb3",  # ¹²³
]


@pytest.mark.parametrize("raw_ts", _ISDIGIT_NOT_DECIMAL, ids=lambda b: b.hex())
async def test_isdigit_but_not_int_parseable_timestamp_is_401_not_500(
    client, db_pool, raw_ts
):
    """A superscript timestamp must be the same opaque 401 as any other junk.

    `str.isdigit()` is True for ¹ ² ³ but `int()` raises on them, so guarding
    with `isdigit()` admitted the header and let the parse escape as an
    unauthenticated 500 — reached BEFORE the signature check, so no secret is
    needed to trigger it. That is both a distinguishing oracle (500 here vs 401
    for ordinary junk) and a way to generate stack traces at will.
    """
    assert raw_ts.decode("latin-1").isdigit()
    assert not raw_ts.decode("latin-1").isdecimal()

    resp = await client.post(
        f"/api/webhooks/life/{SOURCE}",
        content=b"{}",
        headers=[
            (b"x-aegis-timestamp", raw_ts),
            (b"x-aegis-signature", b"0" * 64),
        ],
    )
    assert resp.status_code == 401
    assert resp.json()["detail"] == "unauthorized"

    # Control, in the same run: ordinary non-numeric junk answers identically,
    # so there is nothing to distinguish the two probes by.
    plain = await client.post(
        f"/api/webhooks/life/{SOURCE}",
        content=b"{}",
        headers={"X-Aegis-Timestamp": "not-a-number", "X-Aegis-Signature": "0" * 64},
    )
    assert (resp.status_code, resp.json()) == (plain.status_code, plain.json())
    assert await _counts(db_pool) == (0, 0)


async def test_failures_are_indistinguishable(client):
    """No oracle: a bad timestamp and a bad signature answer identically."""
    _, raw_a, headers_a = _push({"metric": "zzb4-weight"}, ts=str(int(time.time()) - 600))
    _, raw_b, headers_b = _push({"metric": "zzb4-weight"}, secret="wrong")
    url = f"/api/webhooks/life/{SOURCE}"
    bad_ts = await client.post(url, content=raw_a, headers=headers_a)
    bad_sig = await client.post(url, content=raw_b, headers=headers_b)
    assert bad_ts.status_code == bad_sig.status_code == 401
    assert bad_ts.json() == bad_sig.json()


async def test_no_response_ever_carries_the_secret_or_signature(client):
    url, raw, headers = _push({"metric": "zzb4-weight", "value": 80}, secret="wrong")
    resp = await client.post(url, content=raw, headers=headers)
    blob = resp.text + repr(dict(resp.headers))
    assert SECRET not in blob
    assert headers["X-Aegis-Signature"] not in blob


async def test_in_window_replay_is_idempotent(client, db_pool):
    """Identical body + identical timestamp: accepted once, then a no-op."""
    url, raw, headers = _push(
        {"metric": "zzb4-steps", "value": 4200, "source": "zzb4-watch"}
    )
    first = await client.post(url, content=raw, headers=headers)
    second = await client.post(url, content=raw, headers=headers)
    assert first.status_code == 202
    assert first.json()["accepted"] is True
    assert "observation_id" in first.json()
    assert second.status_code == 202
    assert second.json()["duplicate"] is True
    assert "observation_id" not in second.json()
    obs, claims = await _counts(db_pool)
    assert obs == 1
    assert claims == 1


# ---------------------------------------------------------------------------
# Body bound.
# ---------------------------------------------------------------------------


async def test_oversize_body_rejected(client, db_pool):
    fat = json.dumps({"metric": "zzb4-weight", "pad": "x" * (80 * 1024)}).encode()
    assert len(fat) > webhooks.LIFE_MAX_BODY_BYTES
    url, raw, headers = _push(fat)
    resp = await client.post(url, content=raw, headers=headers)
    assert resp.status_code == 413
    assert resp.json()["detail"] == "body_too_large"
    assert await _counts(db_pool) == (0, 0)


# ---------------------------------------------------------------------------
# Routing + the two lanes.
# ---------------------------------------------------------------------------


async def test_unknown_source_returns_404_when_signed(client):
    url, raw, headers = _push({"metric": "zzb4-weight"}, source="zzb4-nope")
    resp = await client.post(url, content=raw, headers=headers)
    assert resp.status_code == 404
    assert resp.json()["detail"] == "unknown_source"


async def test_unknown_source_is_401_when_unsigned(client):
    """Unauthenticated callers cannot enumerate which life sources exist."""
    resp = await client.post("/api/webhooks/life/zzb4-nope", content=b"{}")
    assert resp.status_code == 401
    assert resp.json()["detail"] == "unauthorized"


async def test_valid_push_records_an_observation(client, db_pool):
    url, raw, headers = _push(
        {
            "metric": "zzb4-Weight_KG",
            "value": 80.5,
            "source": "zzb4-Scale",
            "metadata": {"unit": "kg"},
        }
    )
    resp = await client.post(url, content=raw, headers=headers)
    assert resp.status_code == 202
    body = resp.json()
    assert body["accepted"] is True
    row = await db_pool.fetchrow(
        "SELECT source, metric, value::float8 AS value, metadata FROM life.observations "
        "WHERE id = $1::uuid",
        body["observation_id"],
    )
    assert row is not None
    # normalize_key is applied on the way in — the series is case-insensitive.
    assert row["source"] == "zzb4-scale"
    assert row["metric"] == "zzb4-weight_kg"
    assert row["value"] == 80.5
    assert row["metadata"]["unit"] == "kg"
    audit = await db_pool.fetchval(
        "SELECT count(*) FROM audit_log WHERE target_type = 'life_webhook' "
        "AND target_id = $1",
        body["external_id"],
    )
    assert audit == 1


async def test_push_without_a_metric_is_422(client, db_pool):
    """An unlabelled reading can never be queried back; refuse it."""
    url, raw, headers = _push({"value": 1, "source": "zzb4-scale"})
    resp = await client.post(url, content=raw, headers=headers)
    assert resp.status_code == 422
    assert resp.json()["detail"] == "invalid_observation"
    obs, _ = await _counts(db_pool)
    assert obs == 0


async def test_rejected_push_releases_its_idempotency_claim(client, db_pool):
    """A client-fixable 422 must not burn the payload's `id` forever."""
    bad = _push({"id": "zzb4-fixme", "source": "zzb4-scale"})
    assert (await client.post(bad[0], content=bad[1], headers=bad[2])).status_code == 422
    good = _push({"id": "zzb4-fixme", "source": "zzb4-scale", "metric": "zzb4-weight", "value": 1})
    retry = await client.post(good[0], content=good[1], headers=good[2])
    assert retry.status_code == 202
    assert retry.json().get("duplicate") is None
    obs, claims = await _counts(db_pool)
    assert (obs, claims) == (1, 1)


async def test_flow_backed_source_dispatches_to_temporal(
    client, db_pool, temporal_stub, monkeypatch
):
    """The B5/B6 path: a source mapped to a workflow name starts that workflow.

    B4 ships no flow-backed source of its own, so the registry entry is
    injected exactly the way B5 will add `"location": "LocationIngestFlow"`.
    """
    monkeypatch.setitem(webhooks.LIFE_SOURCES, "zzb4-flowsrc", "ZzB4IngestFlow")
    url, raw, headers = _push(
        {"id": "zzb4-evt-1", "lat": 1.0}, source="zzb4-flowsrc"
    )
    resp = await client.post(url, content=raw, headers=headers)
    assert resp.status_code == 202
    assert resp.json()["workflow_id"] == "wf-life-1"
    temporal_stub.start_workflow.assert_awaited_once()
    call = temporal_stub.start_workflow.call_args
    assert call.args[0] == "ZzB4IngestFlow"
    assert call.args[1]["source"] == "zzb4-flowsrc"
    assert call.args[1]["payload"]["lat"] == 1.0
    assert call.kwargs["task_queue"] == "aegis-main"
    assert call.kwargs["id"] == "life-zzb4-flowsrc-zzb4-evt-1"
    # A flow-backed source never touches the inline observation lane.
    obs, _ = await _counts(db_pool)
    assert obs == 0


async def test_flow_backed_replay_starts_no_second_workflow(
    client, temporal_stub, monkeypatch
):
    monkeypatch.setitem(webhooks.LIFE_SOURCES, "zzb4-flowsrc", "ZzB4IngestFlow")
    url, raw, headers = _push({"id": "zzb4-evt-2"}, source="zzb4-flowsrc")
    await client.post(url, content=raw, headers=headers)
    second = await client.post(url, content=raw, headers=headers)
    assert second.json()["duplicate"] is True
    assert temporal_stub.start_workflow.await_count == 1


# ---------------------------------------------------------------------------
# A failed push must not keep its idempotency claim.
#
# The claim is what makes a replay a no-op, so a claim that outlives a FAILED
# handling burns that external_id permanently: the device's retry is answered
# `202 {"duplicate": true}` — indistinguishable from success — while nothing
# was ever recorded. Both lanes are covered: the flow dispatch and the inline
# recorder.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture(loop_scope="function")
async def client_500(db_pool, temporal_stub):
    """Same app as `client`, but an unhandled handler error becomes a real 500
    response (what uvicorn does in production) instead of being re-raised into
    the test, so the retry can be driven through the same client."""
    await _cleanup(db_pool)
    app = _build_app(db_pool, Settings(**_TEST_SETTINGS), temporal_stub)
    async with AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False),
        base_url="http://test",
    ) as c:
        yield c
    await _cleanup(db_pool)


async def _claim_exists(pool, source: str, external_id: str) -> bool:
    return bool(
        await pool.fetchval(
            "SELECT count(*) FROM ingest_idempotency "
            "WHERE source_type = $1 AND external_id = $2",
            f"life:{source}",
            external_id,
        )
    )


async def test_a_failed_temporal_dispatch_releases_the_claim(
    client_500, db_pool, temporal_stub, monkeypatch
):
    """A transient Temporal outage must not cost the device its external_id.

    Latent today only because every shipped source maps to `None`; it goes
    live the moment one is mapped to a workflow.
    """
    monkeypatch.setitem(webhooks.LIFE_SOURCES, "zzb4-flowsrc", "ZzB4IngestFlow")
    handle = MagicMock()
    handle.id = "wf-life-retry"
    temporal_stub.start_workflow = AsyncMock(
        side_effect=[RuntimeError("temporal unreachable"), handle]
    )

    url, raw, headers = _push({"id": "zzb4-evt-outage"}, source="zzb4-flowsrc")
    first = await client_500.post(url, content=raw, headers=headers)
    assert first.status_code == 500
    assert not await _claim_exists(db_pool, "zzb4-flowsrc", "zzb4-evt-outage")

    retry = await client_500.post(url, content=raw, headers=headers)
    assert retry.status_code == 202
    assert retry.json().get("duplicate") is None  # genuinely reprocessed
    assert retry.json()["workflow_id"] == "wf-life-retry"
    assert temporal_stub.start_workflow.await_count == 2


async def test_a_failed_inline_record_releases_the_claim(
    client_500, db_pool, monkeypatch
):
    """The inline lane released the claim only on ValueError/TypeError.

    An asyncpg failure — a closed connection partway through a health batch,
    say — is neither, so the claim survived a push that recorded nothing.
    `record_health_push` sits in this very try-block, and re-sending is safe
    because every ingest path writes through `record_external_observation`,
    whose per-sample `ON CONFLICT DO NOTHING` re-inserts only what is missing.
    """
    attempts: list[dict] = []

    async def _flaky(pool, **kwargs):
        attempts.append(kwargs)
        if len(attempts) == 1:
            raise ConnectionError("connection was closed in the middle of operation")
        return {"id": "00000000-0000-0000-0000-0000000000ff"}

    monkeypatch.setattr(webhooks, "record_observation", _flaky)

    url, raw, headers = _push(
        {"id": "zzb4-inline-outage", "metric": "zzb4-steps", "value": 7, "source": "zzb4-watch"}
    )
    first = await client_500.post(url, content=raw, headers=headers)
    assert first.status_code == 500
    assert not await _claim_exists(db_pool, SOURCE, "zzb4-inline-outage")

    retry = await client_500.post(url, content=raw, headers=headers)
    assert retry.status_code == 202
    assert retry.json().get("duplicate") is None  # genuinely reprocessed
    assert len(attempts) == 2  # the recorder really ran a second time
