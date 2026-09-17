"""Pure deterministic relevance scorer for ranking code repositories against alerts.

No external imports — stdlib only (re, dataclasses, typing).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

INDEPENDENT_SOURCES: frozenset[str] = frozenset(
    {"sentry", "github", "alertmanager", "prometheus", "grafana"}
)

STOPWORDS: frozenset[str] = frozenset(
    {"the", "data", "app", "api", "web", "src", "lib", "com", "www", "git", "github", "io"}
)

# Pre-compiled pattern: split on any run of non-alphanumeric characters
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
# Characters that also act as token boundaries within an alphanumeric token
_BOUNDARY_CHARS = re.compile(r"[.\/_-]")


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Candidate:
    resource_id: str
    label: str  # github_repo or path
    score: float


@dataclass(frozen=True)
class RelevanceResult:
    confident: bool
    resolved_resource_id: str
    candidates: list[Candidate]  # top-5 by score desc, always includes resolved


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> set[str]:
    """Lowercase, split on non-alphanumeric runs AND boundary chars, keep len>=3, drop stopwords."""
    text = text.lower()
    # First split on non-alnum runs
    raw_tokens: list[str] = _NON_ALNUM.split(text)
    expanded: list[str] = []
    for tok in raw_tokens:
        if not tok:
            continue
        # Further split on boundary characters (., /, _, -)
        sub = _BOUNDARY_CHARS.split(tok)
        expanded.extend(sub)
    return {t for t in expanded if len(t) >= 3 and t not in STOPWORDS}


def _score_resource(
    resource: dict,
    independent_service: bool,
    service_base: str,
    issue_tokens: set[str],
) -> tuple[str, str, float] | None:
    """Score a single resource against the alert.

    Returns (resource_id, label, score) or None if the resource should be skipped.
    """
    meta = resource.get("metadata") or {}
    # path is workspace-relative and may be nested ("acme/bcp") —
    # service names compare against its basename.
    path = str(meta.get("path") or "").strip().lower()
    path_base = path.rsplit("/", 1)[-1] if path else ""
    github_repo = str(meta.get("github_repo") or "").strip()
    repo_base = github_repo.rsplit("/", 1)[-1].lower() if github_repo else ""
    org = github_repo.rsplit("/", 1)[0].lower() if "/" in github_repo else ""

    # Skip resources with no useful identity
    if not path and not github_repo:
        return None

    identity_tokens = _tokenize(" ".join([path, repo_base, org]))
    score = 0.0

    # Independent-source exact match on service name
    if independent_service and service_base and service_base in {path_base, repo_base}:
        score += 1.0

    # Content overlap: issue tokens vs repo identity tokens
    content_hits = len(identity_tokens & issue_tokens)
    score += min(1.0, 0.5 * content_hits)

    label = github_repo if github_repo else path
    resource_id = str(resource.get("id") or "")
    return resource_id, label, score


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def score_resources(
    alert: dict,
    resources: list[dict],
    resolved_resource_id: str,
    hint: str = "",
) -> RelevanceResult:
    """Rank registered code resources against an alert.

    Parameters
    ----------
    alert:
        Alert dict with keys: source, title, description, service (all optional).
    resources:
        List of resource dicts shaped as
        {"id": str, "title": str, "metadata": {"path": str, "github_repo": str, ...}}.
        Caller should pass only repository-kind resources; non-repo entries (missing
        both path and github_repo) are skipped defensively.
    resolved_resource_id:
        The resource id that the caller already resolved (e.g. via a prior tier).
        Always present in the returned candidates list.

    Returns
    -------
    RelevanceResult
    """
    if not resources:
        return RelevanceResult(
            confident=False,
            resolved_resource_id=resolved_resource_id,
            candidates=[],
        )

    # Precompute alert-level values once
    source = (alert.get("source") or "").strip().lower()
    independent_service: bool = source in INDEPENDENT_SOURCES
    service = (alert.get("service") or "").strip()
    service_base = service.rsplit("/", 1)[-1].lower() if service else ""
    issue_tokens = _tokenize(f"{alert.get('title', '')} {alert.get('description', '')}")
    if hint:
        issue_tokens = issue_tokens | _tokenize(hint)

    # Score every resource
    scored: list[tuple[str, str, float]] = []  # (resource_id, label, score)
    for r in resources:
        result = _score_resource(r, independent_service, service_base, issue_tokens)
        if result is not None:
            scored.append(result)

    if not scored:
        return RelevanceResult(
            confident=False,
            resolved_resource_id=resolved_resource_id,
            candidates=[],
        )

    # Sort deterministically: score desc, label asc
    scored.sort(key=lambda x: (-x[2], x[1]))

    # Build Candidate objects
    all_candidates = [
        Candidate(resource_id=rid, label=label, score=score) for rid, label, score in scored
    ]

    # Find resolved candidate
    resolved_candidate: Candidate | None = None
    for c in all_candidates:
        if c.resource_id == resolved_resource_id:
            resolved_candidate = c
            break

    # Determine resolved_score
    resolved_score = resolved_candidate.score if resolved_candidate is not None else 0.0

    # Determine confidence:
    # resolved_score >= 0.5 AND no other repo has score >= resolved_score (strict argmax)
    confident = False
    if resolved_score >= 0.5:
        confident = not any(
            c.resource_id != resolved_resource_id and c.score >= resolved_score
            for c in all_candidates
        )

    if hint:
        # A hint round always re-presents Gate-0 for the operator to confirm,
        # never silently auto-resolves.
        confident = False

    # Build top-5 candidates; ensure resolved is present
    top = all_candidates[:5]
    resolved_in_top = any(c.resource_id == resolved_resource_id for c in top)
    if not resolved_in_top and resolved_candidate is not None:
        # Replace the 5th entry (index 4) with resolved
        top = top[:4] + [resolved_candidate]

    return RelevanceResult(
        confident=confident,
        resolved_resource_id=resolved_resource_id,
        candidates=top,
    )
