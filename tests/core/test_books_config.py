"""Books settings, the admin Integrations registry entries and the parse helpers.

The defaults must stay open-source clean: the repo ships nobody's mailbox
names, so the three list-ish knobs default to "" and are DB-configured from
the admin Integrations page.
"""

from pathlib import Path
from types import SimpleNamespace

import yaml
from aegis.services import books
from aegis.services.integrations_config import CONFIG_REGISTRY


def test_settings_defaults_are_open_source_clean(test_settings):
    s = test_settings
    assert s.books_path == "/app/config/books"
    assert s.books_repo_url == "" and s.books_deploy_key == ""
    assert s.books_ignored_mailboxes == "" and s.books_mailbox_entities == ""
    assert s.books_todoist_projects == ""


def test_registry_has_the_books_group():
    by_key = {c.key: c for c in CONFIG_REGISTRY}
    assert by_key["books_repo_url"].group == "Books" and by_key["books_repo_url"].secret is False
    assert by_key["books_deploy_key"].secret is True
    for key in ("books_ignored_mailboxes", "books_mailbox_entities", "books_todoist_projects"):
        assert by_key[key].group == "Books" and by_key[key].secret is False


def test_parse_helpers():
    assert books.parse_csv_set(" a, b ,,c") == frozenset({"a", "b", "c"})
    assert books.parse_csv_set("") == frozenset()
    assert books.parse_kv("personal=6h2f, hikmah = 6h2g") == {"personal": "6h2f", "hikmah": "6h2g"}
    assert books.parse_kv("garbage") == {} and books.parse_kv("") == {}


def test_config_from_settings(tmp_path):
    s = SimpleNamespace(
        books_path=str(tmp_path), books_repo_url="git@x:y.git", gmail_token_dir=str(tmp_path)
    )
    cfg = books.config_from_settings(s)
    assert cfg.path == tmp_path and cfg.repo_url == "git@x:y.git" and cfg.deploy_key is None
    (tmp_path / "books_deploy_key").write_text("k")
    assert books.config_from_settings(s).deploy_key == tmp_path / "books_deploy_key"


def test_compose_gives_core_and_worker_one_books_checkout():
    """`books_path` defaults under `/app/config`, so without a shared mount the
    two containers keep SEPARATE checkouts: the `flock` on `.aegis.lock` no
    longer serialises anything, and a commit whose push failed dies with the
    container while `journal_index` still says `posted`.
    `docs/infrastructure.md` promises one checkout, and the production stack
    mounts `aegis_config` on both — the shipped compose file must match."""
    root = Path(__file__).resolve().parents[2]
    compose = yaml.safe_load((root / "docker-compose.yml").read_text())
    at_config = {}
    for name in ("core", "worker"):
        mounts = [
            v for v in (compose["services"][name].get("volumes") or [])
            if isinstance(v, str) and v.split(":")[1:2] == ["/app/config"]
        ]
        assert len(mounts) == 1, f"{name} mounts nothing at /app/config"
        at_config[name] = mounts[0].split(":")[0]
    assert at_config["core"] == at_config["worker"], "core and worker mount different volumes"
    # A named volume, not a host path: it is declared at the top level.
    assert at_config["core"] in (compose.get("volumes") or {})
