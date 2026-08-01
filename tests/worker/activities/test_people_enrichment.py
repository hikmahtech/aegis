"""C2 — passive people enrichment from email + calendar, against a real Postgres.

The privacy invariants are the point of this file: the account owner must never
become a `life.people` row, and every alias written by enrichment must come back
out of `find_people` (aliases are stored lowercased and probed with `@>` — a
write that skips `normalize_aliases` is invisible to C3's chat tool and radar).
"""

from __future__ import annotations

import datetime as dt

import pytest
import pytest_asyncio
from aegis.services.people import (
    create_person,
    find_people,
    is_probably_human,
    name_from_email,
    parse_contact,
)
from aegis_worker.activities.people import PeopleActivities
from temporalio.testing import ActivityEnvironment

PREFIX = "zzc2-"
# Every address these tests write carries PREFIX in its DOMAIN, so the
# prefix-scoped cleanup below matches auto-created rows (whose name is derived
# from the local part) as well as hand-seeded ones.
DOMAIN = f"{PREFIX}test.example.com"
OWNER = f"owner@{DOMAIN}"


@pytest_asyncio.fixture(loop_scope="function")
async def clean_people(db_pool):
    """Prefix-scoped so the shared test database survives xdist co-location."""
    await db_pool.execute(
        "DELETE FROM life.people WHERE name ILIKE $1 OR aliases::text ILIKE $1",
        f"%{PREFIX}%",
    )
    yield db_pool
    await db_pool.execute(
        "DELETE FROM life.people WHERE name ILIKE $1 OR aliases::text ILIKE $1",
        f"%{PREFIX}%",
    )


def _acts(pool, **kw) -> PeopleActivities:
    kw.setdefault("enabled", True)
    kw.setdefault("owner_emails", frozenset({OWNER}))
    return PeopleActivities(db_pool=pool, **kw)


async def _count(pool) -> int:
    return await pool.fetchval(
        "SELECT count(*) FROM life.people WHERE name ILIKE $1 OR aliases::text ILIKE $1",
        f"%{PREFIX}%",
    )


def _msg(sender: str, when: dt.datetime | None = None) -> dict:
    when = when or dt.datetime(2026, 3, 4, 10, 0, tzinfo=dt.UTC)
    return {
        "id": "zzc2-msg-1",
        "sender": sender,
        "internal_date_ms": int(when.timestamp() * 1000),
    }


# ── pure helpers ──


def test_parse_contact_splits_and_lowercases():
    assert parse_contact('"Zainab Ansari" <Zainab.Ansari@Example.COM>') == (
        "zainab.ansari@example.com",
        "Zainab Ansari",
    )
    assert parse_contact("bare@Example.com") == ("bare@example.com", "")
    assert parse_contact("") == ("", "")


@pytest.mark.parametrize(
    "email",
    [
        "noreply@stripe.com",
        "no-reply@github.com",
        "bounces+abc123@sendgrid.net",
        "support@vendor.io",
        "billing@vendor.io",
        "mailer-daemon@googlemail.com",
        "c_188abc@resource.calendar.google.com",
        "notifications@slack.com",
        "not-an-email",
    ],
)
def test_is_probably_human_rejects_machines_and_role_mailboxes(email):
    assert is_probably_human(email) is False


@pytest.mark.parametrize(
    "email",
    ["zainab.ansari@example.com", "sanjeev.info@example.com", "j_doe@sub.example.co.uk"],
)
def test_is_probably_human_accepts_people(email):
    assert is_probably_human(email) is True


def test_name_from_email_titlecases_the_local_part():
    assert name_from_email("john.doe@example.com") == "John Doe"


# ── email lane: enriches, never creates ──


@pytest.mark.asyncio
async def test_email_from_a_known_address_moves_last_contact_forward(clean_people):
    await create_person(
        clean_people,
        {"name": f"{PREFIX}Zainab", "aliases": [f"Zainab.Ansari@{DOMAIN}".upper()]},
    )
    when = dt.datetime(2026, 3, 4, 10, 0, tzinfo=dt.UTC)
    out = await ActivityEnvironment().run(
        _acts(clean_people).enrich_people_from_email,
        _msg(f"Zainab Ansari <zainab.ansari@{DOMAIN}>", when),
    )
    assert out == {"outcome": "updated"}
    rows = await find_people(clean_people, f"{PREFIX}Zainab")
    assert len(rows) == 1
    assert rows[0]["last_contact"] == when


@pytest.mark.asyncio
async def test_email_teaches_an_address_to_a_hand_entered_person_and_find_people_sees_it(
    clean_people,
):
    """The normalize_aliases contract, end to end: a person entered by name only
    gains the sender's address, and C3's lookup (find_people → `aliases @>`)
    resolves it however it is capitalised."""
    await create_person(clean_people, {"name": f"{PREFIX}Sajid Khan"})
    out = await ActivityEnvironment().run(
        _acts(clean_people).enrich_people_from_email,
        _msg(f'"{PREFIX}Sajid Khan" <Sajid.Khan@{DOMAIN.upper()}>'),
    )
    assert out == {"outcome": "updated"}

    for probe in (f"Sajid.Khan@{DOMAIN}".upper(), f"sajid.khan@{DOMAIN}"):
        found = await find_people(clean_people, probe)
        assert [p["name"] for p in found] == [f"{PREFIX}Sajid Khan"], f"lookup {probe!r} missed"


@pytest.mark.asyncio
async def test_enrichment_stores_the_alias_lowercased(clean_people):
    """Read the raw column, not the service's answer: `find_people` lowercases
    its needle, so a mixed-case alias sitting in the table would look fine
    through it and still be invisible to the `aliases @> ARRAY[$1]` probe C3's
    chat tool and the radar ride."""
    await create_person(clean_people, {"name": f"{PREFIX}Case Test"})
    await ActivityEnvironment().run(
        _acts(clean_people).enrich_people_from_email,
        _msg(f'"{PREFIX}Case Test" <MiXeD.CaSe@{DOMAIN.upper()}>'),
    )
    stored = await clean_people.fetchval(
        "SELECT aliases FROM life.people WHERE name = $1", f"{PREFIX}Case Test"
    )
    assert stored == [f"mixed.case@{DOMAIN}"]


@pytest.mark.asyncio
async def test_email_from_an_unknown_sender_creates_nobody(clean_people):
    out = await ActivityEnvironment().run(
        _acts(clean_people).enrich_people_from_email,
        _msg(f"Stranger <stranger@{DOMAIN}>"),
    )
    assert out == {"outcome": "no_match"}
    assert await _count(clean_people) == 0


@pytest.mark.asyncio
async def test_email_never_moves_last_contact_backwards(clean_people):
    newer = dt.datetime(2026, 5, 1, 9, 0, tzinfo=dt.UTC)
    await create_person(
        clean_people,
        {
            "name": f"{PREFIX}Old Mail",
            "aliases": [f"old@{DOMAIN}"],
            "last_contact": newer,
        },
    )
    await ActivityEnvironment().run(
        _acts(clean_people).enrich_people_from_email,
        _msg(f"<old@{DOMAIN}>", dt.datetime(2026, 1, 1, 9, 0, tzinfo=dt.UTC)),
    )
    rows = await find_people(clean_people, f"old@{DOMAIN}")
    assert rows[0]["last_contact"] == newer


@pytest.mark.asyncio
async def test_email_lane_is_inert_while_the_feature_flag_is_off(clean_people):
    await create_person(
        clean_people, {"name": f"{PREFIX}Flag Off", "aliases": [f"flag@{DOMAIN}"]}
    )
    out = await ActivityEnvironment().run(
        _acts(clean_people, enabled=False).enrich_people_from_email,
        _msg(f"<flag@{DOMAIN}>"),
    )
    assert out == {"outcome": "disabled"}
    rows = await find_people(clean_people, f"flag@{DOMAIN}")
    assert rows[0]["last_contact"] is None


# ── calendar lane: may create ──


@pytest.mark.asyncio
async def test_calendar_creates_a_person_for_a_new_attendee(clean_people):
    out = await ActivityEnvironment().run(
        _acts(clean_people).enrich_people_from_events,
        [{"id": "ev-1", "attendees": [OWNER, f"Rafi.Ahmed@{DOMAIN}".upper()]}],
    )
    assert out.get("created") == 1
    assert out.get("skipped_owner") == 1

    found = await find_people(clean_people, f"rafi.ahmed@{DOMAIN}")
    assert len(found) == 1, "alias written by calendar enrichment is not findable"
    assert found[0]["name"] == "Rafi Ahmed"
    assert found[0]["metadata"] == {"source": "calendar_enrichment"}
    # An upcoming meeting is not contact that has already happened.
    assert found[0]["last_contact"] is None


@pytest.mark.asyncio
async def test_calendar_rerun_does_not_duplicate(clean_people):
    events = [{"id": "ev-1", "attendees": [f"rafi@{DOMAIN}"]}]
    acts = _acts(clean_people)
    first = await ActivityEnvironment().run(acts.enrich_people_from_events, events)
    second = await ActivityEnvironment().run(acts.enrich_people_from_events, events)
    assert first.get("created") == 1
    assert second.get("updated") == 1
    assert await _count(clean_people) == 1


@pytest.mark.asyncio
async def test_calendar_never_creates_a_row_for_the_owner(clean_people):
    """owner_emails CONFIGURED: the owner is on their own events, and must be
    dropped even when they are the only attendee."""
    out = await ActivityEnvironment().run(
        _acts(clean_people).enrich_people_from_events,
        [{"id": "ev-1", "attendees": [OWNER, OWNER.upper()]}],
    )
    assert out == {"skipped_owner": 2}
    assert await find_people(clean_people, OWNER) == []
    assert await _count(clean_people) == 0


@pytest.mark.asyncio
async def test_calendar_refuses_entirely_while_owner_emails_is_unset(clean_people):
    """owner_emails UNSET — the live production state. Google lists the owner
    among the attendees, so with nothing to compare against the lane must not
    create anybody at all, owner or otherwise."""
    out = await ActivityEnvironment().run(
        _acts(clean_people, owner_emails=frozenset()).enrich_people_from_events,
        [{"id": "ev-1", "attendees": [OWNER, f"rafi@{DOMAIN}"]}],
    )
    assert out == {"outcome": "owner_emails_unset"}
    assert await _count(clean_people) == 0


@pytest.mark.asyncio
async def test_calendar_skips_mass_invites(clean_people):
    attendees = [f"person{i}@{DOMAIN}" for i in range(9)]
    out = await ActivityEnvironment().run(
        _acts(clean_people, max_event_attendees=8).enrich_people_from_events,
        [{"id": "ev-1", "attendees": attendees}],
    )
    assert out == {"skipped_mass_invite": 1}
    assert await _count(clean_people) == 0


@pytest.mark.asyncio
async def test_calendar_skips_rooms_and_machine_addresses(clean_people):
    out = await ActivityEnvironment().run(
        _acts(clean_people).enrich_people_from_events,
        [
            {
                "id": "ev-1",
                "attendees": [
                    "c_1a2b@resource.calendar.google.com",
                    f"noreply@{DOMAIN}",
                ],
            }
        ],
    )
    assert out == {"skipped_non_human": 2}
    assert await _count(clean_people) == 0


@pytest.mark.asyncio
async def test_calendar_lane_is_inert_while_the_feature_flag_is_off(clean_people):
    out = await ActivityEnvironment().run(
        _acts(clean_people, enabled=False).enrich_people_from_events,
        [{"id": "ev-1", "attendees": [f"rafi@{DOMAIN}"]}],
    )
    assert out == {"outcome": "disabled"}
    assert await _count(clean_people) == 0


# ── wiring ──


def _main_tree():
    import ast
    import inspect

    import aegis_worker.__main__ as worker_main

    return ast.parse(inspect.getsource(worker_main))


def test_people_activities_are_in_the_runtime_activities_list():
    """main() builds its own `activities` list; read it via AST so a mention in
    a comment or a docstring cannot satisfy this."""
    import ast

    attrs: set[str] = set()
    for node in ast.walk(_main_tree()):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "activities"
            and isinstance(node.value, ast.List)
        ):
            attrs |= {e.attr for e in node.value.elts if isinstance(e, ast.Attribute)}

    for name in ("enrich_people_from_email", "enrich_people_from_events"):
        assert name in attrs, f"{name} missing from main()'s activities list"


def test_people_activities_are_constructed_from_settings():
    """Registered but permanently disabled is the silent failure this catches:
    the flag and the owner allowlist must both come off Settings."""
    import ast

    call = next(
        (
            n
            for n in ast.walk(_main_tree())
            if isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id == "PeopleActivities"
        ),
        None,
    )
    assert call is not None, "main() never constructs PeopleActivities"
    kwargs = {k.arg: ast.unparse(k.value) for k in call.keywords}
    assert "people_enrichment_enabled" in kwargs.get("enabled", "")
    assert "owner_emails" in kwargs.get("owner_emails", "")
    assert kwargs.get("db_pool") == "deps.pool"


def _executed_activity_names(module) -> set[str]:
    """Activity names a flow module actually passes to execute_activity —
    parsed, so a comment or docstring naming the activity proves nothing."""
    import ast
    import inspect

    names: set[str] = set()
    for node in ast.walk(ast.parse(inspect.getsource(module))):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "execute_activity"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            names.add(node.args[0].value)
    return names


def test_flows_call_the_enrichment_activities_by_name():
    """The activities are unreachable unless a flow actually asks for them."""
    from aegis_worker.flows import calendar_ingest, gmail_ingest

    assert "enrich_people_from_email" in _executed_activity_names(gmail_ingest)
    assert "enrich_people_from_events" in _executed_activity_names(calendar_ingest)


def test_people_enrichment_is_a_db_backed_feature_flag():
    from aegis.config import Settings
    from aegis.services.integrations_config import CONFIG_REGISTRY

    spec = next((c for c in CONFIG_REGISTRY if c.key == "people_enrichment_enabled"), None)
    assert spec is not None, "people_enrichment_enabled is not admin-settable"
    assert spec.boolean is True
    assert Settings.model_fields["people_enrichment_enabled"].default is False
