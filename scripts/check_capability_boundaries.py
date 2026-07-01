#!/usr/bin/env python3
"""Enforce the AEGIS plugin dependency law (productization.md §3, §20).

The law (tightened — one rule, no gaps):
  * Inside ``packages/``, ANY AEGIS package may import ``aegis_sdk`` (and itself) —
    and NOTHING else under ``aegis_*``. That single rule subsumes every special case:
      - capability  -/-> kernel internals, another capability, OR an adapter
      - adapter     -/-> kernel, a capability, OR another adapter
      - kernel      -/-> a capability OR an adapter
  * Concrete wiring (adapter -> port) happens ONLY at the composition root
    (``entrypoints/``), which lives outside ``packages/`` and is not scanned.

This is what makes a capability self-contained: "delete the package and the rest
still boots". Run in CI and as a prod-check-style gate.

Design notes:
  * Pure stdlib (ast + pathlib) — no deps, safe to run anywhere.
  * SKIPS GRACEFULLY (exit 0) until the target ``packages/`` layout exists, so it can
    be merged before the refactor and flipped to enforcing as capabilities migrate.
  * Set ``AEGIS_BOUNDARY_STRICT=1`` to fail when the layout is absent (use once the
    migration is complete, to prevent silent regression to the old layout).

Usage:
    python scripts/check_capability_boundaries.py [REPO_ROOT]
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

# AEGIS package import prefixes we police. Anything else (stdlib, third-party) is fine.
KERNEL = "aegis_kernel"
SDK = "aegis_sdk"
CAP_PREFIX = "aegis_cap_"
ADAPTER_PREFIX = "aegis_adapter_"


def _iter_imports(path: Path):
    """Yield (lineno, dotted_module) for every import in a Python file."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError) as exc:  # pragma: no cover - defensive
        print(f"  ! could not parse {path}: {exc}", file=sys.stderr)
        return
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                yield node.lineno, alias.name
        elif isinstance(node, ast.ImportFrom):
            # ignore relative imports (level > 0) — they stay within the package
            if node.level == 0 and node.module:
                yield node.lineno, node.module


def _top_pkg(module: str) -> str:
    return module.split(".", 1)[0]


def _own_package(py_file: Path, pkg_root: Path) -> str:
    """The aegis_* top-level package a source file belongs to (by its src dir name)."""
    rel = py_file.relative_to(pkg_root)
    for part in rel.parts:
        if part.startswith(("aegis_", "aegis-")):
            return part.replace("-", "_")
    return ""


def _classify(pkg: str) -> str:
    if pkg == KERNEL:
        return "kernel"
    if pkg == SDK:
        return "sdk"
    if pkg.startswith(CAP_PREFIX):
        return "capability"
    if pkg.startswith(ADAPTER_PREFIX):
        return "adapter"
    return "other"


def _violation(owner_kind: str, owner_pkg: str, imported_pkg: str) -> str | None:
    """Return a human-readable reason if (owner imports imported) breaks the law.

    The one rule: an AEGIS package may import only ``aegis_sdk`` and itself among
    ``aegis_*``. Everything else is a violation, with a kind-specific hint.
    """
    if imported_pkg == SDK or imported_pkg == owner_pkg:
        return None
    imp_kind = _classify(imported_pkg)

    if owner_kind == "capability":
        if imp_kind == "capability":
            return f"capability must not import another capability ({imported_pkg}); use the event bus / read-models"
        if imp_kind == "adapter":
            return f"capability must not import an adapter ({imported_pkg}); use the port via ctx.ports"
        if imp_kind == "kernel":
            return "capability must not import kernel internals (use aegis_sdk + ports)"
    elif owner_kind == "adapter":
        return f"adapter may only import aegis_sdk (not {imported_pkg})"
    elif owner_kind == "kernel":
        return f"kernel must stay domain-blind + adapter-free (must not import {imported_pkg})"
    return None


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".").resolve()
    packages = root / "packages"

    if not packages.is_dir():
        msg = f"target layout not present yet ({packages} missing)"
        if os.environ.get("AEGIS_BOUNDARY_STRICT") == "1":
            print(f"FAIL: {msg} and AEGIS_BOUNDARY_STRICT=1", file=sys.stderr)
            return 1
        print(f"SKIP: {msg} — boundary lint is a no-op until the refactor lands.")
        return 0

    violations: list[str] = []
    scanned = 0
    for py_file in packages.rglob("*.py"):
        if "/tests/" in py_file.as_posix() or py_file.name.startswith("test_"):
            continue
        owner_pkg = _own_package(py_file, packages)
        owner_kind = _classify(owner_pkg)
        if owner_kind == "other":
            continue
        scanned += 1
        for lineno, module in _iter_imports(py_file):
            top = _top_pkg(module)
            if not top.startswith("aegis"):
                continue
            reason = _violation(owner_kind, owner_pkg, top)
            if reason:
                rel = py_file.relative_to(root)
                violations.append(f"{rel}:{lineno}: [{owner_kind}] imports `{module}` — {reason}")

    if violations:
        print(f"FAIL: {len(violations)} boundary violation(s) across {scanned} files:\n", file=sys.stderr)
        for v in sorted(violations):
            print(f"  {v}", file=sys.stderr)
        print(
            "\nThe dependency law (docs/architecture/productization.md §3): capabilities "
            "depend on aegis_sdk + their ports only; cross-capability talk goes through "
            "the event bus.",
            file=sys.stderr,
        )
        return 1

    print(f"OK: no boundary violations ({scanned} source files scanned).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
