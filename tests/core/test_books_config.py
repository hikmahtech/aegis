"""Books settings, the admin Integrations registry entries and the parse helpers.

The defaults must stay open-source clean: the repo ships nobody's mailbox
names, so the three list-ish knobs default to "" and are DB-configured from
the admin Integrations page.
"""

from types import SimpleNamespace

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
