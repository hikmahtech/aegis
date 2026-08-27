"""Mount-token mint/verify (issue #288).

The binding tests are the point of the issue: a run that reads its own token
must not be able to reach another agent's tool surface, or downgrade a gated
run to the ungated endpoint.
"""

from __future__ import annotations

from aegis.services.mcp_tokens import (
    DEFAULT_TTL_SECONDS,
    mint_mount_token,
    verify_mount_token,
)

SECRET = "test-secret-key"
OTHER_SECRET = "a-different-secret"


def test_round_trip():
    token = mint_mount_token("sebas", SECRET)
    assert verify_mount_token(token, "sebas", SECRET) is True


def test_token_is_bound_to_its_agent():
    """The path-segment attack from #288: sebas's token must not drive pandora."""
    token = mint_mount_token("sebas", SECRET)
    assert verify_mount_token(token, "pandora", SECRET) is False


def test_token_is_bound_to_its_gated_mode():
    """A gated run must not present its token at the ungated endpoint."""
    gated = mint_mount_token("sebas", SECRET, gated=True)
    assert verify_mount_token(gated, "sebas", SECRET, gated=True) is True
    assert verify_mount_token(gated, "sebas", SECRET, gated=False) is False

    ungated = mint_mount_token("sebas", SECRET, gated=False)
    assert verify_mount_token(ungated, "sebas", SECRET, gated=True) is False


def test_token_expires():
    token = mint_mount_token("sebas", SECRET, ttl_seconds=100, now=1_000_000)
    assert verify_mount_token(token, "sebas", SECRET, now=1_000_050) is True
    assert verify_mount_token(token, "sebas", SECRET, now=1_000_101) is False


def test_signature_is_checked():
    """A payload edited to extend expiry or change agent must fail."""
    token = mint_mount_token("sebas", SECRET)
    encoded, _, signature = token.partition(".")
    forged = mint_mount_token("pandora", SECRET)
    forged_encoded = forged.partition(".")[0]
    # Splice pandora's payload onto sebas's signature.
    assert verify_mount_token(f"{forged_encoded}.{signature}", "pandora", SECRET) is False
    # And sebas's payload onto a signature from a different secret.
    wrong_sig = mint_mount_token("sebas", OTHER_SECRET).partition(".")[2]
    assert verify_mount_token(f"{encoded}.{wrong_sig}", "sebas", SECRET) is False


def test_a_different_secret_never_verifies():
    token = mint_mount_token("sebas", OTHER_SECRET)
    assert verify_mount_token(token, "sebas", SECRET) is False


def test_no_secret_or_agent_mints_nothing():
    """An empty token is a visible mount failure, never a silent downgrade."""
    assert mint_mount_token("sebas", "") == ""
    assert mint_mount_token("", SECRET) == ""


def test_garbage_is_rejected_without_raising():
    for bad in ["", ".", "junk", "a.b", "!!!.@@@", "x." * 50]:
        assert verify_mount_token(bad, "sebas", SECRET) is False


def test_empty_token_never_verifies():
    assert verify_mount_token("", "sebas", SECRET) is False


def test_ttl_has_a_floor():
    """A zero or negative TTL must not mint an already-dead token."""
    token = mint_mount_token("sebas", SECRET, ttl_seconds=0, now=1_000_000)
    assert verify_mount_token(token, "sebas", SECRET, now=1_000_001) is True


def test_default_ttl_outlives_the_longest_run():
    """Runs cap at 240 minutes; the default must comfortably exceed that."""
    assert DEFAULT_TTL_SECONDS > 240 * 60
