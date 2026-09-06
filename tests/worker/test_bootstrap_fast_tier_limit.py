"""#414 — the worker's fast-tier concurrency cap must key on the RESOLVED model.

`bootstrap()` builds the `LLMClient` with `concurrency_limits={<model>: 2}` and
says why in its own comment: "Cap the fast tier at 2 concurrent calls — it
typically shares a GPU with everything else aegis hosts". It then keyed that
cap on `settings.model_fast`, the raw `AEGIS_MODEL_FAST` env field, eleven
lines after `set_model_tiers(backend["tiers"])` had already resolved what
`fast` actually is.

When the two disagree — which is the whole failure in #414, a release rendered
from a stale checkout — the semaphore is created for a model name nobody ever
calls, and the model that IS the fast tier runs with no cap at all.

`bootstrap()` needs a live database, deploy keys and every connector, so this
reads the call site itself rather than running it. That is enough to be
falsifiable: put `settings.model_fast` back as the key and this fails.
"""

from __future__ import annotations

import ast
import inspect
import textwrap


def _concurrency_limits_source() -> str:
    """The source of the `concurrency_limits=` argument to `LLMClient(...)`."""
    from aegis_worker import bootstrap as mod

    src = textwrap.dedent(inspect.getsource(mod.bootstrap))
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = node.func.id if isinstance(node.func, ast.Name) else ""
        if name != "LLMClient":
            continue
        for kw in node.keywords:
            if kw.arg == "concurrency_limits":
                return ast.get_source_segment(src, kw.value) or ""
    raise AssertionError("no LLMClient(concurrency_limits=...) call in bootstrap()")


def test_the_fast_cap_is_keyed_on_the_resolved_tier_not_the_env_field():
    source = _concurrency_limits_source()
    assert "tiers" in source, (
        f"the fast cap must key on the resolved tier map, got: {source}"
    )
    assert not source.lstrip("{").startswith("settings.model_fast"), source


def test_the_env_field_is_still_the_fallback():
    """Not a rewrite of the fallback chain — a tier map missing `fast` (boot
    order, or a backend that omits it) must still produce a cap rather than a
    `None` key, so the old value stays as the second choice."""
    assert "settings.model_fast" in _concurrency_limits_source()
