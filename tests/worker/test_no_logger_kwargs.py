"""Guard: no structlog-style kwargs on Temporal's stdlib loggers.

`activity.logger` and `workflow.logger` are `logging.LoggerAdapter`s. Passing
bare keyword args (anything other than `extra=`/`exc_info=`/`stacklevel=`/
`stack_info=`) reaches `Logger._log()` and raises
`TypeError: Logger._log() got an unexpected keyword argument '<k>'` at the
logger's enabled level — which has killed live workflows (e.g. create_github_pr
after kimi pushed a branch). Use f-strings, %-args, or `extra={...}` instead.

This static scan prevents reintroduction anywhere in the worker package.
"""

from __future__ import annotations

import pathlib
import re

# Matches `activity.logger.<level>("plain string", <ident>=...` — a plain-string
# first arg (NOT an f-string, which would carry `key={}` before the close quote)
# followed by a keyword argument. Positional %-args (no `=`) don't match.
_BAD = re.compile(
    r'\b(?:activity|workflow)\.logger\.'
    r'(?:info|warning|warn|error|debug|exception|critical)'
    r'\("[^"]*",\s*([a-zA-Z_]\w*)='
)
_ALLOWED_KWARGS = {"extra", "exc_info", "stacklevel", "stack_info"}

_WORKER_SRC = pathlib.Path(__file__).resolve().parents[1] / "worker" / "src"


def test_no_structlog_kwargs_on_temporal_stdlib_loggers():
    violations: list[str] = []
    for py in _WORKER_SRC.rglob("*.py"):
        for lineno, line in enumerate(py.read_text().splitlines(), start=1):
            m = _BAD.search(line)
            if m and m.group(1) not in _ALLOWED_KWARGS:
                violations.append(f"{py}:{lineno}: {line.strip()}")
    assert not violations, (
        "activity/workflow.logger called with structlog-style kwargs (use f-strings "
        "or extra={...} — stdlib LoggerAdapter rejects bare kwargs):\n  "
        + "\n  ".join(violations)
    )
