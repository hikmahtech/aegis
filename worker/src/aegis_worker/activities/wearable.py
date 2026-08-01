"""WearableActivities — poll a wearable vendor's REST API into life.observations.

The transport half of the "how did I sleep / how active was I" signal, for
vendors that expose an API but no webhook (Oura today; Whoop/Withings slot in
by adding one entry to `_VENDORS`).

Three things this module is careful about, because none of them can be tested
against the real vendor:

* **Fail closed and *visibly*.** No token, or a vendor nobody has written a
  mapping for, returns a result with an explicit `status` — never an empty
  "ok". The flow puts that status in `result_summary`, so an unconfigured
  install reads as `token_missing` on the Flows page rather than as a run that
  quietly found nothing. It never issues a request with an empty token.
* **A bad response degrades to a skipped run.** Every per-endpoint fetch is
  wrapped: a 429, a 500, a timeout or a payload that is not the shape the
  vendor documents increments `errors` and moves on. The activity itself does
  not raise, so the workflow does not fail; the flow refuses to advance the
  cursor while `errors > 0`, so nothing is skipped over.
* **Idempotency is not this module's job.** Records carry the vendor's own id
  as `external_id`; `record_external_observation` turns that into a unique-index
  conflict. Polling the same window ten times writes the rows once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import httpx
import structlog
from temporalio import activity

logger = structlog.get_logger()

# Oura API v2. A personal access token authenticates as `Bearer <token>`.
_OURA_BASE = "https://api.ouraring.com/v2/usercollection"

# endpoint -> ((vendor field, aegis metric), ...). One vendor record commonly
# yields several metrics; each becomes its own observation row, distinguished
# by `metric` in the dedup key.
_OURA_METRICS: dict[str, tuple[tuple[str, str], ...]] = {
    "daily_sleep": (("score", "sleep_score"),),
    "daily_readiness": (("score", "readiness_score"),),
    "daily_activity": (("score", "activity_score"), ("steps", "steps")),
}

_VENDORS = ("oura",)

# Runaway guards. A wearable publishes a handful of daily summaries, so these
# are far above any real volume and exist only to stop a misbehaving API (an
# endless `next_token` chain, a cursor from 2019) from looping or from pulling
# years of history in one run.
_MAX_PAGES = 5
_MAX_LOOKBACK_DAYS = 30
_MAX_RECORDS = 1000

_DEFAULT_LOOKBACK_DAYS = 7
_HTTP_TIMEOUT = 30.0


@dataclass
class PollWearableInput:
    vendor: str = "oura"
    # ISO date (YYYY-MM-DD) of the latest fully-resolved day, or None on the
    # first ever poll. Inclusive: the cursor day is re-fetched so a partially
    # written day heals, and the re-read rows dedup on write.
    since_cursor: str | None = None
    lookback_days: int = _DEFAULT_LOOKBACK_DAYS


@dataclass
class PollWearableResult:
    # ok | token_missing | unsupported_vendor | fetch_failed
    status: str = "ok"
    records: list[dict] = field(default_factory=list)
    # Number of vendor endpoints that could not be read at all this run.
    errors: int = 0
    detail: str = ""


@dataclass
class RecordWearableInput:
    source: str
    records: list[dict] = field(default_factory=list)


@dataclass
class RecordWearableResult:
    written: int = 0
    duplicates: int = 0
    failed: int = 0
    # Highest day (YYYY-MM-DD) safe to move the cursor to: every record on it
    # and before it reached a definite outcome (written or already present).
    latest_resolved_day: str | None = None


def _window(since_cursor: str | None, lookback_days: int, today: date) -> tuple[date, date]:
    """[start, end] dates to request, clamped to `_MAX_LOOKBACK_DAYS`.

    A stuck cursor (the flow holds it back whenever an endpoint errors) must
    not grow the request window without bound, so the floor wins over it.
    """
    lookback = max(1, min(int(lookback_days or _DEFAULT_LOOKBACK_DAYS), _MAX_LOOKBACK_DAYS))
    floor = today - timedelta(days=_MAX_LOOKBACK_DAYS)
    start = today - timedelta(days=lookback)
    if since_cursor:
        try:
            start = date.fromisoformat(str(since_cursor)[:10])
        except (TypeError, ValueError):
            logger.warning("wearable_cursor_unparseable", cursor=str(since_cursor)[:32])
    return max(start, floor), today


def _observed_at(item: dict, day: str) -> str:
    """ISO timestamp for a vendor record: its own `timestamp`, else the day."""
    ts = item.get("timestamp")
    if isinstance(ts, str) and ts.strip():
        return ts.strip()
    return f"{day}T00:00:00+00:00"


def _oura_records(endpoint: str, items: list, source: str) -> list[dict]:
    """Vendor payload rows -> observation records. Unusable rows are dropped."""
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        day = str(item.get("day") or "")[:10]
        item_id = str(item.get("id") or "")
        if not day or not item_id:
            continue
        for field_name, metric in _OURA_METRICS[endpoint]:
            raw = item.get(field_name)
            if raw is None:
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            out.append(
                {
                    "external_id": item_id,
                    "metric": metric,
                    "value": value,
                    "day": day,
                    "observed_at": _observed_at(item, day),
                    "metadata": {"vendor": source, "endpoint": endpoint},
                }
            )
    return out


@dataclass
class WearableActivities:
    oura_api_token: str = ""
    db_pool: Any = None
    http_client: httpx.AsyncClient | None = None

    def _token(self, vendor: str) -> str:
        return {"oura": self.oura_api_token}.get(vendor, "") or ""

    @activity.defn
    async def poll_wearable(self, input: PollWearableInput) -> PollWearableResult:
        vendor = (input.vendor or "").strip().lower()
        if vendor not in _VENDORS:
            logger.warning("wearable_vendor_unsupported", vendor=vendor[:32])
            return PollWearableResult(
                status="unsupported_vendor",
                detail=f"no metric mapping for vendor {vendor!r}",
            )

        token = self._token(vendor)
        if not token:
            # Fail closed, loudly. Never issue a request with an empty token.
            logger.warning("wearable_token_missing", vendor=vendor)
            return PollWearableResult(
                status="token_missing",
                detail=f"{vendor}_api_token is not configured (admin → Integrations)",
            )

        start, end = _window(input.since_cursor, input.lookback_days, datetime.now(UTC).date())
        client = self.http_client or httpx.AsyncClient(timeout=_HTTP_TIMEOUT)
        records: list[dict] = []
        errors = 0
        details: list[str] = []

        try:
            for endpoint in _OURA_METRICS:
                try:
                    items = await self._fetch_oura(client, token, endpoint, start, end)
                except Exception as exc:
                    # 429 / 5xx / timeout / bad JSON all land here. One dead
                    # endpoint must not lose the other two, and must not fail
                    # the workflow — the flow reads `errors` and holds the
                    # cursor instead.
                    errors += 1
                    details.append(f"{endpoint}: {type(exc).__name__}")
                    logger.warning(
                        "wearable_endpoint_failed",
                        vendor=vendor,
                        endpoint=endpoint,
                        error=str(exc)[:200],
                    )
                    continue
                records.extend(_oura_records(endpoint, items, vendor))
        finally:
            if self.http_client is None:
                await client.aclose()

        if len(records) > _MAX_RECORDS:
            logger.warning("wearable_record_cap_hit", vendor=vendor, count=len(records))
            records = records[:_MAX_RECORDS]

        status = "fetch_failed" if errors and not records else "ok"
        logger.info(
            "wearable_poll_done",
            vendor=vendor,
            status=status,
            records=len(records),
            errors=errors,
            start=start.isoformat(),
            end=end.isoformat(),
        )
        return PollWearableResult(
            status=status,
            records=records,
            errors=errors,
            detail="; ".join(details)[:400],
        )

    async def _fetch_oura(
        self,
        client: httpx.AsyncClient,
        token: str,
        endpoint: str,
        start: date,
        end: date,
    ) -> list:
        """All items of one Oura v2 collection in [start, end], paginated."""
        params: dict[str, Any] = {
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
        }
        items: list = []
        for _ in range(_MAX_PAGES):
            resp = await client.get(
                f"{_OURA_BASE}/{endpoint}",
                headers={"Authorization": f"Bearer {token}"},
                params=params,
            )
            resp.raise_for_status()
            body = resp.json()
            if not isinstance(body, dict):
                raise ValueError(f"{endpoint} returned {type(body).__name__}, expected object")
            page = body.get("data")
            if not isinstance(page, list):
                raise ValueError(f"{endpoint} 'data' is {type(page).__name__}, expected list")
            items.extend(page)
            next_token = body.get("next_token")
            if not next_token:
                return items
            params = {**params, "next_token": next_token}
        logger.warning("wearable_poll_hit_page_cap", endpoint=endpoint, pages=_MAX_PAGES)
        return items

    @activity.defn
    async def record_wearable_observations(
        self, input: RecordWearableInput
    ) -> RecordWearableResult:
        """Write poll records to `life.observations`, deduped by external id.

        `latest_resolved_day` is the cursor the flow may safely advance to:
        the newest day with no unresolved record on or before it. A single
        write failure therefore pins the cursor below its day, leaving that
        day inside the next tick's window — the same rule `rss_ingest`
        applies per entry.
        """
        result = RecordWearableResult()
        if not self.db_pool or not input.records:
            return result

        from aegis.services.observations import record_external_observation

        resolved_days: set[str] = set()
        failed_days: set[str] = set()
        for rec in input.records:
            day = str(rec.get("day") or "")[:10]
            try:
                row = await record_external_observation(
                    self.db_pool,
                    source=input.source,
                    metric=str(rec.get("metric") or ""),
                    external_id=str(rec.get("external_id") or ""),
                    value=rec.get("value"),
                    observed_at=_parse_dt(rec.get("observed_at")),
                    metadata=dict(rec.get("metadata") or {}),
                )
            except Exception as exc:
                result.failed += 1
                if day:
                    failed_days.add(day)
                logger.warning(
                    "wearable_observation_write_failed",
                    metric=str(rec.get("metric") or "")[:64],
                    day=day,
                    error=str(exc)[:200],
                )
                continue
            if row is None:
                result.duplicates += 1
            else:
                result.written += 1
            if day:
                resolved_days.add(day)

        candidates = resolved_days
        if failed_days:
            floor = min(failed_days)
            candidates = {d for d in resolved_days if d < floor}
        result.latest_resolved_day = max(candidates) if candidates else None

        logger.info(
            "wearable_observations_recorded",
            source=input.source,
            written=result.written,
            duplicates=result.duplicates,
            failed=result.failed,
            cursor=result.latest_resolved_day,
        )
        return result


def _parse_dt(raw: Any) -> datetime | None:
    """ISO string -> aware datetime. None when unusable (caller defaults to now)."""
    if isinstance(raw, datetime):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
