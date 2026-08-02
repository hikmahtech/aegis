"""`_truncate_result` must never hand the model a partial result it can't detect.

Issue #148. The truncator caps a tool result at 4096 bytes. When the result was
a dict it kept the first N KEYS and shrank strings inside them — it could not
drop list items at all. Two silent failures fell out of that:

1. The payload key isn't in the first N. The metadata keys fit, the pass
   "succeeds", and the model receives a structurally plausible
   `{count, window, _truncated: true}` with the data simply absent.
2. The payload key IS first but its list is large. No pass can shrink a list,
   so every pass overflows and the whole result becomes the
   "Results too large to display" stub.

Both had already cost real damage: on 2026-08-01 `social_timeline` returned 13
of 75 posts and an agent concluded no Bluesky channel existed, while Bluesky
was connected with 5 posts queued. That was worked around per-tool (#147/#185)
by making `posts` the first key and pre-fitting the budget; every other
dict-returning tool was still exposed.

The fix has three parts, tested independently below because either one alone
would mask the other:
  * nested lists are capped, with the dropped count kept INSIDE the list;
  * keys are chosen by `_payload_rank` (list > dict > scalar), stable, so a
    leading scalar can no longer evict the payload;
  * the truncation signal is written after selection, so it cannot be evicted
    by the same pass that produced it.
"""

import json

import pytest
from aegis.services.chat import _SHRINK_PASSES, _smart_subset, _truncate_result

# The production cap (`send_message`'s `tool_result_max_bytes` default). Written
# out rather than imported so a change to the default can't quietly rewrite what
# these tests consider "fits".
CAP = 4096


def _posts(n: int) -> list[dict]:
    """`social_timeline`-shaped rows — the payload that went missing in prod."""
    return [
        {
            "date": f"2026-08-{(i % 28) + 1:02d} 09:30",
            "state": "QUEUE" if i % 3 else "PUBLISHED",
            "platform": ["bluesky", "linkedin", "mastodon", "devto"][i % 4],
            "channel": f"Channel {i % 7}",
            "text": f"Post number {i} about something moderately interesting.",
            "url": f"https://example.test/p/{i}",
        }
        for i in range(n)
    ]


# --- 1. The bug: a payload key that isn't first ---------------------------


def test_payload_survives_when_its_key_is_not_first():
    """THE BUG. Key order must not decide whether the model sees the data."""
    raw = json.dumps(
        {
            "count": 75,
            "window": {"days_back": 14, "days_ahead": 14, "state": None},
            "generated_at": "2026-08-03T04:00:00+00:00",
            # Last of four keys — exactly the shape that returned metadata only.
            "posts": _posts(75),
        }
    )
    assert len(raw.encode()) > CAP, "fixture must be over budget or it proves nothing"

    result = json.loads(_truncate_result(raw, max_bytes=CAP))

    assert "posts" in result, f"payload dropped; model got only {list(result)}"
    assert result["posts"], "payload key present but empty — same failure, quieter"
    # And it is the real rows, not a placeholder.
    assert result["posts"][0]["url"] == "https://example.test/p/0"


def test_payload_survives_when_it_is_first_but_huge():
    """The other half of #148: a leading payload no pass could shrink used to
    take the entire result down to the 'too large to display' stub."""
    raw = json.dumps({"posts": _posts(200), "count": 200})
    assert len(raw.encode()) > CAP

    result = json.loads(_truncate_result(raw, max_bytes=CAP))

    assert result.get("note") != "Results too large to display", "fell through to the stub"
    assert result.get("posts"), f"payload lost; model got {list(result)}"
    assert result["posts"][0]["url"] == "https://example.test/p/0"


# --- 2. `_payload_rank`, proven on its own --------------------------------


def test_list_payload_outranks_metadata_when_only_one_key_fits():
    """Isolates the ranking change. At n_items=1 the positional rule kept
    `count`; the payload must win instead. List trimming alone cannot produce
    this outcome, so this test fails if only the trimming half is present."""
    data = {
        "count": 3,
        "window": {"days_back": 14},
        "posts": _posts(3),
    }

    subset = _smart_subset(data, n_items=1, max_str_len=1000)

    surviving = [k for k in subset if not k.startswith("_")]
    assert surviving == ["posts"], f"expected the list payload to win, kept {surviving}"


def test_key_order_still_breaks_ties_within_a_rank():
    """`social_timeline` and `list_social_channels` deliberately lead with their
    payload and document that the leading key survives. The sort is stable, so
    that guarantee must hold between two equally-ranked list keys."""
    data = {
        "posts": _posts(3),
        "also_posts": _posts(3),
        "count": 3,
    }

    subset = _smart_subset(data, n_items=1, max_str_len=1000)

    surviving = [k for k in subset if not k.startswith("_")]
    assert surviving == ["posts"], f"leading list key lost its precedence, kept {surviving}"


def test_dict_payload_outranks_scalars():
    """`channels_in_window` is a dict, not a list, and is still the answer —
    it must outrank the scalar metadata it sits beside."""
    data = {"count": 2, "truncated": False, "channels_in_window": {"bluesky": {"posts": 5}}}

    subset = _smart_subset(data, n_items=1, max_str_len=1000)

    surviving = [k for k in subset if not k.startswith("_")]
    assert surviving == ["channels_in_window"], f"kept {surviving}"


# --- 3. List trimming, proven on its own ----------------------------------


def test_nested_list_carries_its_own_dropped_count():
    """The count of dropped items lives INSIDE the list it describes, so the
    same mechanism that removed the items cannot separate them from the
    evidence that they existed."""
    subset = _smart_subset({"posts": _posts(75)}, n_items=3, max_str_len=250)

    posts = subset["posts"]
    assert len(posts) == 4, f"expected 3 rows plus one marker, got {len(posts)}"
    assert posts[-1] == "… +72 of 75 more", f"marker missing or wrong: {posts[-1]!r}"


def test_top_level_list_is_not_given_a_second_marker():
    """A top-level list already reports its count in `total`; adding an in-band
    marker there would put a bare string into a list of records."""
    result = json.loads(_truncate_result(json.dumps(_posts(75)), max_bytes=CAP))

    assert result["total"] == 75
    assert all(isinstance(item, dict) for item in result["results"]), result["results"]


# --- 4. The signal cannot be truncated away -------------------------------

# Spelled out rather than read from `_SHRINK_PASSES` so this pins the passes the
# invariant was actually checked against; `test_shrink_passes_have_not_drifted`
# fails if the constant moves out from under it.
_PASSES = [(5, 1000), (5, 500), (3, 500), (3, 250), (1, 500), (1, 150)]


def test_shrink_passes_have_not_drifted():
    assert list(_SHRINK_PASSES) == _PASSES


@pytest.mark.parametrize(("n_items", "max_str_len"), _PASSES)
def test_signal_survives_every_shrink_pass(n_items, max_str_len):
    """At every level — including the tightest, where a single key survives —
    the result still says it is partial and how much is missing."""
    data = {
        "count": 75,
        "window": {"days_back": 14},
        "generated_at": "2026-08-03T04:00:00+00:00",
        "posts": _posts(75),
    }

    subset = _smart_subset(data, n_items=n_items, max_str_len=max_str_len)

    assert subset["_truncated"] is True
    assert subset["_total_keys"] == 4
    dropped = subset.get("_dropped_keys", [])
    kept = [k for k in subset if not k.startswith("_")]
    assert len(kept) + len(dropped) == 4, f"kept {kept} + dropped {dropped} != 4 keys"
    # Unconditional on purpose: the payload outranks all three metadata keys, so
    # it survives every pass down to the tightest. Guarding this on
    # `if "posts" in kept` would let a positional regression slip through.
    assert "posts" in kept, f"payload evicted at pass ({n_items}, {max_str_len}); kept {kept}"
    assert subset["posts"][-1] == f"… +{75 - n_items} of 75 more"


def test_dropped_keys_are_named_so_the_model_can_narrow_its_query():
    """Blind-retrying the same call is the observed failure mode (#148 saw six
    identical calls). Naming the casualties is what makes a narrower ask
    possible."""
    data = {"count": 75, "window": {"d": 14}, "generated_at": "x", "posts": _posts(75)}

    subset = _smart_subset(data, n_items=1, max_str_len=150)

    assert subset["_dropped_keys"] == ["count", "window", "generated_at"]


def test_signal_stays_bounded_and_cannot_blow_the_budget():
    """`_dropped_keys` is capped like the `+K more` aggregates in
    `list_social_channels`, so naming what was dropped can never be the thing
    that pushes a pass over budget and forces the minimal stub."""
    raw = json.dumps({f"metric_{i:03d}_with_a_long_name": f"value {i}" for i in range(300)})

    encoded = _truncate_result(raw, max_bytes=CAP)
    result = json.loads(encoded)

    assert len(encoded.encode()) <= CAP
    assert result["_truncated"] is True
    assert result["_total_keys"] == 300
    assert len(result["_dropped_keys"]) == 13, result["_dropped_keys"]
    assert result["_dropped_keys"][-1] == "… +283 more"


# --- 5. A realistic worst case still fits ---------------------------------


def _status_digest(rows: int, error_repeat: int, reason_len: int, stack_repeat: int) -> str:
    """`system_status` — the tool #148 named as the next victim: a scalar leads,
    every payload is a list, and unicode inflates `json.dumps` to six bytes a
    character (twelve for an emoji surrogate pair)."""
    return json.dumps(
        {
            "window_hours": 168,
            "runs_by_type_status": [
                {"workflow_type": f"हिकमह Flow {i:03d}", "status": "completed", "count": i}
                for i in range(400)
            ],
            "failed_runs": [
                {
                    "workflow_type": f"Flow {i}",
                    "error": "ApplicationError: something_failed at step=X " * error_repeat,
                    "completed_at": "2026-08-03T04:00:00+00:00",
                }
                for i in range(rows)
            ],
            "completed_but_failed": [
                {"workflow_type": "F", "status": "error", "reason": "r" * reason_len}
                for _ in range(rows)
            ],
            "llm_calls": 9001,
            "llm_tokens": 12345678,
            "pending_interactions": 4,
            "infra_stuck": [f"स्टैक_{i}_सेवा 🚀 " * stack_repeat for i in range(40)],
            "infra_confirmed": [f"stack_{i}_service " * stack_repeat for i in range(40)],
        }
    )


@pytest.mark.parametrize(
    ("label", "shape"),
    [
        # Settles on the loosest pass.
        ("busy_week", (10, 4, 200, 1)),
        # 126KB — long errors and long stack names push it to the third pass, so
        # the byte gate in `_truncate_result` is genuinely exercised, not assumed.
        ("pathological", (30, 20, 900, 8)),
    ],
)
def test_realistic_worst_case_stays_under_the_cap(label, shape):
    raw = _status_digest(*shape)
    assert len(raw.encode()) > 30_000, f"{label}: fixture is not a worst case"

    encoded = _truncate_result(raw, max_bytes=CAP)
    result = json.loads(encoded)

    assert len(encoded.encode()) <= CAP, f"{label}: {len(encoded.encode())} bytes over {CAP}"
    # Not the stub, and the leading scalar did not evict the lists.
    assert result.get("note") != "Results too large to display", label
    assert result["runs_by_type_status"][0]["workflow_type"] == "हिकमह Flow 000", label
    assert result["failed_runs"], f"{label}: a payload list was dropped; kept {list(result)}"
    assert result["_truncated"] is True, label
