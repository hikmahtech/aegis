"""A `think()` call that names no model must have its purpose routed.

`LLMClient.think()` carries a hardcoded `model="gemma4:e2b"` default, which is a
local Ollama model. A call site that passes neither `model=` nor a routed
`purpose` therefore runs on that default no matter what the tier map or the
deployment's env says — silently, because the call still succeeds.

That is not hypothetical: `knowledge_ask` did exactly this in production and
every Knowledge-UI answer ran on the local model at ~23s average while the rest
of the fleet was on a hosted model at ~2s. Nothing failed, so nothing surfaced
it.

The invariant is generic, not one deployment's model list: either the caller
chooses a model, or the routing table does. This walks the real source tree and
the real `config/models.yaml`, so a new unrouted call site fails here rather
than in someone's production.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[2]
_SOURCE_DIRS = (_REPO / "core" / "src", _REPO / "worker" / "src")


def _routed_purposes() -> set[str]:
    data = yaml.safe_load((_REPO / "config" / "models.yaml").read_text()) or {}
    return set((data.get("routes") or {}).get("purposes") or {})


def _think_calls() -> list[tuple[str, int, str | None, bool]]:
    """(file, line, purpose literal or None, whether model= was passed)."""
    found: list[tuple[str, int, str | None, bool]] = []
    for root in _SOURCE_DIRS:
        for path in sorted(root.rglob("*.py")):
            tree = ast.parse(path.read_text(), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if not (isinstance(func, ast.Attribute) and func.attr == "think"):
                    continue
                kwargs = {kw.arg for kw in node.keywords if kw.arg}
                purpose = next(
                    (
                        kw.value.value
                        for kw in node.keywords
                        if kw.arg == "purpose" and isinstance(kw.value, ast.Constant)
                    ),
                    None,
                )
                # A positional second argument is the model.
                has_model = "model" in kwargs or len(node.args) >= 2
                found.append(
                    (str(path.relative_to(_REPO)), node.lineno, purpose, has_model)
                )
    return found


def test_there_are_think_calls_to_check():
    """Guards the walker itself: a broken matcher would make every assertion
    below pass vacuously, which is the failure mode this repo keeps finding."""
    assert len(_think_calls()) >= 10


@pytest.mark.parametrize("routed", [_routed_purposes()])
def test_every_modelless_think_call_has_a_routed_purpose(routed):
    offenders = [
        f"{path}:{line} purpose={purpose!r}"
        for path, line, purpose, has_model in _think_calls()
        if not has_model and purpose not in routed
    ]
    assert not offenders, (
        "these think() calls pass no model= and no routed purpose, so they run "
        "on think()'s hardcoded local default: " + "; ".join(offenders)
    )
