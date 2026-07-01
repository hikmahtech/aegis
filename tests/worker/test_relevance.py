"""Unit tests for worker/src/aegis_worker/relevance.py.

All tests are synchronous / pure — no asyncio needed.
"""


from aegis_worker.relevance import (
    Candidate,
    RelevanceResult,
    _tokenize,
    score_resources,
)

# ---------------------------------------------------------------------------
# Shared fixtures — realistic resource dicts
# ---------------------------------------------------------------------------

BCP = {
    "id": "res-bcp",
    "title": "BCP",
    "metadata": {"path": "bcp", "github_repo": "acme/bcp"},
}

AEGIS = {
    "id": "res-aegis",
    "title": "AEGIS",
    "metadata": {"path": "aegis", "github_repo": "youruser/aegis"},
}

TSP = {
    "id": "res-tsp",
    "title": "Trading System Pipeline",
    "metadata": {
        "path": "trading-system-pipeline",
        "github_repo": "youruser/trading-system-pipeline",
    },
}

ALL_REPOS = [BCP, AEGIS, TSP]


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _ids(candidates: list[Candidate]) -> list[str]:
    return [c.resource_id for c in candidates]


# ---------------------------------------------------------------------------
# Test 1: Sentry independent-service exact match → confident True
# ---------------------------------------------------------------------------


def test_sentry_exact_service_match_confident():
    """source=sentry + service=bcp should produce an exact service match for BCP
    and score 0 for all other repos, yielding confident=True."""
    alert = {
        "source": "sentry",
        "service": "bcp",
        "title": "TypeError in request handler",
        "description": "Something broke",
    }
    result = score_resources(alert, ALL_REPOS, resolved_resource_id="res-bcp")

    assert isinstance(result, RelevanceResult)
    assert result.confident is True
    assert result.resolved_resource_id == "res-bcp"

    # BCP must be first candidate with score 1.0 (or above if content also matches)
    bcp_candidate = next(c for c in result.candidates if c.resource_id == "res-bcp")
    assert bcp_candidate.score >= 1.0

    # Other repos must have score 0 from the independent-service signal
    for c in result.candidates:
        if c.resource_id != "res-bcp":
            assert c.score < 1.0


# ---------------------------------------------------------------------------
# Test 2: Chat/todoist source — service not independent, content match → confident True
# ---------------------------------------------------------------------------


def test_chat_source_content_match_confident():
    """source=todoist-chat is NOT independent; service=bcp alone shouldn't give +1.0.
    But title/description mentioning 'bcp' gives a content hit → score=0.5, unique top
    → confident=True."""
    alert = {
        "source": "todoist-chat",
        "service": "bcp",
        "title": "Issue in bcp service",
        "description": "The bcp module threw an error",
    }
    result = score_resources(alert, ALL_REPOS, resolved_resource_id="res-bcp")

    assert result.confident is True
    bcp_candidate = next(c for c in result.candidates if c.resource_id == "res-bcp")
    assert bcp_candidate.score >= 0.5


# ---------------------------------------------------------------------------
# Test 3: Chat wrong-pick — resolved=bcp but description matches TSP more
# ---------------------------------------------------------------------------


def test_chat_wrong_pick_not_confident():
    """resolved=bcp, but description mentions trading pipeline tokens.
    TSP should score higher; bcp should score low → confident=False.
    TSP should appear first; bcp must still be present in candidates."""
    alert = {
        "source": "todoist-chat",
        "service": "bcp",
        "title": "pipeline failure in trading system",
        "description": "The trading pipeline encountered an error in the pipeline module",
    }
    # "trading" → hits TSP path/repo_base tokens
    # "pipeline" → hits TSP path token
    # "bcp" does NOT appear in title/description
    result = score_resources(alert, ALL_REPOS, resolved_resource_id="res-bcp")

    assert result.confident is False

    tsp_candidate = next(c for c in result.candidates if c.resource_id == "res-tsp")
    bcp_candidate = next(c for c in result.candidates if c.resource_id == "res-bcp")

    # TSP should outscore BCP
    assert tsp_candidate.score > bcp_candidate.score

    # TSP is ranked first
    assert result.candidates[0].resource_id == "res-tsp"

    # BCP must still be present (resolved always present)
    assert "res-bcp" in _ids(result.candidates)


# ---------------------------------------------------------------------------
# Test 4: Tie — two repos both score same and resolved is one of them → not confident
# ---------------------------------------------------------------------------


def test_tie_not_confident():
    """When two repos both match at the same score (including resolved), confident=False."""
    # Craft an alert that hits both "bcp" and "aegis" tokens equally
    alert = {
        "source": "sentry",
        "service": "",  # no exact match, only content
        "title": "bcp aegis connection error",
        "description": "",
    }
    # With independent_service=True but service_base="" → no +1.0 bonus
    # Both "bcp" and "aegis" appear in issue_tokens and match their respective identity_tokens
    # → each gets score=0.5 (1 hit × 0.5)
    result = score_resources(alert, [BCP, AEGIS], resolved_resource_id="res-bcp")

    # Both score 0.5 → tie → not confident
    bcp_candidate = next(c for c in result.candidates if c.resource_id == "res-bcp")
    aegis_candidate = next(c for c in result.candidates if c.resource_id == "res-aegis")
    assert bcp_candidate.score == aegis_candidate.score
    assert result.confident is False


# ---------------------------------------------------------------------------
# Test 5: Empty resources → confident False, candidates []
# ---------------------------------------------------------------------------


def test_empty_resources():
    alert = {"source": "sentry", "service": "bcp", "title": "crash", "description": ""}
    result = score_resources(alert, [], resolved_resource_id="res-bcp")

    assert result.confident is False
    assert result.resolved_resource_id == "res-bcp"
    assert result.candidates == []


# ---------------------------------------------------------------------------
# Test 6: Stopword / short token noise
# ---------------------------------------------------------------------------


def test_stopwords_not_matched():
    """A description made entirely of stopwords must NOT produce content hits.
    'data-platform' repo path splits into tokens {'data', 'platform'};
    'data' is a stopword so it gets dropped. A description 'the api web data' has
    only stopwords → issue_tokens is empty → score=0."""
    data_platform = {
        "id": "res-dp",
        "title": "Data Platform",
        "metadata": {"path": "data-platform", "github_repo": "org/data-platform"},
    }
    alert = {
        "source": "prometheus",
        "service": "",
        "title": "the api web data",
        "description": "the web app",
    }
    result = score_resources(alert, [data_platform], resolved_resource_id="res-dp")

    dp_candidate = next(c for c in result.candidates if c.resource_id == "res-dp")
    # "data" and "platform": "data" is a stopword, "platform" doesn't appear in issue_tokens
    assert dp_candidate.score == 0.0
    assert result.confident is False


def test_stopwords_dropped_from_issue_tokens():
    """Verify _tokenize drops stopwords and short tokens properly."""
    tokens = _tokenize("the api web data app")
    assert tokens == set(), f"Expected empty set, got {tokens}"

    tokens2 = _tokenize("trading-system-pipeline failure")
    assert "trading" in tokens2
    assert "system" in tokens2
    assert "pipeline" in tokens2
    assert "failure" in tokens2
    # 'api' and 'the' are stopwords
    assert "api" not in tokens2
    assert "the" not in tokens2


# ---------------------------------------------------------------------------
# Test 7: Top-5 cap — 8 repos → candidates length <= 5 and resolved always present
# ---------------------------------------------------------------------------


def test_top5_cap_resolved_present():
    """With 8 repos, candidates must be ≤5 long; resolved must always be present."""
    extras = [
        {
            "id": f"res-extra-{i}",
            "title": f"Extra Repo {i}",
            "metadata": {"path": f"extra-repo-{i}", "github_repo": f"org/extra-repo-{i}"},
        }
        for i in range(5)
    ]
    eight_repos = ALL_REPOS + extras  # 3 + 5 = 8

    alert = {
        "source": "sentry",
        "service": "bcp",
        "title": "crash in bcp service",
        "description": "",
    }
    # BCP gets service-match +1.0 → should be top; resolved=res-bcp
    result = score_resources(alert, eight_repos, resolved_resource_id="res-bcp")

    assert len(result.candidates) <= 5
    assert "res-bcp" in _ids(result.candidates)
    assert result.confident is True


def test_top5_cap_resolved_not_in_natural_top5():
    """When resolved scores 0 and 5 other repos score higher, resolved replaces slot 5."""
    # Make resolved = AEGIS which scores 0 while others (extras) all score 0.5+
    # We need >=5 repos that all outscore aegis so it falls off the natural top-5
    extras = [
        {
            "id": f"res-extra-{i}",
            "title": "Extra Service",
            "metadata": {
                "path": "bcp",  # same path as BCP to get content hit
                "github_repo": f"org/bcp-extra-{i}",
            },
        }
        for i in range(5)
    ]
    all_repos = [AEGIS] + extras  # aegis + 5 extras

    alert = {
        "source": "sentry",
        "service": "bcp",
        "title": "issue in bcp module",
        "description": "",
    }
    # All extras have path=bcp → service_base=bcp hit → +1.0
    # AEGIS has path=aegis → no match → 0.0
    result = score_resources(alert, all_repos, resolved_resource_id="res-aegis")

    # AEGIS scores 0 but must still be in candidates
    assert "res-aegis" in _ids(result.candidates)
    assert len(result.candidates) <= 5
    # AEGIS at score 0 → not confident
    assert result.confident is False


# ---------------------------------------------------------------------------
# Test 8: label falls back to path when github_repo empty
# ---------------------------------------------------------------------------


def test_label_fallback_to_path():
    """When github_repo is absent/empty, label must equal path."""
    no_github = {
        "id": "res-internal",
        "title": "Internal Tool",
        "metadata": {"path": "internal-tool"},  # no github_repo key
    }
    alert = {
        "source": "prometheus",
        "service": "internal-tool",
        "title": "crash",
        "description": "",
    }
    result = score_resources(alert, [no_github], resolved_resource_id="res-internal")

    candidate = result.candidates[0]
    assert candidate.label == "internal-tool"
    assert candidate.resource_id == "res-internal"


# ---------------------------------------------------------------------------
# Test: skip resources missing both path and github_repo
# ---------------------------------------------------------------------------


def test_skip_resource_missing_identity():
    """Resources with no path and no github_repo should be silently skipped."""
    no_identity = {
        "id": "res-ghost",
        "title": "Ghost",
        "metadata": {},  # no path, no github_repo
    }
    alert = {"source": "sentry", "service": "ghost", "title": "error", "description": ""}
    result = score_resources(alert, [no_identity, BCP], resolved_resource_id="res-bcp")

    # Ghost should not appear in candidates
    assert "res-ghost" not in _ids(result.candidates)
    assert "res-bcp" in _ids(result.candidates)


# ---------------------------------------------------------------------------
# Test: independent source with service using org/repo format
# ---------------------------------------------------------------------------


def test_service_with_org_prefix_uses_base():
    """service='acme/bcp' → service_base='bcp' → matches BCP path/repo_base."""
    alert = {
        "source": "github",
        "service": "acme/bcp",
        "title": "CI failure",
        "description": "build failed",
    }
    result = score_resources(alert, ALL_REPOS, resolved_resource_id="res-bcp")

    bcp_candidate = next(c for c in result.candidates if c.resource_id == "res-bcp")
    assert bcp_candidate.score >= 1.0
    assert result.confident is True


# ---------------------------------------------------------------------------
# Test: resolved_resource_id not in resources at all → resolved_score=0
# ---------------------------------------------------------------------------


def test_resolved_not_in_resources():
    """If resolved_resource_id does not match any resource, score is 0 and confident=False."""
    alert = {
        "source": "sentry",
        "service": "bcp",
        "title": "crash",
        "description": "",
    }
    result = score_resources(alert, [BCP], resolved_resource_id="res-unknown")

    assert result.confident is False
    # resolved candidate should NOT appear (it wasn't in resources)
    assert "res-unknown" not in _ids(result.candidates)
    # BCP candidate is present
    assert "res-bcp" in _ids(result.candidates)
