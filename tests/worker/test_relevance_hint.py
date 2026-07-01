from aegis_worker.relevance import score_resources

_RESOURCES = [
    {"id": "a", "title": "aegis", "metadata": {"path": "aegis", "github_repo": "example/aegis"}},
    {"id": "b", "title": "news", "metadata": {"path": "acme/news-service",
                                              "github_repo": "acme/news-service"}},
]


def test_empty_hint_preserves_behaviour():
    alert = {"title": "aegis worker crash", "description": ""}
    a = score_resources(alert, _RESOURCES, "a")
    b = score_resources(alert, _RESOURCES, "a", hint="")
    assert a == b


def test_hint_reranks_and_forces_not_confident():
    alert = {"title": "crash", "description": ""}  # weak signal -> low scores
    res = score_resources(alert, _RESOURCES, "b", hint="news service parser")
    assert res.confident is False
    # the hint tokens ("news","service") lift resource b
    ids = [c.resource_id for c in res.candidates]
    assert "b" in ids
