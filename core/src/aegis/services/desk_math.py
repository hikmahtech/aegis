"""The trading desk's arithmetic: pure functions, no I/O.

Spec: docs/superpowers/specs/2026-09-12-maou-trading-desk-design.md (§4-§8).
Everything here takes plain values and returns plain values, so every rule the
spec states is one unit test away. `trading_desk.py` does the reading and writing.

Money is float. `ponytail:` this is a score, not the ledger; Decimal matters
where a live fill is posted to the books, which is the live spec's job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

DEFAULT_TAX_RATE = {"equity": 0.20, "etf": 0.30}
_EPS = 1e-9


@dataclass(frozen=True)
class Rules:
    """The desk's knobs (spec §12). ``from_config`` merges an activities row's
    ``config`` over these defaults, so a missing key is never an error."""

    mode: str = "paper"
    capital: float = 100_000.0
    asset_classes: tuple[str, ...] = ("equity", "etf")
    cost_pct_per_side: float = 0.002
    sell_charge_inr: float = 16.0
    band_abs: float = 0.02
    band_rel: float = 0.25
    max_order_pct: float = 0.25
    tax_rate: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_TAX_RATE))
    ltcg_rate: float = 0.125
    ltcg_exemption_inr: float = 125_000.0
    benchmark: str = "SHARIABEES.NS"
    context_benchmark: str = "^NSEI"
    expected_excess_pa: float = 0.06

    @classmethod
    def from_config(cls, cfg: dict | None) -> Rules:
        cfg = cfg or {}
        base = cls()

        def num(key: str) -> float:
            raw = cfg.get(key)
            return float(raw) if raw is not None else float(getattr(base, key))

        rates = {k: float(v) for k, v in (cfg.get("tax_rate") or {}).items()}
        return cls(
            mode=str(cfg.get("mode") or base.mode),
            capital=num("capital"),
            asset_classes=tuple(cfg.get("asset_classes") or base.asset_classes),
            cost_pct_per_side=num("cost_pct_per_side"),
            sell_charge_inr=num("sell_charge_inr"),
            band_abs=num("band_abs"),
            band_rel=num("band_rel"),
            max_order_pct=num("max_order_pct"),
            tax_rate={**DEFAULT_TAX_RATE, **rates},
            ltcg_rate=num("ltcg_rate"),
            ltcg_exemption_inr=num("ltcg_exemption_inr"),
            benchmark=str(cfg.get("benchmark") or base.benchmark),
            context_benchmark=str(cfg.get("context_benchmark") or base.context_benchmark),
            expected_excess_pa=num("expected_excess_pa"),
        )


@dataclass(frozen=True)
class Decision:
    """One row of the trading system's target portfolio, as the desk copied it."""

    symbol: str
    asset_class: str
    halal_status: str
    direction: str
    target_weight: float
    selection_rank: int
    recovery_state: str = "NORMAL"
    active_kill_conditions: str = ""


@dataclass(frozen=True)
class Check:
    """What the checks before trading decided (spec §5).

    ``outcome`` is ``ok``, ``held_stale`` or ``held_suspect``. ``rows`` is what
    may trade. ``problems`` are plain-English lines for ``desk_decisions_suspect``,
    and can be non-empty on an ``ok`` day: a bad row was dropped and the rest traded.
    """

    outcome: str
    rows: tuple[Decision, ...]
    problems: tuple[str, ...]


def last_trading_day(index_days: list[date], today: date) -> date | None:
    """The latest market day strictly before ``today``, from the index's bars."""
    before = [d for d in index_days if d < today]
    return max(before) if before else None


def check_decisions(rows: list[Decision], held_classes: set[str], rules: Rules) -> Check:
    """Spec §5. An empty day is stale; odd weights or a held class that vanished
    for no stated reason hold the whole day; a non-halal or short row is dropped."""
    if not rows:
        return Check("held_stale", (), ())
    problems: list[str] = []
    kept: list[Decision] = []
    for r in rows:
        if r.halal_status != "COMPLIANT" or r.direction != "LONG":
            problems.append(f"{r.symbol} came as {r.halal_status}/{r.direction}; dropped and never bought.")
        else:
            kept.append(r)
    enabled = [r for r in kept if r.asset_class in rules.asset_classes]
    total = sum(r.target_weight for r in enabled)
    bad = [r for r in enabled if r.target_weight <= 0 or r.target_weight > rules.max_order_pct]
    if total > 1.0 + 1e-6:
        problems.append(f"Target weights add up to {total:.1%}, more than the whole portfolio.")
    for r in bad:
        problems.append(
            f"{r.symbol} has a target weight of {r.target_weight:.1%}, outside 0-{rules.max_order_pct:.0%}."
        )
    if total > 1.0 + 1e-6 or bad:
        return Check("held_suspect", (), tuple(problems))
    explained = any(r.active_kill_conditions or r.recovery_state != "NORMAL" for r in rows)
    present = {r.asset_class for r in kept}
    vanished = sorted(c for c in held_classes if c in rules.asset_classes and c not in present)
    if vanished and not explained:
        problems.append(
            f"The desk holds {', '.join(vanished)} but today's decisions have none, "
            "and no kill switch or recovery state explains it."
        )
        return Check("held_suspect", (), tuple(problems))
    return Check("ok", tuple(enabled), tuple(problems))
