"""Unit tests for the symbol rules, independent of git.

The repository-level tests in `test_git_analysis.py` prove the pipeline; these prove the two rules
the pipeline depends on and that no git fixture can distinguish: the innermost definition wins, and
a read that failed is an error rather than an empty answer.
"""

from __future__ import annotations

import pytest

from core.git.symbols import NotAttributed, attribute, enclosing, parse_python

MODULE = '''"""Store."""


class Store:
    def load(self, text):
        def strip_chunk(chunk):
            return chunk.strip()
        return strip_chunk(text)

    @staticmethod
    def save(value):
        return str(value)


def top(level):
    return level
'''


def names(text: str) -> list[str]:
    return [item.name for item in parse_python(text)]


def test_definitions_are_qualified_and_typed():
    found = {item.name: item for item in parse_python(MODULE)}
    assert set(found) == {"Store", "Store.load", "Store.load.strip_chunk", "Store.save", "top"}
    assert found["Store"].kind == "class"
    assert found["Store.load"].kind == "method"
    assert found["Store.load.strip_chunk"].kind == "function", "a helper is not a method"


def test_a_decorator_belongs_to_the_function_under_it():
    found = {item.name: item for item in parse_python(MODULE)}
    assert found["Store.save"].start == MODULE[:MODULE.index("@staticmethod")].count("\n") + 1


def test_enclosing_picks_the_innermost_definition():
    definitions = sorted(parse_python(MODULE), key=lambda i: (i.end - i.start, i.start))
    line = MODULE[:MODULE.index("return chunk.strip()")].count("\n") + 1
    assert enclosing(definitions, line).name == "Store.load.strip_chunk"
    assert enclosing(definitions, 1) is None, "a docstring line is inside no definition"


def test_a_line_between_definitions_is_attributed_to_none():
    definitions = parse_python(MODULE)
    line = MODULE[:MODULE.index("def top(")].count("\n") + 1
    assert enclosing(sorted(definitions, key=lambda i: i.end - i.start), line - 1) is None


def test_modified_added_and_removed_are_different_claims():
    old = MODULE.replace("return chunk.strip()", "return chunk.strip()\n        # noqa")
    new = MODULE.replace("def top(level):", "def other(level):")
    found = {item["name"]: item["change"]
             for item in attribute(old, new, [6, 16, 17], [7, 15, 16])}
    assert found == {"Store.load.strip_chunk": "modified", "top": "removed", "other": "added"}


def test_a_new_file_is_every_definition_added():
    found = attribute(None, MODULE, [], [], new_file=True)
    assert all(item["change"] == "added" for item in found)
    assert {item["name"] for item in found} == set(names(MODULE))


def test_a_deleted_file_is_every_definition_removed():
    found = attribute(MODULE, None, [], [], deleted=True)
    assert all(item["change"] == "removed" for item in found)


def test_a_side_that_cannot_be_read_is_not_an_empty_change_set():
    with pytest.raises(NotAttributed, match="could not be read"):
        attribute(None, MODULE, [], [])


def test_unparseable_python_raises_instead_of_reporting_no_symbols():
    with pytest.raises(NotAttributed):
        attribute("def broken(:\n    return 1\n", MODULE, [1], [])
    with pytest.raises(NotAttributed):
        attribute(MODULE, "def broken(:\n    return 1\n", [], [1])


def test_a_rename_of_a_definition_is_removed_plus_added():
    renamed = MODULE.replace("def save", "def write")
    found = attribute(MODULE, renamed, [11], [11])
    assert {item["name"]: item["change"] for item in found} == {"Store.save": "removed",
                                                                "Store.write": "added"}
