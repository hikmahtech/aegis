"""The books' entities and chart of accounts: read them, check them, save them.

AEGIS is forked and configured for someone else's life, so which sets of books
exist, what they are called, which account-name segment marks each one, and
which category posts to which account are configuration, not code. They live in
the `settings` row keyed `books_chart`, read on every post, so a change needs no
redeploy.

The module owns three things and deliberately nothing else:

* **One reading.** :class:`Chart` answers every question the money lane used to
  answer with a literal — which entity is meant, which account a category
  posts to, which account an unposted thing falls back to, and which set of
  books an account belongs to. No caller re-derives any of it, because two
  implementations of "which books is this?" is how a business expense ends up
  in the personal journal.
* **A lenient read.** A malformed row must never stop money being posted, so
  :func:`merge` drops what it cannot use and falls back to :data:`DEFAULT_CHART`
  rather than raising.
* **A strict write.** The same leniency at the write boundary would let a typo
  save with a 200 and then misfile transactions for months, so
  :func:`validate` refuses anything that would not work and names the field.

The code default names nobody: one entity, the segment-less `personal`, and
the generic expense and income categories. An operator's real chart — a second
entity, a company segment, its own accounts — is the settings row, seeded by
migration 049 from what used to be hardcoded here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

SETTINGS_KEY = "books_chart"

#: An entity id. It is a JSON key, a journal DIRECTORY name (`hikmah/2026.journal`)
#: and part of an account segment, so it is kept to the characters all three
#: can carry without quoting.
_ID_RE = re.compile(r"^[a-z0-9_-]{1,32}$")

#: One account-name segment, and so also the shape of an entity's `segment`.
_SEGMENT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

#: A whole account name: colon-separated lowercase segments.
_ACCOUNT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*(?::[a-z0-9][a-z0-9_-]*)*$")

#: The two sides of a posting, and the two keys of an entity's `unknown` pair.
SIDES = ("in", "out")

#: The tree each unknown account must live in. An unknown-IN in the expense
#: tree would file every unrecognised CREDIT as spending.
_UNKNOWN_TREE = {"in": "income:", "out": "expenses:"}

#: The chart a deployment has before anyone configures one. One entity, no
#: segment (so it owns every expense and income account), and the generic
#: categories — nobody's company, nobody's bank.
DEFAULT_CHART: dict[str, Any] = {
    "default_entity": "personal",
    "income_categories": ["interest", "refund", "salary"],
    "entities": {
        "personal": {
            "label": "Personal",
            "segment": "",
            "unknown": {"in": "income:unknown", "out": "expenses:unknown"},
            "categories": {
                "electricity": "expenses:utilities:electricity",
                "fees": "expenses:fees:bank",
                "food": "expenses:food",
                "groceries": "expenses:groceries",
                "health": "expenses:health",
                "infra": "expenses:saas",
                "insurance": "expenses:insurance",
                "interest": "income:interest",
                "internet": "expenses:utilities:internet",
                "media": "expenses:media",
                "mobile": "expenses:utilities:mobile",
                "people": "expenses:people",
                "refund": "income:refunds",
                "saas": "expenses:saas",
                "salary": "income:salary",
                "shopping": "expenses:shopping",
                "tax": "expenses:tax",
                "transport": "expenses:transport",
            },
        }
    },
}


@dataclass(frozen=True)
class Chart:
    """The chart, with the behaviour that reads it.

    Built by :func:`merge` from the stored row, so every field is already
    normalised — an id is lowercase, a side is `in` or `out`, an entity always
    has both unknown accounts. Nothing here validates; that is :func:`validate`,
    on the way in.
    """

    default_entity: str
    income_categories: frozenset[str]
    #: id → `{label, segment, unknown: {in, out}, categories: {name: account}}`,
    #: in the order the row stores them.
    entities: dict[str, dict[str, Any]]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Chart:
        return cls(
            default_entity=data["default_entity"],
            income_categories=frozenset(data["income_categories"]),
            entities=data["entities"],
        )

    @property
    def ids(self) -> tuple[str, ...]:
        """Every configured entity id, in the order the chart stores them."""
        return tuple(self.entities)

    def resolve(self, entity: str | None) -> str:
        """The entity this name means: itself when configured, else the default.

        An entity nobody configured is not an error. `MoneyEvent.entity` is
        shape-checked and never checked against the chart — the model, the
        worker and the tests all build events with no pool to read one — so an
        unconfigured name arrives here and has to mean something. The default
        set of books is the only honest answer: it is where an account no other
        entity's segment claims already lives.
        """
        name = (entity or "").strip().lower()
        return name if name in self.entities else self.default_entity

    def label(self, entity: str | None) -> str:
        """The entity's human name, for a brief or a form."""
        ent = self.resolve(entity)
        return str(self.entities[ent].get("label") or ent)

    def segment(self, entity: str | None) -> str:
        """The account-name segment that marks an account as this entity's."""
        return str(self.entities[self.resolve(entity)].get("segment") or "")

    def unknown(self, entity: str | None, side: str) -> str:
        """Where a posting goes when nothing says where it belongs.

        This is the review queue, not a filing decision: every "what is still
        unclassified?" surface keys on an account ending `:unknown`.
        """
        return str(self.entities[self.resolve(entity)]["unknown"][side])

    def account_for(self, category: str | None, direction: str | None, entity: str | None) -> str:
        """Counter account for an event (spec §4). Unknown ⇒ the entity's unknown account.

        Three rules, and each one earns its place:

        * The SIDE is the direction when there is one, and otherwise whether
          the category is one of `income_categories` — a receipt that says
          "salary" with no direction is still money coming in.
        * An entity whose categories name no income account has no income side
          to file into, so money in goes to its unknown-IN account rather than
          to an expense account of the same name.
        * A mapped account whose tree DISAGREES with the side is not used. A
          category that means an expense cannot absorb a credit; sending it to
          the unknown account keeps the money visible instead of quietly
          reversing the sign of an account nobody reviews.
        """
        ent = self.resolve(entity)
        categories: dict[str, str] = self.entities[ent].get("categories") or {}
        side = "in" if direction == "in" or (category or "") in self.income_categories else "out"
        if side == "in" and not any(a.startswith("income:") for a in categories.values()):
            return self.unknown(ent, "in")
        mapped = categories.get((category or "").strip().lower())
        if mapped and (mapped.startswith("income:") == (side == "in")):
            return mapped
        return self.unknown(ent, side)

    def entity_of(self, account: str) -> str | None:
        """Which set of books an account belongs to, or None when it belongs to
        both.

        Only the expense and income trees carry an entity — an entity's is the
        one holding its `segment`. Assets, liabilities and equity are
        entity-NEUTRAL by design: `post_event` writes `assets:bank:hdfc:1225`
        into every set of books through `instrument_account`, which has no
        notion of entity at all, and a chart declares `equity:transfers`
        precisely for a move between one's own accounts. Treating those as the
        default entity would refuse a real correction — a business posting
        moved onto the shared bank account — for no gain, since the hazard the
        callers guard against (a segmented posting filed in the default
        entity's journal) lives entirely in the two trees this does cover.

        Four callers rely on it and all four guard the same hazard from a
        different door: `ledger_post` refuses to write an account into another
        set of books, `ledger_reclassify` refuses a cross-entity move,
        `ledger_add_rule` defaults an omitted `entity` and refuses one that
        contradicts the account, and the curiosity answer hook stamps the rule
        it writes with no human in the loop at all. `ledger_post` was the door
        that stood open: its write balanced and passed `check --strict`, and
        `ledger_reclassify` then refused to undo it, so the repair path was
        narrower than the path in.

        The default entity's segment is empty, so it claims every expense and
        income account no other entity's segment does — which is what makes
        this total over those two trees.
        """
        if not account.startswith(("expenses:", "income:")):
            return None
        # Padded on the right so a segment that ENDS the account (`expenses:acme`)
        # matches the same way as one in the middle.
        padded = f"{account}:"
        for ent, spec in self.entities.items():
            segment = str(spec.get("segment") or "")
            if segment and f":{segment}:" in padded:
                return ent
        return self.default_entity

    def as_dict(self) -> dict[str, Any]:
        """The chart as it is stored — what a form edits and a PUT sends back."""
        return {
            "default_entity": self.default_entity,
            "income_categories": sorted(self.income_categories),
            "entities": self.entities,
        }


def _entity(spec: Any) -> dict[str, Any] | None:
    """One entity as :class:`Chart` wants it, or None when it is unusable.

    Lenient: a missing label becomes the id's own name at read time, a missing
    unknown account falls back to the default entity's, and a categories map
    that is not a map is dropped. Only an entity that is not an object at all
    is refused, because there is nothing in it to keep.
    """
    if not isinstance(spec, dict):
        return None
    fallback = DEFAULT_CHART["entities"]["personal"]["unknown"]
    raw_unknown = spec.get("unknown")
    raw_unknown = raw_unknown if isinstance(raw_unknown, dict) else {}
    unknown = {
        side: str(raw_unknown.get(side) or fallback[side]).strip().lower() for side in SIDES
    }
    raw_categories = spec.get("categories")
    categories = {
        str(name).strip().lower(): str(account).strip().lower()
        for name, account in (raw_categories if isinstance(raw_categories, dict) else {}).items()
        if str(name).strip() and str(account).strip()
    }
    return {
        "label": str(spec.get("label") or "").strip(),
        "segment": str(spec.get("segment") or "").strip().lower(),
        "unknown": unknown,
        "categories": categories,
    }


def merge(value: Any) -> dict[str, Any]:
    """A stored (possibly partial or broken) row, read as a whole chart.

    Nothing here raises. The money lane reads this on every post, and a row a
    person hand-edited into nonsense must cost the wrong accounts on a few
    transactions, never the whole lane.
    """
    raw = value if isinstance(value, dict) else {}
    raw_entities = raw.get("entities")
    entities: dict[str, dict[str, Any]] = {}
    for ent_id, spec in (raw_entities if isinstance(raw_entities, dict) else {}).items():
        key = str(ent_id).strip().lower()
        parsed = _entity(spec) if key else None
        if parsed is not None:
            entities[key] = parsed
    if not entities:
        entities = {k: _entity(v) or {} for k, v in DEFAULT_CHART["entities"].items()}
    for key, spec in entities.items():
        spec["label"] = spec["label"] or key.replace("-", " ").replace("_", " ").title()

    default = str(raw.get("default_entity") or "").strip().lower()
    if default not in entities:
        # A row whose default is missing still has to name one, and the
        # segment-less entity is the one that owns whatever no segment claims.
        unsegmented = [k for k, v in entities.items() if not v["segment"]]
        default = unsegmented[0] if unsegmented else next(iter(entities))

    raw_categories = raw.get("income_categories")
    if isinstance(raw_categories, str):
        raw_categories = raw_categories.split(",")
    if not isinstance(raw_categories, list):
        raw_categories = DEFAULT_CHART["income_categories"]
    income = sorted({str(c).strip().lower() for c in raw_categories if str(c).strip()})
    return {
        "default_entity": default,
        "income_categories": income,
        "entities": entities,
    }


def _account(value: Any, what: str) -> str:
    text = str(value or "").strip().lower()
    if not _ACCOUNT_RE.match(text):
        raise ValueError(
            f"{what}: {str(value or '')!r} is not an account name — use lowercase "
            "segments separated by colons, like expenses:utilities:internet"
        )
    return text


def validate(body: dict[str, Any]) -> dict[str, Any]:
    """The chart as it will be stored, or a ValueError naming what is wrong.

    Strict, because every refusal here is a misfiling that does not happen.
    What it will not accept, and why each one is unrecoverable rather than
    merely odd:

    * An entity id outside `[a-z0-9_-]{1,32}` — it names a journal directory.
    * A `default_entity` that is not one of the entities — every unconfigured
      entity resolves to it, so a name nothing defines files those postings
      nowhere.
    * An account name that is not colon-separated lowercase segments — hledger
      would take it as a different account from the declared one.
    * An unknown-IN account outside the `income:` tree, or an unknown-OUT
      outside `expenses:` — the review queue would file credits as spending.
    * Two entities sharing a segment — an account name could not then say
      which set of books it belongs to.
    * A non-default entity with an empty segment — nothing in an account name
      could ever point at it.
    """
    if not isinstance(body, dict):
        raise ValueError("the chart must be an object")
    raw_entities = body.get("entities")
    if not isinstance(raw_entities, dict) or not raw_entities:
        raise ValueError("the chart needs at least one entity")

    ids: list[str] = []
    for ent_id in raw_entities:
        key = str(ent_id).strip().lower()
        if not _ID_RE.match(key):
            raise ValueError(
                f"{str(ent_id)!r} is not an entity id — use up to 32 lowercase "
                "letters, digits, hyphens or underscores"
            )
        if key in ids:
            raise ValueError(f"{key} is named twice")
        ids.append(key)

    default = str(body.get("default_entity") or "").strip().lower()
    if default not in ids:
        raise ValueError(
            f"default_entity {str(body.get('default_entity') or '')!r} is not one of "
            f"the entities ({', '.join(ids)})"
        )

    entities: dict[str, dict[str, Any]] = {}
    by_segment: dict[str, str] = {}
    for ent_id, spec in raw_entities.items():
        key = str(ent_id).strip().lower()
        if not isinstance(spec, dict):
            raise ValueError(
                f"{key}: an entity is an object with a label, a segment, its unknown "
                "accounts and its categories"
            )
        segment = str(spec.get("segment") or "").strip().lower()
        if segment and not _SEGMENT_RE.match(segment):
            raise ValueError(
                f"{key}: {segment!r} is not an account segment — use one lowercase "
                "segment, like the middle of expenses:acme:saas"
            )
        if not segment and key != default:
            raise ValueError(
                f"{key} needs an account segment: without one, no account name could "
                f"say it belongs to {key} rather than to {default}"
            )
        if segment:
            if segment in by_segment:
                raise ValueError(
                    f"{key} and {by_segment[segment]} both claim the segment "
                    f"{segment!r} — an account name could not say which one it belongs to"
                )
            by_segment[segment] = key

        raw_unknown = spec.get("unknown")
        raw_unknown = raw_unknown if isinstance(raw_unknown, dict) else {}
        unknown: dict[str, str] = {}
        for side in SIDES:
            account = _account(raw_unknown.get(side), f"{key}: the unknown-{side.upper()} account")
            tree = _UNKNOWN_TREE[side]
            if not account.startswith(tree):
                raise ValueError(
                    f"{key}: the unknown-{side.upper()} account is {account} — it has to "
                    f"be in the {tree.rstrip(':')} tree, or unexplained money "
                    f"{'in' if side == 'in' else 'out'} would be filed as the opposite"
                )
            unknown[side] = account

        raw_categories = spec.get("categories")
        if raw_categories is not None and not isinstance(raw_categories, dict):
            raise ValueError(f"{key}: categories must be a map of category name to account")
        categories: dict[str, str] = {}
        for name, account in (raw_categories or {}).items():
            category = str(name).strip().lower()
            if not category:
                raise ValueError(f"{key}: a category needs a name")
            if category in categories:
                raise ValueError(f"{key}: the category {category} is named twice")
            categories[category] = _account(account, f"{key}: the account for {category}")

        entities[key] = {
            "label": str(spec.get("label") or "").strip() or key.title(),
            "segment": segment,
            "unknown": unknown,
            "categories": categories,
        }

    raw_income = body.get("income_categories")
    if isinstance(raw_income, str):
        raw_income = raw_income.split(",")
    if raw_income is None:
        raw_income = []
    if not isinstance(raw_income, list):
        raise ValueError("income_categories must be a list of category names")
    income: set[str] = set()
    for name in raw_income:
        category = str(name).strip().lower()
        if not category:
            raise ValueError("income_categories must not contain an empty name")
        income.add(category)

    return {
        "default_entity": default,
        "income_categories": sorted(income),
        "entities": entities,
    }


async def get_chart(pool: Any) -> Chart:
    """The effective chart: the `books_chart` settings row, read leniently."""
    row = await pool.fetchrow("SELECT value FROM settings WHERE key = $1", SETTINGS_KEY)
    return Chart.from_dict(merge(row["value"] if row else None))


async def read(pool: Any) -> dict[str, Any]:
    """What the admin page shows: the chart the money lane reads, and whether
    it is configured or still the code default."""
    row = await pool.fetchrow("SELECT value FROM settings WHERE key = $1", SETTINGS_KEY)
    return {"stored": bool(row and row["value"]), "chart": merge(row["value"] if row else None)}


async def save_chart(pool: Any, body: dict[str, Any]) -> dict[str, Any]:
    """Check the chart and store it. Raises ValueError on anything that would
    not work, having written nothing.

    A REPLACEMENT, not a merge: an entity or a category the form dropped is one
    the operator removed, and merging would make removing anything impossible.
    """
    stored = validate(body)
    await pool.execute(
        "INSERT INTO settings (key, value, updated_at) VALUES ($1, $2, NOW()) "
        "ON CONFLICT (key) DO UPDATE SET value = $2, updated_at = NOW()",
        SETTINGS_KEY,
        stored,
    )
    return await read(pool)
