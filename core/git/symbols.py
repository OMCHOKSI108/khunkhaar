"""Symbol attribution from the source, not from git's guess.

`git diff` labels a hunk with the nearest line matching its `userdiff` regex. That is a heuristic
with two known failure modes: for a file type with no driver it prints arbitrary preceding text,
and even for Python it can name a function that the change never touched while staying silent about
the one that was added or deleted. Where the stdlib `ast` module can read the file, this module
answers three questions git cannot: which definition a changed line is really inside, which
definitions are new, and which ones are gone.

A file that is named `.py` but does not parse is reported as *not attributed*. "No symbols" and
"symbols unknown" are different claims, and only the first one would be a lie.
"""

from __future__ import annotations

import ast

# Definitions worth naming: anything with a body a change can land inside.
_NODES = {
    ast.FunctionDef: "function",
    ast.AsyncFunctionDef: "async function",
    ast.ClassDef: "class",
}


class NotAttributed(RuntimeError):
    """This text is not parseable Python 3, so no symbol can honestly be named inside it."""


class Definition:
    """One Python definition and the line range it occupies, decorators included."""

    __slots__ = ("name", "kind", "start", "end")

    def __init__(self, name: str, kind: str, start: int, end: int):
        self.name = name
        self.kind = kind
        self.start = start
        self.end = end

    def covers(self, line: int) -> bool:
        return self.start <= line <= self.end

    def __repr__(self) -> str:  # only used by test failures
        return f"<{self.kind} {self.name} {self.start}-{self.end}>"


def parse_python(text: str) -> list[Definition]:
    """Every function, method and class in `text`, with qualified names (`Store.write`).

    Raises SyntaxError (or ValueError on a null byte) when the file is not valid Python 3; the
    caller decides what to report.
    """
    tree = ast.parse(text)
    out: list[Definition] = []
    _walk(tree, out, prefix="", in_class=False)
    return out


def _walk(node: ast.AST, out: list[Definition], *, prefix: str, in_class: bool) -> None:
    for child in ast.iter_child_nodes(node):
        kind = _NODES.get(type(child))
        if kind is None:
            _walk(child, out, prefix=prefix, in_class=in_class)
            continue
        name = f"{prefix}.{child.name}" if prefix else child.name
        if in_class and kind in ("function", "async function"):
            kind = "method"
        out.append(Definition(name, kind, _start(child), _end(child)))
        # A nested function belongs to the function that contains it, not to the class.
        _walk(child, out, prefix=name, in_class=isinstance(child, ast.ClassDef))


def _start(node) -> int:
    """A decorator line is part of the definition: editing one changes the function it decorates."""
    lines = [node.lineno]
    lines.extend(getattr(dec, "lineno", 0) for dec in getattr(node, "decorator_list", []))
    return min(line for line in lines if line)


def _end(node) -> int:
    return getattr(node, "end_lineno", None) or node.lineno


def enclosing(definitions: list[Definition], line: int) -> Definition | None:
    """The innermost definition around `line`, so a nested helper is not credited to its parent."""
    for item in definitions:  # ordered innermost-first by `attribute`
        if item.covers(line):
            return item
    return None


def _definitions(text: str) -> list[Definition]:
    """Definitions ordered smallest-span first, so the innermost match is found first."""
    try:
        found = parse_python(text)
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise NotAttributed(f"{type(exc).__name__}: {exc}") from exc
    return sorted(found, key=lambda item: (item.end - item.start, item.start))


def attribute(old_text: str | None, new_text: str | None,
              old_lines: list[int], new_lines: list[int], *, new_file: bool = False,
              deleted: bool = False) -> list[dict]:
    """Symbol-level changes between two versions of one Python file.

    `old_lines` are lines the diff removed (numbered in the old file) and `new_lines` lines it
    added (numbered in the new file). Definitions that no changed line falls inside are not
    reported, which is the point: a change that only shifts a definition's start line is not a
    change to it.

    `new_file` and `deleted` are how the caller says "this side does not exist because the file is
    new / gone", which is a whole-file claim: every definition in the surviving side is an addition
    or a removal. A missing text without one of those flags is a read that failed, and a failed
    read is reported as unknown rather than turned into a claim about what the diff contains.

    Raises `NotAttributed` when a side that is present does not parse as Python 3 - a template, a
    Python 2 leftover, a truncated read.
    """
    if new_file:
        if new_text is None:
            raise NotAttributed("a new file with no readable content")
        return [_entry(item, "added") for item in _definitions(new_text)]
    if deleted:
        if old_text is None:
            raise NotAttributed("a deleted file with no readable content")
        return [_entry(item, "removed") for item in _definitions(old_text)]
    if old_text is None or new_text is None:
        raise NotAttributed("one side of the diff could not be read")

    old_defs = _definitions(old_text)
    new_defs = _definitions(new_text)
    old_names = {item.name for item in old_defs}
    new_names = {item.name for item in new_defs}
    found: dict[str, dict] = {}

    for line in new_lines:
        symbol = enclosing(new_defs, line)
        if symbol is None:
            continue
        _record(found, symbol, "modified" if symbol.name in old_names else "added")

    for line in old_lines:
        symbol = enclosing(old_defs, line)
        if symbol is None:
            continue
        _record(found, symbol, "modified" if symbol.name in new_names else "removed")

    return sorted(found.values(), key=lambda item: (item["start"], item["name"]))


def _entry(symbol: Definition, change: str) -> dict:
    return {"name": symbol.name, "kind": symbol.kind, "change": change,
            "start": symbol.start, "end": symbol.end}


def _record(found: dict[str, dict], symbol: Definition, change: str) -> None:
    entry = found.setdefault(symbol.name, _entry(symbol, change))
    # A definition can be both touched and renamed away; 'removed' is the stronger claim.
    order = {"added": 0, "modified": 1, "removed": 2}
    if order[change] > order[entry["change"]]:
        entry["change"] = change
