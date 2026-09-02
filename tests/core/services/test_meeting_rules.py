"""settings.meeting_rules — who "you" are in a transcript."""

from __future__ import annotations

import pytest
from aegis.services.meeting_rules import is_self, merge, validate


def test_merge_defaults_are_empty_so_a_fork_ships_no_name():
    assert merge(None) == {"self_names": []}
    assert merge({}) == {"self_names": []}


def test_merge_is_lenient_and_strips():
    assert merge({"self_names": [" Sam Doe ", "", 3, "Sam"]}) == {"self_names": ["Sam Doe", "Sam"]}
    assert merge({"self_names": "Sam"}) == {"self_names": []}


def test_merge_tolerates_a_non_dict_row_instead_of_raising():
    """The generic /api/settings editor validates nothing, so a bare string can
    be stored under this key. Reading it must degrade, not 500 the admin GET."""
    assert merge("Sam") == {"self_names": []}
    assert merge(["Sam"]) == {"self_names": []}
    assert merge(7) == {"self_names": []}


def test_validate_rejects_a_non_dict_row():
    with pytest.raises(ValueError):
        validate("Sam")
    with pytest.raises(ValueError):
        validate(["Sam"])


def test_validate_rejects_non_list_and_blank_entries():
    with pytest.raises(ValueError):
        validate({"self_names": "Sam"})
    with pytest.raises(ValueError):
        validate({"self_names": ["Sam", ""]})
    with pytest.raises(ValueError):
        validate({"self_names": [1]})
    assert validate({"self_names": ["Sam Doe"]}) == {"self_names": ["Sam Doe"]}


def test_is_self_matches_case_insensitive_substring():
    assert is_self("Sam Doe", ["sam"])
    assert is_self("SAM DOE", ["Sam Doe"])
    assert not is_self("Samantha Roe", ["Sam Doe"])
    assert not is_self("Sam Doe", [])
    assert not is_self("", ["Sam"])
