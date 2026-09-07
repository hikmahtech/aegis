"""Statement passwords — derive at use time, never store the string (spec
`2026-09-07-statement-reconciliation-design.md` §5.3).

Every scheme either bank uses is the same shape: the first four letters of a
name, uppercase, spaces and periods removed, plus one variable part. Both banks
offer **two** options for that variable part on at least one product, so the
return is an ordered list of candidates, tried in order — a single stored string
breaks the day a bank switches which option it uses, while a derived list simply
falls through to the second.

Pure functions: no I/O, no storage, no logging. The components are what gets
stored (encrypted, `crypto.encrypt_secret`); a derived password never enters a
log, an error message or the digest.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

# Spaces and periods only, exactly as the banks state the rule ("first 4 letters
# of the name, spaces and periods removed"). Deliberately not "strip every
# non-letter": that is a different rule, it would silently change the derived
# password for a name the observed rule has never been tested against, and a
# wrong password is a statement that never opens rather than a visible error.
_NAME_STRIP = re.compile(r"[ .]")
_DIGITS = re.compile(r"\D")

#: The variable part each scheme accepts, in the order to try it.
SCHEMES = ("axis_current", "axis_card", "hdfc")


@dataclass(frozen=True)
class PasswordComponents:
    """What is stored, encrypted, per account. Never a password."""

    name: str = ""
    customer_id: str = ""
    dob: date | None = None
    card_last4: str = ""


def name_prefix(name: str) -> str:
    """`Hikmah Technologies` -> `HIKM`; `A. B. Sharma` -> `ABSH`."""
    return _NAME_STRIP.sub("", name or "").upper()[:4]


def _ddmm(d: date | None) -> str:
    return f"{d.day:02d}{d.month:02d}" if d else ""


def derive_candidates(scheme: str, components: PasswordComponents) -> list[str]:
    """Ordered password candidates for `scheme`, best guess first.

    Empty when the components cannot make one (an unset date of birth, no
    customer id) — the caller reports "the statement could not be opened" and
    names the account, never the attempt. An unknown scheme raises: a typo must
    not read as "this account has no password".
    """
    if scheme not in SCHEMES:
        raise ValueError(f"unknown statement password scheme: {scheme!r}")
    prefix = name_prefix(components.name)
    if not prefix:
        return []
    customer_id = _DIGITS.sub("", components.customer_id or "")
    ddmm = _ddmm(components.dob)
    card_last4 = _DIGITS.sub("", components.card_last4 or "")

    if scheme == "axis_current":
        # The only option Axis offers: 4 + a 9-digit customer id = 13 chars.
        variables = [customer_id]
    elif scheme == "axis_card":
        variables = [ddmm, card_last4]
    else:  # hdfc
        variables = [ddmm, customer_id[:4]]

    out: list[str] = []
    for variable in variables:
        if not variable:
            continue
        candidate = prefix + variable
        if candidate not in out:
            out.append(candidate)
    return out
