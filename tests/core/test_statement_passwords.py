"""Statement password derivation (spec §5.3).

Components in, an ORDERED candidate list out. Both banks offer two options on at
least one product, which is why the answer is never a single string: a stored
password breaks the day a bank switches which option it uses, a derived list
falls through to the second.
"""

from datetime import date

import pytest
from aegis.services.statement_passwords import (
    PasswordComponents,
    derive_candidates,
    name_prefix,
)


def test_name_prefix_is_four_letters_uppercase_without_spaces_or_periods():
    assert name_prefix("Hikmah Technologies") == "HIKM"
    assert name_prefix("A. B. Sharma") == "ABSH"
    assert name_prefix("mo ha") == "MOHA"
    assert name_prefix("Ali") == "ALI"  # shorter names are not padded
    assert name_prefix("") == ""


def test_axis_current_offers_exactly_one_candidate_the_customer_id():
    components = PasswordComponents(
        name="Hikmah Technologies", customer_id="900000001", dob=date(1990, 4, 7)
    )
    # 4 + 9 = 13 characters, and the date of birth is NOT an Axis current-account
    # option even though the components carry one.
    assert derive_candidates("axis_current", components) == ["HIKM900000001"]


def test_axis_card_tries_the_birthday_then_the_card_tail():
    components = PasswordComponents(
        name="Specimen Person", dob=date(1990, 4, 7), card_last4="1313"
    )
    assert derive_candidates("axis_card", components) == ["SPEC0704", "SPEC1313"]


def test_hdfc_tries_the_birthday_then_the_customer_id_prefix():
    components = PasswordComponents(
        name="Specimen Person", dob=date(1990, 12, 31), customer_id="12345678"
    )
    assert derive_candidates("hdfc", components) == ["SPEC3112", "SPEC1234"]


def test_a_missing_component_drops_only_its_own_candidate():
    components = PasswordComponents(name="Specimen Person", card_last4="1313")
    assert derive_candidates("axis_card", components) == ["SPEC1313"]


def test_no_name_means_no_candidate_rather_than_a_bare_variable_part():
    components = PasswordComponents(customer_id="900000001", dob=date(1990, 4, 7))
    assert derive_candidates("axis_current", components) == []
    assert derive_candidates("hdfc", components) == []


def test_two_options_that_derive_the_same_string_collapse_to_one():
    # 13 January and a card ending 1301 both give "1301".
    components = PasswordComponents(
        name="Specimen", dob=date(1990, 1, 13), card_last4="1301"
    )
    assert derive_candidates("axis_card", components) == ["SPEC1301"]


def test_non_digits_in_a_stored_customer_id_are_ignored():
    components = PasswordComponents(name="Specimen", customer_id="9000-0000-1")
    assert derive_candidates("axis_current", components) == ["SPEC900000001"]


def test_an_unknown_scheme_raises_rather_than_reading_as_no_password():
    with pytest.raises(ValueError):
        derive_candidates("axis_savings", PasswordComponents(name="Specimen"))
