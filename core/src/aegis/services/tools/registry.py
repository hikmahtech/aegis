"""Declarative chat-tool registry.

`@aegis_tool` turns a typed executor function into a registered chat tool: the
JSON Schema the LLM sees is generated from the function's type annotations and
docstring, and the (schema, executor) pair lands in `TOOL_REGISTRY` at import
time. `services/chat.py` then builds `CHAT_TOOLS` and `TOOL_EXECUTORS` from the
registry — there is no hand-written schema or dispatch entry to forget.

An executor looks like::

    @aegis_tool
    async def _exec_get_quote(pool, ctx, *, symbols: list[str]) -> str:
        \"\"\"Get current stock quotes.

        Args:
            symbols: Ticker symbols, e.g. ["AAPL", "^NSEI", "BTC-USD"]. Max 10.
        \"\"\"

Convention: the first two parameters are always `pool` and `ctx`; every
parameter after them is a tool argument. Supported annotations:

- `str` / `int` / `float` / `bool`        -> string / integer / number / boolean
- `list[str]`                             -> {"type": "array", "items": {...}}
- `dict` / `dict[str, Any]`               -> {"type": "object"}
- `Literal[...]`                          -> {"type": ..., "enum": [...]}
- `X | Y`                                 -> {"type": [<t(X)>, <t(Y)>]}
- `X | None = None`                       -> optional parameter (schema of X)
- `Annotated[int, Field(ge=.., le=..)]`   -> minimum / maximum

A signature default becomes the schema's `"default"`; parameters without a
default are listed in `"required"` (the key is omitted when empty, unless
`empty_required=True` preserves a legacy explicit `[]`). The tool
description is the docstring's first paragraph; per-argument descriptions come
from a Google-style `Args:` section — both must reproduce the advertised text
verbatim, because the schema is the LLM's contract.

The decorated object keeps the historic calling convention
`async (pool, args: dict, ctx) -> str` (the wrapper unpacks `args` into the
typed parameters), so `TOOL_EXECUTORS` entries, direct test calls,
`__qualname__` snapshots and `inspect.getsource` (which unwraps `__wrapped__`)
all behave exactly as they did for the hand-written executors.
"""

from __future__ import annotations

import functools
import inspect
import re
import types
import typing
from dataclasses import dataclass
from typing import Annotated, Any, Literal, get_args, get_origin


@dataclass
class AegisTool:
    """One registered chat tool: the advertised schema plus its executor."""

    name: str
    description: str
    parameters: dict
    executor: Any  # async (pool, args: dict, ctx) -> str


# Registration order == CHAT_TOOLS order; chat.py controls it via the order it
# imports the domain modules in.
TOOL_REGISTRY: dict[str, AegisTool] = {}

_SCALAR_TYPES = {str: "string", bool: "boolean", int: "integer", float: "number"}
_UNION_ORIGINS = (typing.Union, types.UnionType)


def _field_constraints(metadata: tuple) -> dict:
    """Pull JSON-Schema `minimum`/`maximum` out of Annotated metadata.

    Accepts pydantic `Field(ge=…, le=…)` (a FieldInfo wrapping annotated_types
    constraints) as well as bare objects with `ge`/`le` attributes.
    """
    out: dict[str, Any] = {}
    for item in metadata:
        constraints = getattr(item, "metadata", None) or (item,)
        for c in constraints:
            ge = getattr(c, "ge", None)
            le = getattr(c, "le", None)
            if ge is not None:
                out["minimum"] = ge
            if le is not None:
                out["maximum"] = le
    return out


def _schema_for(annotation: Any, *, param: str, tool: str) -> dict:
    """Map a Python type annotation to a JSON-Schema fragment."""
    if annotation is inspect.Parameter.empty:
        raise TypeError(f"@aegis_tool {tool}: parameter '{param}' needs a type annotation")
    origin = get_origin(annotation)
    if origin is Annotated:
        base, *metadata = get_args(annotation)
        spec = _schema_for(base, param=param, tool=tool)
        spec.update(_field_constraints(tuple(metadata)))
        return spec
    if origin in _UNION_ORIGINS:
        members = [a for a in get_args(annotation) if a is not type(None)]
        if len(members) == 1:
            return _schema_for(members[0], param=param, tool=tool)
        return {
            "type": [
                _schema_for(a, param=param, tool=tool)["type"] for a in members
            ]
        }
    if annotation in _SCALAR_TYPES:
        return {"type": _SCALAR_TYPES[annotation]}
    if origin is Literal:
        values = list(get_args(annotation))
        return {"type": _SCALAR_TYPES[type(values[0])], "enum": values}
    if origin is list:
        (item_ann,) = get_args(annotation)
        return {"type": "array", "items": _schema_for(item_ann, param=param, tool=tool)}
    if annotation is dict or origin is dict:
        return {"type": "object"}
    raise TypeError(
        f"@aegis_tool {tool}: parameter '{param}' has unsupported annotation {annotation!r}"
    )


_ARG_ENTRY_RE = re.compile(r"^(\s*)(\w+):\s*(.*)$")


def _parse_docstring(doc: str, *, tool: str) -> tuple[str, dict[str, str]]:
    """Extract (tool description, {param: description}) from a docstring.

    The description is the first paragraph with its lines joined by single
    spaces. Argument descriptions come from a Google-style `Args:` block;
    continuation lines (deeper indent than the entry) are folded in.
    """
    lines = doc.splitlines()
    # First paragraph = tool description.
    desc_lines: list[str] = []
    for line in lines:
        if not line.strip():
            if desc_lines:
                break
            continue
        if line.strip() in ("Args:", "Returns:"):
            break
        desc_lines.append(line.strip())
    description = " ".join(desc_lines)

    arg_docs: dict[str, str] = {}
    try:
        start = next(i for i, line in enumerate(lines) if line.strip() == "Args:")
    except StopIteration:
        return description, arg_docs
    current: str | None = None
    current_indent = 0
    for line in lines[start + 1 :]:
        if not line.strip():
            continue
        match = _ARG_ENTRY_RE.match(line)
        if match and (current is None or len(match.group(1)) <= current_indent):
            if current is None:
                current_indent = len(match.group(1))
            elif len(match.group(1)) < current_indent:
                break  # dedented out of the Args block (e.g. a "Returns:" section)
            current = match.group(2)
            arg_docs[current] = match.group(3).strip()
        elif current is not None:
            arg_docs[current] = (arg_docs[current] + " " + line.strip()).strip()
        else:
            break
    return description, arg_docs


def aegis_tool(
    fn=None,
    *,
    name: str | None = None,
    hide: tuple[str, ...] = (),
    empty_required: bool = False,
):
    """Register a chat tool; schema generated from the signature + docstring.

    `name` overrides the default (the function name minus a leading `_exec_`).
    `hide` lists signature parameters that are accepted at call time but must
    NOT appear in the advertised schema (legacy argument aliases).
    `empty_required` keeps an explicit `"required": []` on a tool whose
    parameters are all optional — JSON Schema treats that as identical to
    omitting the key, but a hand-written schema that spelled it out keeps
    spelling it out, so a migration is a byte-for-byte no-op against the
    golden snapshot (`whats_next` is the only such tool).
    """

    def decorate(func):
        tool_name = name or func.__name__.removeprefix("_exec_")
        hints = typing.get_type_hints(func, include_extras=True)
        params = [
            p for p in inspect.signature(func).parameters.values() if p.name not in ("pool", "ctx")
        ]
        description, arg_docs = _parse_docstring(inspect.getdoc(func) or "", tool=tool_name)
        if not description:
            raise TypeError(f"@aegis_tool {tool_name}: docstring needs a description paragraph")
        properties: dict[str, dict] = {}
        required: list[str] = []
        param_names: set[str] = set()
        for p in params:
            param_names.add(p.name)
            if p.name in hide:
                continue
            spec = _schema_for(hints.get(p.name, p.annotation), param=p.name, tool=tool_name)
            if p.name in arg_docs:
                spec["description"] = arg_docs[p.name]
            if p.default is inspect.Parameter.empty:
                required.append(p.name)
            elif p.default is not None:
                spec["default"] = p.default
            properties[p.name] = spec
        parameters: dict[str, Any] = {"type": "object", "properties": properties}
        if required or empty_required:
            parameters["required"] = required

        @functools.wraps(func)
        async def wrapper(pool, args, ctx):
            kwargs = {k: v for k, v in (args or {}).items() if k in param_names}
            return await func(pool, ctx, **kwargs)

        TOOL_REGISTRY[tool_name] = AegisTool(
            name=tool_name, description=description, parameters=parameters, executor=wrapper
        )
        return wrapper

    if fn is not None:
        return decorate(fn)
    return decorate
