from aegis_worker.activework.guard import assess


def _empty():
    return {"prs": [], "pushes": [], "tasks": []}


def test_no_signals_is_inactive():
    aw = assess(repo="example/aegis", service="aegis", **_empty())
    assert aw == {"active": False, "reasons": []}


def test_open_pr_fires():
    aw = assess(repo="example/aegis", service="aegis",
                prs=[{"number": 123, "user": "alice"}], pushes=[], tasks=[])
    assert aw["active"] is True
    assert aw["reasons"] == ["open PR #123 (alice)"]


def test_recent_push_fires_and_strips_refs_heads():
    aw = assess(repo="example/aegis", service="aegis", prs=[],
                pushes=[{"ref": "refs/heads/feature/parser", "actor": "bob"}], tasks=[])
    assert aw["active"] is True
    assert aw["reasons"] == ["recent push to feature/parser (bob)"]


def test_todoist_matches_repo_basename_only():
    tasks = [
        {"content": "fix aegis parser", "labels": ["@pandora"], "due_date": "2026-06-20"},
        {"content": "buy milk", "labels": [], "due_date": "2026-06-20"},
    ]
    aw = assess(repo="example/aegis", service="", prs=[], pushes=[], tasks=tasks)
    assert aw["active"] is True
    assert aw["reasons"] == ['Todoist "fix aegis parser" due 2026-06-20']


def test_todoist_matches_service_in_label():
    tasks = [{"content": "investigate", "labels": ["project/news-service"], "due_date": "2026-06-20"}]
    aw = assess(repo="acme/x", service="news-service", prs=[], pushes=[], tasks=tasks)
    assert aw["active"] is True
