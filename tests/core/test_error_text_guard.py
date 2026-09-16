"""Guard: a caught exception in core is reported with `error_text`, not `str(exc)`.

httpx raises its timeouts with no message, so `str(exc)` is "" and a log line
read `error=` with nothing after it (#573, #575). `aegis.errors.error_text`
names the exception's type as well, so it is never empty. This scan stops the
old forms coming back: `str(exc)[:N]` / `str(e)[:N]` anywhere, and an
`error=str(exc)` keyword argument.

A site whose text is a contract keeps `str(exc)` and is listed in `_ALLOWED`
with the reason. The test also fails on an entry that no longer matches, so the
list cannot go stale.
"""

from __future__ import annotations

import pathlib
import re

_SRC = pathlib.Path(__file__).resolve().parents[2] / "core" / "src"

_BAD = re.compile(r"\bstr\((?:exc|e)\)\[:|\berror=str\((?:exc|e)\)")

# (path under core/src, text on the line) -> why the site keeps str(exc).
# Empty today: every core site reports through `error_text`.
_ALLOWED: dict[tuple[str, str], str] = {}


def test_no_bare_str_exc_error_reporting_in_core():
    violations: list[str] = []
    used: set[tuple[str, str]] = set()
    for py in sorted(_SRC.rglob("*.py")):
        rel = py.relative_to(_SRC).as_posix()
        for lineno, line in enumerate(py.read_text().splitlines(), start=1):
            if not _BAD.search(line):
                continue
            key = next((k for k in _ALLOWED if k[0] == rel and k[1] in line), None)
            if key:
                used.add(key)
                continue
            violations.append(f"core/src/{rel}:{lineno}: {line.strip()}")
    assert not violations, (
        "report a caught exception with aegis.errors.error_text(exc, limit), not "
        "str(exc) — an httpx timeout has an empty message (#575). If the text is "
        "a contract, add the site to _ALLOWED with the reason:\n  " + "\n  ".join(violations)
    )
    stale = set(_ALLOWED) - used
    assert not stale, f"_ALLOWED entries that no longer match any line: {sorted(stale)}"
