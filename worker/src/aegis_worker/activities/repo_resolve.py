"""Match a coding task's text to a coding-enabled repository.

`AgentTaskActivities.resolve_task_repo` uses this as its tier 2, when the
task's Todoist project names no repo. It is what remained of the alert
investigation's resource resolver after the infra lane moved to the DevOps
vertical (a2-devops): a task has no alert labels, no service and no knowledge
graph claim, so only two tiers ever applied to it, and only they are kept:

1. a deterministic token match on the title and description (one repo, or
   none);
2. the model, asked to pick up to three repos from the coding-enabled list.

The input is alert-shaped (`title`, `description`, `fingerprint`, `service`)
so the matchers keep their tested contract. Only repositories with
`metadata.coding_enabled = 'true'` are ever candidates: a match here can start
a coding run.
"""

from __future__ import annotations

import re
from typing import Any

from aegis.llm import parse_llm_json
from temporalio import activity

from aegis_worker.shared.jsonb import decode_jsonb

_NULL_RESULT: dict = {
    "resource_id": None,
    "resource_title": None,
    "resource_path": None,
    "github_repo": "",
    "confidence": 0.0,
    "source": "none",
    "resources": [],
}

_CODING_GATE = "kind = 'repository' AND metadata->>'coding_enabled' = 'true'"


def _decode_metadata(row: Any) -> dict:
    """A resource row's `metadata` as a dict, `{}` when it will not decode —
    an unreadable row must not stop the task being routed."""
    try:
        return decode_jsonb(row["metadata"], {})
    except (ValueError, TypeError):
        return {}


def _coding_match(rid: Any, title: Any, meta: dict, confidence: float) -> dict:
    """Build a resource-match dict carrying resource-scoped coding routing.

    `engine` ('claude'|'kimi'|'') and `claude_account` (a CLAUDE_CONFIG_DIR
    account label; kimi ignores it) come from the resource's metadata and let
    the caller pin the coding run's engine + profile per repo.
    """
    meta = meta if isinstance(meta, dict) else {}
    return {
        "resource_id": str(rid),
        "resource_title": title,
        "resource_path": meta.get("path"),
        "github_repo": (meta.get("github_repo") or "").strip(),
        "engine": (meta.get("engine") or "").strip().lower(),
        "claude_account": (meta.get("claude_account") or "").strip(),
        "confidence": confidence,
    }


# ---------------------------------------------------------------------------
# Deterministic token match
# ---------------------------------------------------------------------------

_MATCH_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Vocabulary common enough across BOTH many alerts and many repo names that a
# shared token alone is coincidental, not identifying — e.g. "pipeline"
# appears in almost every Dagster alert title AND in a repo literally named
# "*-pipeline"; matching on it would false-positive. Deliberately small/local
# to this matcher, not a general stopword list.
#
# Every word here is language or alert vocabulary. A PRODUCT name does not
# belong: `dagster` was in the list, which is one deployment's stack in an
# open-source repo, and it stopped a fork whose repo is named after its
# orchestrator from ever token-matching (#505).
_GENERIC_MATCH_TOKENS = frozenset(
    {
        "the", "a", "an", "is", "of", "to", "in", "on", "for", "and", "or",
        "down", "up", "unreachable", "failed", "failure", "error", "errors",
        "alert", "critical", "warning", "warn", "service", "endpoint", "repo",
        "pipeline", "job", "run", "http", "https", "www",
        "com", "org", "io", "net", "unknown", "none", "true", "false",
        "prod", "production", "staging", "dev", "class", "type", "message",
    }
)

# Tokens shorter than this are dropped — short fragments ("em", "io") are too
# likely to coincidentally appear in an unrelated resource's name.
_MIN_MATCH_TOKEN_LEN = 4

# Alert label keys that describe SCOPE/SEVERITY/CATEGORY rather than
# identity. `alertname` in particular is a class, not an instance — e.g.
# "Dagster Pipeline Failure" fires for every Dagster pipeline in every repo,
# so including it would make every such alert token-match every repo whose
# name contains "pipeline".
#
# `run_id` stays (#505 asked): the key NAMES an identifier of one run, so it is
# non-identifying of a repo by construction, whoever emits it — the same reason
# `job` and `grafana_folder` are here. Unlike a product name, it carries no
# deployment's vocabulary.
_NON_IDENTIFYING_LABEL_KEYS = frozenset(
    {"alertname", "severity", "cluster", "environment", "job", "grafana_folder", "run_id"}
)


def _match_tokens(*values: Any) -> set[str]:
    """Lowercase + tokenize a set of strings into meaningful matching words.

    Shared by both the alert side and the resource side of
    `_deterministic_resource_match` so both use identical normalization.
    """
    tokens: set[str] = set()
    for value in values:
        if not value:
            continue
        for word in _MATCH_TOKEN_RE.findall(str(value).lower()):
            if len(word) >= _MIN_MATCH_TOKEN_LEN and word not in _GENERIC_MATCH_TOKENS:
                tokens.add(word)
    return tokens


def _alert_match_tokens(alert: dict) -> set[str]:
    """Identifying tokens pulled from the alert's title/service/labels."""
    labels = alert.get("labels") or {}
    if not isinstance(labels, dict):
        labels = {}
    label_values = [
        v
        for k, v in labels.items()
        if k not in _NON_IDENTIFYING_LABEL_KEYS and isinstance(v, str)
    ]
    return _match_tokens(alert.get("title"), alert.get("service"), *label_values)


def _resource_match_tokens(title: Any, meta: dict) -> set[str]:
    """Identifying tokens for one resources row: title, github_repo, path."""
    return _match_tokens(title, meta.get("github_repo"), meta.get("path"))


def _deterministic_resource_match(alert: dict, rows: list) -> dict | None:
    """Free-text token overlap between the task (alert-shaped dict) and a
    candidate resource's title/github_repo/path.

    Returns the single unambiguously-matched resource, or None when zero or
    multiple candidate resources share a token with it: ambiguity always falls
    through to the LLM tier rather than guessing.
    """
    alert_tokens = _alert_match_tokens(alert)
    if not alert_tokens:
        return None
    matches: list[tuple[Any, dict]] = []
    for row in rows:
        meta = _decode_metadata(row)
        if alert_tokens & _resource_match_tokens(row["title"], meta):
            matches.append((row, meta))
    if len(matches) != 1:
        return None
    row, meta = matches[0]
    return _coding_match(row["id"], row["title"], meta, 1.0)


async def resolve_repo_by_text(
    db_pool: Any, llm_client: Any, model: str, alert: dict
) -> dict:
    """Map a task's text to matching repositories: the token match, then the model.

    Returns `resource_id`, `resource_title`, `resource_path`, `github_repo`,
    `confidence`, `source` ("deterministic" | "llm" | "llm_unconfirmed" |
    "none") and a `resources` list of up to three candidates.
    """
    if not db_pool:
        return dict(_NULL_RESULT)
    rows = await db_pool.fetch(
        f"SELECT id, title, kind, url, metadata FROM resources WHERE {_CODING_GATE} ORDER BY title"
    )
    deterministic = _deterministic_resource_match(alert, rows)
    if deterministic is not None:
        return {**deterministic, "source": "deterministic", "resources": [deterministic]}
    if not llm_client:
        return dict(_NULL_RESULT)

    resource_lines = []
    for row in rows:
        meta = _decode_metadata(row)
        resource_lines.append(
            f"- id={row['id']} title={row['title']} kind={row.get('kind', '')} "
            f"path={meta.get('path', '')}"
        )
    title = alert.get("title", "")
    description = alert.get("description", "")
    prompt = (
        "You are matching a task to the code repository it is about.\n\n"
        "Task:\n"
        f"  Title: {title}\n"
        f"  Description: {description[:500]}\n\n"
        "Available resources:\n" + "\n".join(resource_lines) + "\n\n"
        'Return JSON only: {"resources": [{"resource_id": "<id>", "resource_title": '
        '"<title>", "confidence": <0.0-1.0>}, ...]}\n'
        "Return up to 3 resources ordered by relevance. Only include resources with "
        "confidence >= 0.5. Return an empty list if nothing matches."
    )
    llm_result = await llm_client.think(
        prompt,
        model=model,
        system_prompt="You map work items to the code repositories they concern.",
        db_pool=db_pool,
        # The alert resolver's purpose name, kept so a deployment's LLM routes
        # (`settings.llm_backend`, then `config/models.yaml`) still apply.
        purpose="alert_resource_resolution",
        agent_id=alert.get("agent_id"),
    )
    parsed = parse_llm_json(llm_result.get("response", ""))
    if not isinstance(parsed, dict):
        activity.logger.warning("resolve_repo_parse_failed")
        return dict(_NULL_RESULT)
    raw_resources = parsed.get("resources") or []
    if not isinstance(raw_resources, list):
        return dict(_NULL_RESULT)
    # Confident picks clear the 0.5 bar. When none do, keep the top
    # sub-threshold picks as "llm_unconfirmed" candidates: the caller asks the
    # user instead of guessing.
    confident = [r for r in raw_resources if float(r.get("confidence", 0.0)) >= 0.5]
    if confident:
        raw_resources, source = confident[:3], "llm"
    else:
        raw_resources = sorted(
            raw_resources, key=lambda r: float(r.get("confidence", 0.0)), reverse=True
        )[:3]
        source = "llm_unconfirmed"
    rows_by_id = {str(row["id"]): row for row in rows}
    enriched: list[dict] = []
    for r in raw_resources:
        rid = str(r.get("resource_id", ""))
        row = rows_by_id.get(rid)
        if not row:
            continue
        meta = _decode_metadata(row)
        enriched.append(
            {
                **_coding_match(
                    rid,
                    r.get("resource_title") or row["title"],
                    meta,
                    float(r.get("confidence", 0.0)),
                ),
                "resource_path": meta.get("path") or "",
            }
        )
    if not enriched:
        return dict(_NULL_RESULT)
    primary = enriched[0]
    return {
        "resource_id": primary["resource_id"],
        "resource_title": primary["resource_title"],
        "resource_path": primary["resource_path"] or None,
        "github_repo": primary.get("github_repo", ""),
        "confidence": primary["confidence"],
        "source": source,
        "resources": enriched,
    }
