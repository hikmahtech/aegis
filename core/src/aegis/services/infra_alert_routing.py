"""Which alerts count as infrastructure, and which repo investigates them.

An infra alert has no application code behind it: a node left the swarm, a
service is below its replicas. AlertInvestigationFlow therefore skips the repo
match for it, tries the one safe force-restart, and investigates in the infra
repo (the one holding the swarm or ansible config) without staging a fix.

Stored in the ``settings`` table under ``infra_alert_routing``::

    {"extra_alertnames": ["dagster pipeline failure", "clickhousedown"],
     "repo": "acme/infra-gitops",
     "platform_hint": "This cluster is Docker Swarm. Read it with "
                      "`docker --context swarm node ls` and "
                      "`docker --context swarm service ps <service>`."}

- ``extra_alertnames`` are ADDED to ``DEFAULT_INFRA_ALERTNAMES``. Alertnames are
  compared stripped and lowercased.
- ``repo`` is the ``owner/name`` (``resources.metadata.github_repo``) of the
  repository resource infra alerts are investigated in. Empty means none: the
  flow falls back to an LLM-only investigation. It is also the repo a connector
  or service alert expands to, since the config that deploys a thing is as
  likely to be at fault as the thing (#505).
- ``platform_hint`` is one or two sentences telling the investigating agent
  what the cluster IS and how to read it. The generic instructions name no
  orchestrator, because AEGIS does not know whether you run Swarm, k8s, Nomad
  or a handful of systemd units — this is where you say (#505). Empty is fine;
  the agent then works it out from the infra repo.

The defaults are generic: the alerts AEGIS's own heartbeat raises, plus the
usual host, container and monitoring-stack alerts. Anything that only makes
sense for one deployment (a Dagster alert, a ClickHouse alert, the infra repo's
name) belongs in the row. It used to be Python constants in the worker, which
meant this public repository shipped one operator's alert rules and repo (#498).

A repository can still pull one of these alerts away from the infra repo by
claiming it with ``resources.metadata.alert_labels``; see
``aegis_worker.activities.alerts``.

Read is lenient and write is strict, like ``project_repo_map`` and
``content_routes``: a malformed row must never stop an alert being
investigated, but a typo must not save and then silently do nothing.
"""

from __future__ import annotations

import re
import time
from typing import Any

SETTINGS_KEY = "infra_alert_routing"

DEFAULT_INFRA_ALERTNAMES: frozenset[str] = frozenset(
    {
        # Raised by AEGIS itself (InfraHeartbeatFlow), so infra everywhere.
        # ServiceDownProlonged is also Prometheus' 2h escalation of
        # DockerServiceDown, and the force-restart only runs on this branch (#138).
        "nodedown",
        "dockerservicedown",
        "servicedownprolonged",
        "heartbeatcollectfailed",
        # The way in to AEGIS stopped answering from outside (#492). Infra by
        # definition: it is the proxy or the route, never application code.
        "ingressunreachable",
        # The monitoring stack and its database.
        "prometheusdown",
        "alertmanagerdown",
        "lokidown",
        "postgresqldown",
        # Host and container resource alerts.
        "hostoutofmemory",
        "hostmemorylimitreached",
        "hostdiskspacefull",
        "hostdiskreadlatency",
        "hostdiskwritelatency",
        "containermemorylimitreached",
        "containerkilledbysigterm",
        "containerkilledbysigkill",
    }
)

# owner/repo — GitHub's own character set for both halves.
_REPO_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")

_WRITABLE_KEYS = frozenset({"extra_alertnames", "repo", "platform_hint"})
# A hint longer than this is a prompt, not a hint: the investigation's context
# has a budget, and this text goes in front of everything else.
_HINT_CAP = 1000
# What GET adds on top of the stored row. Accepted and ignored on PUT so the
# admin page can send back what it read.
_COMPUTED_KEYS = frozenset({"alertnames", "default_alertnames"})

_CACHE_SECONDS = 30.0
_cache: dict = {"value": None, "ts": 0.0}


def _norm(name: str) -> str:
    return name.strip().lower()


def validate(raw: Any) -> dict:
    """Normalise the stored row, or raise ValueError (the PUT answers 400)."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("infra_alert_routing must be an object")
    unknown = set(raw) - _WRITABLE_KEYS - _COMPUTED_KEYS
    if unknown:
        raise ValueError(f"unknown key(s): {sorted(unknown)}; allowed: {sorted(_WRITABLE_KEYS)}")
    names_raw = raw.get("extra_alertnames")
    if names_raw is None:
        names_raw = []
    if not isinstance(names_raw, list):
        raise ValueError("extra_alertnames must be a list of alert names")
    names: set[str] = set()
    for name in names_raw:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("extra_alertnames: every entry must be a non-empty string")
        names.add(_norm(name))
    repo = str(raw.get("repo") or "").strip()
    if repo and not _REPO_RE.match(repo):
        raise ValueError(f"repo must be 'owner/name', got {repo!r}")
    hint_raw = raw.get("platform_hint") or ""
    if not isinstance(hint_raw, str):
        raise ValueError("platform_hint must be a string")
    hint = hint_raw.strip()
    if len(hint) > _HINT_CAP:
        raise ValueError(f"platform_hint must be at most {_HINT_CAP} characters")
    return {"extra_alertnames": sorted(names), "repo": repo, "platform_hint": hint}


def merge(value: Any) -> dict:
    """The effective routing for a stored row. Never raises: each field that
    cannot be read falls back on its own, so a bad repo keeps the names."""
    v = value if isinstance(value, dict) else {}
    names_raw = v.get("extra_alertnames")
    extra = sorted(
        {_norm(n) for n in names_raw if isinstance(n, str) and n.strip()}
        if isinstance(names_raw, list)
        else set()
    )
    repo = v.get("repo")
    repo = repo.strip() if isinstance(repo, str) else ""
    if not _REPO_RE.match(repo):
        repo = ""
    hint = v.get("platform_hint")
    hint = hint.strip()[:_HINT_CAP] if isinstance(hint, str) else ""
    return {
        "alertnames": sorted(DEFAULT_INFRA_ALERTNAMES | set(extra)),
        "extra_alertnames": extra,
        "repo": repo,
        "platform_hint": hint,
    }


async def get_infra_alert_routing(pool: Any, *, cached: bool = True) -> dict:
    """The effective routing, cached for 30s. The defaults when there is no pool
    or the read fails: routing config must never stop an alert being handled."""
    if pool is None:
        return merge(None)
    now = time.monotonic()
    if cached and _cache["value"] is not None and now - _cache["ts"] < _CACHE_SECONDS:
        return _cache["value"]
    try:
        row = await pool.fetchrow("SELECT value FROM settings WHERE key = $1", SETTINGS_KEY)
    except Exception:  # noqa: BLE001 — a config read is best-effort, never fatal
        return merge(None)
    value = merge(row["value"] if row else None)
    _cache.update(value=value, ts=now)
    return value


async def save_infra_alert_routing(pool: Any, raw: Any) -> dict:
    """Replace the row (validated); returns the effective routing."""
    stored = validate(raw)
    await pool.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()",
        SETTINGS_KEY,
        stored,
    )
    _cache.update(value=None, ts=0.0)
    return merge(stored)
