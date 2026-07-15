"""Manual lead logging for Lead Factory week 1 (P0).

P0 is deliberately by hand: ads run, a human answers every lead on WhatsApp,
and this script keeps the diary so attribution and response-time metrics are
real from day one. The P1 automation replaces the human, not the tables.

Usage (DSN via AEGIS_DATABASE_URL, defaults to the dev compose Postgres):

    python scripts/leadfactory_log.py add-client --slug manasrealty \\
        --name "Manas Realty" --domain manasrealty.com
    python scripts/leadfactory_log.py add-project --client manasrealty \\
        --slug malhar-24-east --name "Malhar 24 East" \\
        --rera P52000012345 --locality Kharghar --configs 1BHK,2BHK \\
        --price-min 50 --price-max 60
    python scripts/leadfactory_log.py add-lead --client manasrealty \\
        --phone +919812345678 --name Rakesh --project malhar-24-east \\
        --source meta_leadform --consent
    python scripts/leadfactory_log.py msg --phone +919812345678 \\
        --direction out --template T1 --body "Hi Rakesh! ..."
    python scripts/leadfactory_log.py set-state --phone +919812345678 \\
        --state QUALIFIED --note "50-60L, 2BHK, ready" --next-hours 48
    python scripts/leadfactory_log.py note --phone +919812345678 \\
        --text "wants top floor"
    python scripts/leadfactory_log.py due
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os

import asyncpg

DSN = os.getenv("AEGIS_DATABASE_URL", "postgresql://aegis:aegis_dev@localhost:25432/aegis")
ACTOR = os.getenv("LEADFACTORY_ACTOR", "arshad")


async def _lead_by_phone(conn: asyncpg.Connection, phone: str) -> asyncpg.Record:
    rows = await conn.fetch(
        "SELECT l.*, c.slug AS client_slug FROM leadfactory.leads l "
        "JOIN leadfactory.clients c ON c.id = l.client_id WHERE l.phone = $1",
        phone,
    )
    if not rows:
        raise SystemExit(f"no lead with phone {phone}")
    if len(rows) > 1:
        # ponytail: phone-only lookup, fine while there is one active client
        raise SystemExit(f"{phone} exists under multiple clients — disambiguate in SQL")
    return rows[0]


async def _event(conn, lead_id: int, event: str, detail: dict | None = None) -> None:
    await conn.execute(
        "INSERT INTO leadfactory.lead_events (lead_id, actor, event, detail) "
        "VALUES ($1, $2, $3, $4)",
        lead_id, ACTOR, event, json.dumps(detail) if detail else None,
    )


async def add_client(conn, args) -> None:
    client_id = await conn.fetchval(
        "INSERT INTO leadfactory.clients (slug, name, domain) VALUES ($1, $2, $3) "
        "RETURNING id",
        args.slug, args.name, args.domain,
    )
    print(f"client {args.slug} created (id {client_id})")


async def add_project(conn, args) -> None:
    project_id = await conn.fetchval(
        """INSERT INTO leadfactory.projects
             (client_id, slug, name, rera_no, locality, configs,
              price_min_lakh, price_max_lakh)
           SELECT id, $2, $3, $4, $5, string_to_array($6, ','), $7, $8
           FROM leadfactory.clients WHERE slug = $1
           RETURNING id""",
        args.client, args.slug, args.name, args.rera, args.locality,
        args.configs or "", args.price_min, args.price_max,
    )
    if project_id is None:
        raise SystemExit(f"no client {args.client}")
    print(f"project {args.slug} created (id {project_id})")


async def add_lead(conn, args) -> None:
    lead_id = await conn.fetchval(
        """INSERT INTO leadfactory.leads
             (client_id, project_id, phone, name, source, utm_campaign,
              meta_campaign_id, meta_adset_id, meta_ad_id,
              budget_min_lakh, budget_max_lakh, timeline, preferred_locality,
              next_action, next_action_at)
           SELECT c.id, p.id, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13,
                  'reply_T1', now()
           FROM leadfactory.clients c
           LEFT JOIN leadfactory.projects p ON p.client_id = c.id AND p.slug = $2
           WHERE c.slug = $1
           RETURNING id""",
        args.client, args.project, args.phone, args.name, args.source,
        args.utm_campaign, args.campaign_id, args.adset_id, args.ad_id,
        args.budget_min, args.budget_max, args.timeline, args.locality,
    )
    if lead_id is None:
        raise SystemExit(f"no client {args.client}")
    await _event(conn, lead_id, "created", {"source": args.source})
    if args.consent:
        await _event(conn, lead_id, "consent", {"via": args.source})
    print(f"lead #{lead_id} created ({args.phone}, {args.source}) — 60s clock running")


async def msg(conn, args) -> None:
    lead = await _lead_by_phone(conn, args.phone)
    await conn.execute(
        "INSERT INTO leadfactory.messages (lead_id, direction, template, body) "
        "VALUES ($1, $2, $3, $4)",
        lead["id"], args.direction, args.template, args.body,
    )
    print(f"logged {args.direction} message for lead #{lead['id']}")


async def set_state(conn, args) -> None:
    lead = await _lead_by_phone(conn, args.phone)
    next_at = ("NULL" if args.next_hours is None
               else f"now() + interval '{float(args.next_hours)} hours'")
    await conn.execute(
        f"""UPDATE leadfactory.leads
            SET state = $2, next_action_at = {next_at},
                next_action = $3, updated_at = now()
            WHERE id = $1""",
        lead["id"], args.state, args.next,
    )
    await _event(conn, lead["id"], "state_change",
                 {"from": lead["state"], "to": args.state})
    if args.note:
        await _event(conn, lead["id"], "note", {"text": args.note})
    print(f"lead #{lead['id']}: {lead['state']} -> {args.state}")


async def note(conn, args) -> None:
    lead = await _lead_by_phone(conn, args.phone)
    await _event(conn, lead["id"], "note", {"text": args.text})
    print(f"noted on lead #{lead['id']}")


async def due(conn, _args) -> None:
    rows = await conn.fetch(
        """SELECT l.id, l.phone, l.name, l.state, l.next_action, l.next_action_at
           FROM leadfactory.leads l
           WHERE l.next_action_at <= now()
           ORDER BY l.next_action_at"""
    )
    for r in rows:
        print(f"#{r['id']} {r['phone']} {r['name'] or '-'} [{r['state']}] "
              f"{r['next_action'] or '-'} due {r['next_action_at']:%d %b %H:%M}")
    print(f"-- {len(rows)} due --")
    counts = await conn.fetch(
        "SELECT state, count(*) FROM leadfactory.leads GROUP BY state ORDER BY 2 DESC"
    )
    print(" | ".join(f"{r['state']}:{r['count']}" for r in counts))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("add-client")
    p.add_argument("--slug", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--domain")
    p.set_defaults(fn=add_client)

    p = sub.add_parser("add-project")
    p.add_argument("--client", required=True)
    p.add_argument("--slug", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--rera", required=True)
    p.add_argument("--locality", required=True)
    p.add_argument("--configs", help="comma-separated, e.g. 1BHK,2BHK")
    p.add_argument("--price-min", type=int, dest="price_min")
    p.add_argument("--price-max", type=int, dest="price_max")
    p.set_defaults(fn=add_project)

    p = sub.add_parser("add-lead")
    p.add_argument("--client", required=True)
    p.add_argument("--phone", required=True, help="E.164, e.g. +919812345678")
    p.add_argument("--name")
    p.add_argument("--project", help="project slug")
    p.add_argument("--source", default="manual",
                   choices=["meta_leadform", "ctwa", "gbp", "organic", "referral", "manual"])
    p.add_argument("--utm-campaign", dest="utm_campaign")
    p.add_argument("--campaign-id", dest="campaign_id")
    p.add_argument("--adset-id", dest="adset_id")
    p.add_argument("--ad-id", dest="ad_id")
    p.add_argument("--budget-min", type=int, dest="budget_min")
    p.add_argument("--budget-max", type=int, dest="budget_max")
    p.add_argument("--timeline", choices=["ready", "3m", "6m", "12m_plus"])
    p.add_argument("--locality")
    p.add_argument("--consent", action="store_true",
                   help="form carried the WhatsApp-consent line")
    p.set_defaults(fn=add_lead)

    p = sub.add_parser("msg")
    p.add_argument("--phone", required=True)
    p.add_argument("--direction", required=True, choices=["in", "out"])
    p.add_argument("--body", required=True)
    p.add_argument("--template", help="T1/N1/R1/… when applicable")
    p.set_defaults(fn=msg)

    p = sub.add_parser("set-state")
    p.add_argument("--phone", required=True)
    p.add_argument("--state", required=True,
                   choices=["NEW", "QUALIFYING", "QUALIFIED", "VISIT_BOOKED", "VISITED",
                            "NEGOTIATING", "LONG_TAIL", "DORMANT", "DISQUALIFIED",
                            "CLOSED_WON", "CLOSED_LOST"])
    p.add_argument("--note")
    p.add_argument("--next", help="next_action label, e.g. send_nudge_N1")
    p.add_argument("--next-hours", type=float, dest="next_hours",
                   help="set next_action_at = now() + N hours (else alarm cleared)")
    p.set_defaults(fn=set_state)

    p = sub.add_parser("note")
    p.add_argument("--phone", required=True)
    p.add_argument("--text", required=True)
    p.set_defaults(fn=note)

    p = sub.add_parser("due", help="list due leads + state counts")
    p.set_defaults(fn=due)

    return parser


async def main() -> None:
    args = build_parser().parse_args()
    conn = await asyncpg.connect(DSN)
    try:
        await args.fn(conn, args)
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
