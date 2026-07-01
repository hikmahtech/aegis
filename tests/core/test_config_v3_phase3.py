"""Config surface changes for Phase 3."""

from __future__ import annotations

# Minimal set of required fields (after n8n fields are dropped) for Settings() to boot.
_REQUIRED_PHASE3 = {
    "AEGIS_DATABASE_URL": "postgresql://test:test@localhost:5432/test",
    "AEGIS_LITELLM_URL": "https://litellm.test/v1",
    "AEGIS_TEMPORAL_UI_URL": "https://temporal.test",
    "AEGIS_ADMIN_USERNAME": "admin",
    "AEGIS_ADMIN_PASSWORD": "admin",
}


def test_phase3_fields_present(monkeypatch):
    """Phase 3 surface — the aegis_ui_url is the only knob that survived
    the v3 ingest/learning-loop cleanup."""
    for k, v in _REQUIRED_PHASE3.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("AEGIS_UI_URL", "https://aegis.example.com")
    from aegis.config import Settings

    s = Settings()
    assert s.aegis_ui_url == "https://aegis.example.com"


def test_phase3_learner_fields_deleted(monkeypatch):
    """The TriageLearner pipeline + per-email corpus capture were removed
    along with the Prolog ghost layer; the config knobs that fed them
    must no longer exist."""
    for k, v in _REQUIRED_PHASE3.items():
        monkeypatch.setenv(k, v)
    from aegis.config import Settings

    s = Settings()
    for gone in (
        "email_corpus_path",
        "learner_min_samples",
        "learner_confidence_threshold",
        "reasoning_rules_dir",
    ):
        assert not hasattr(s, gone), f"{gone} should have been deleted"


def test_phase3_n8n_fields_deleted(monkeypatch):
    """Phase 3 deletes n8n_url, n8n_ui_url, n8n_api_key, n8n_webhook_secret.

    Phase 5 closeout additionally deletes the homelab_n8n_* fields — the
    stack no longer runs n8n so the Homelab Guardian has nothing to query.
    """
    for k, v in _REQUIRED_PHASE3.items():
        monkeypatch.setenv(k, v)
    from aegis.config import Settings

    s = Settings()
    for gone in (
        "n8n_url",
        "n8n_ui_url",
        "n8n_api_key",
        "n8n_webhook_secret",
        "homelab_n8n_api_url",
        "homelab_n8n_api_key",
    ):
        assert not hasattr(s, gone), f"{gone} should have been deleted"
