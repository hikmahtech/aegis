"""validate_coding's rules for the inventory block.

The account cross-check matters more than it looks: an account label that is not
a `config_dirs` key would enumerate nothing at all, so a typo would silently
disable the guard rather than fail loudly.
"""

from __future__ import annotations

import pytest
from aegis.services.infra import validate_coding

CLAUDE = {
    "binary_path": "/usr/local/bin/claude",
    "config_dirs": {"personal": "/home/u/.claude-personal", "work": "/home/u/.claude"},
    "default_account": "personal",
}


def _coding(**inventory):
    return {"enabled": True, "engines": {"claude": dict(CLAUDE)}, "inventory": inventory}


def test_inventory_defaults_when_absent():
    out = validate_coding({"enabled": True, "engines": {"claude": dict(CLAUDE)}})
    assert out["inventory"] == {"enabled": False, "skip_when_busy": True, "accounts": []}


def test_inventory_accepts_a_good_block():
    out = validate_coding(_coding(enabled=True, skip_when_busy=False, accounts=["personal"]))
    assert out["inventory"] == {
        "enabled": True,
        "skip_when_busy": False,
        "accounts": ["personal"],
    }


def test_inventory_must_be_an_object():
    with pytest.raises(ValueError, match="inventory"):
        validate_coding({"enabled": True, "inventory": ["nope"]})


@pytest.mark.parametrize("field", ["enabled", "skip_when_busy"])
def test_inventory_flags_must_be_boolean(field):
    with pytest.raises(ValueError, match=field):
        validate_coding(_coding(**{field: "yes"}))


def test_inventory_accounts_must_be_a_list():
    with pytest.raises(ValueError, match="accounts"):
        validate_coding(_coding(accounts="personal"))


def test_inventory_accounts_must_be_strings():
    with pytest.raises(ValueError, match="accounts"):
        validate_coding(_coding(accounts=[1]))


def test_inventory_account_must_exist_in_config_dirs():
    """A typo'd account would otherwise silently enumerate nothing."""
    with pytest.raises(ValueError, match="config_dirs"):
        validate_coding(_coding(accounts=["personl"]))
