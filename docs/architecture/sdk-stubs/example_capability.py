"""Reference stub — a complete example capability (``aegis-cap-renewals``).

Non-running reference / TEMPLATE. See ``docs/architecture/productization.md`` §5, §10.

This single file is the executable spec for the plugin contract: it exercises EVERY
surface a real capability uses — a scheduled flow, an activity, a chat tool with a
permission tier, an event subscription (no import of the emitting plugin), a published
read-model, a UI page, lifecycle hooks, and port/feature declarations. ``aegis new
capability <name>`` scaffolds exactly this shape.

It is a slimmed 'renewals' slice (a real piece of today's money capability) chosen
because it touches the interesting parts: it CONSUMES an event from ``mail`` it must
not import, PUBLISHES a read-model ``digests`` consumes without importing it, and has
an ``approval``-tier mutation.
"""

from __future__ import annotations

# In the real package these import from the installed aegis_sdk; here they are the
# sibling reference stubs.
from capability import Capability, ReadModel, Schedule, Subscription, ToolSpec, UiPage
from events import CHARGE_UPSERTED, EMAIL_CLASSIFIED


# --- flow (Temporal; gets an ActivityContext-backed activity) ----------------------
class RenewalRadarFlow:  # @workflow.defn in the real thing
    async def run(self, cfg: dict) -> dict: ...


async def evaluate_renewals(ctx, cfg: dict) -> dict:  # @activity.defn
    """Reads charges from this capability's own schema; emits CHARGE_UPSERTED."""
    ...


# --- chat tool --------------------------------------------------------------------
async def _exec_snooze_renewal(ctx, args: dict) -> str:
    """Mutating → 'approval' tier → kernel spawns an Interactions card before running."""
    await ctx.ports.tasks.comment(task_id=args["task_id"], text="snoozed")
    return "snoozed"


# --- event handler (reacts to mail WITHOUT importing it) --------------------------
async def _on_email_classified(ctx, payload: dict) -> None:
    if "financial" in payload.get("tags", []):
        await ctx.ports.knowledge.search("renewal", limit=1)  # illustrative


# --- published read-model (digests reads this; never our schema) ------------------
async def _build_upcoming_renewals(ctx) -> list[dict]:
    return await ctx.db.fetch("select * from renewals.upcoming")  # illustrative


# --- lifecycle --------------------------------------------------------------------
async def _health(ctx) -> dict:
    return {"ok": True}


# --- THE MANIFEST: the one place this plugin is registered ------------------------
CAPABILITY = Capability(
    name="renewals",
    version="0.1.0",
    schema="renewals",
    requires_ports=["TaskStore", "KnowledgeStore", "DeliveryChannel", "Interactions"],
    requires_features=["KnowledgeStore:search"],
    flows=[RenewalRadarFlow],
    activities=[evaluate_renewals],
    schedules=[Schedule("renewal-radar-daily", RenewalRadarFlow, "0 9 * * *")],
    tools=[
        ToolSpec(
            name="snooze_renewal",
            schema={"type": "object", "properties": {"task_id": {"type": "string"}},
                    "required": ["task_id"]},
            executor=_exec_snooze_renewal,
            permission="approval",   # reclaimed from the dead personalities.yaml tiers
            scopes=("maou",),
        ),
    ],
    emits=[CHARGE_UPSERTED],
    consumes=[Subscription(EMAIL_CLASSIFIED, _on_email_classified)],
    read_models=[ReadModel("renewals.upcoming", version=1, builder=_build_upcoming_renewals)],
    ui_pages=[UiPage(slug="renewals", title="Renewals", route="/api/renewals/upcoming")],
    migrations_dir="migrations",
    lens="maou",
    voice_md="voice.md",
    health=_health,
    entitlement=None,  # OSS; set "premium.money" to gate behind a license
)
