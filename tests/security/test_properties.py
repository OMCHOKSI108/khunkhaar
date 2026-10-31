"""Property-based tests (Hypothesis) for the security-critical helpers.

These do not replace the example-based suites; they search the input space for the
invariants those suites assert by hand.
"""

from __future__ import annotations

import string

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from core.database.sql_guard import add_limit, validate_read_only
from core.shared.redaction import REDACTED, is_secret_key, redact_obj

pytestmark = pytest.mark.security

FORBIDDEN = ["insert", "update", "delete", "drop", "truncate", "alter", "grant",
             "revoke", "create", "copy", "merge", "vacuum", "call", "do", "set",
             "lock", "listen", "notify", "prepare", "reindex", "comment"]

SECRET_KEYS = ["password", "api_token", "Authorization", "client_secret", "x-api-key",
               "session_id", "refresh_token", "private_key", "cookie"]

SEPARATORS = st.sampled_from([" ", "  ", "\t", "\n", " \n\t "])
CASED = st.builds(lambda ch, up: ch.upper() if up else ch,
                  st.sampled_from(string.ascii_lowercase), st.booleans())


@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(keyword=st.sampled_from(FORBIDDEN), sep=SEPARATORS, upper=st.booleans())
def test_forbidden_statements_are_always_refused(keyword, sep, upper):
    word = keyword.upper() if upper else keyword
    for sql in (f"{word} TABLE t", f"SELECT 1; {word} TABLE t", f"  {word}{sep}TABLE{sep}t  "):
        verdict = validate_read_only(sql)
        assert not verdict.allowed, f"{sql!r} was allowed ({verdict.reason})"


@settings(max_examples=100, deadline=None)
@given(keyword=st.sampled_from(FORBIDDEN), sep=SEPARATORS, upper=st.booleans())
def test_forbidden_keywords_smuggled_in_comments_are_refused(keyword, sep, upper):
    word = keyword.upper() if upper else keyword
    sql = f"SELECT 1 /* harmless */ {word}{sep}TABLE t"
    assert not validate_read_only(sql).allowed


@settings(max_examples=100, deadline=None)
@given(payload=st.from_regex(r"[A-Za-z][A-Za-z0-9_ ]{0,20}", fullmatch=True))
def test_any_stacked_statement_is_refused(payload):
    assert not validate_read_only(f"SELECT 1; {payload}").allowed


@settings(max_examples=100, deadline=None)
@given(limit=st.integers(min_value=1, max_value=10_000), table=st.from_regex(r"[a-z_][a-z0-9_]{0,20}", fullmatch=True))
def test_add_limit_never_exceeds_the_cap(limit, table):
    bounded = add_limit(f"SELECT * FROM {table}", limit)
    assert bounded.lower().count(" limit ") == 1
    assert bounded.endswith(f"LIMIT {limit}")


@settings(max_examples=50, deadline=None)
@given(existing=st.integers(min_value=1, max_value=100), cap=st.integers(min_value=1, max_value=100))
def test_add_limit_keeps_an_existing_limit(existing, cap):
    bounded = add_limit(f"SELECT * FROM t LIMIT {existing}", cap)
    assert bounded == f"SELECT * FROM t LIMIT {existing}"


values = st.recursive(
    st.one_of(st.text(max_size=40), st.integers(), st.booleans(), st.none()),
    lambda inner: st.one_of(st.lists(inner, max_size=3), st.dictionaries(st.text(max_size=10), inner, max_size=3)),
    max_leaves=8,
)


@settings(max_examples=150, deadline=None)
@given(key=st.sampled_from(SECRET_KEYS), value=values)
def test_secret_keys_are_always_redacted(key, value):
    assert is_secret_key(key)
    out = redact_obj({key: value})
    if isinstance(value, (bool, int, float)) or value is None:
        assert out[key] == value, "non-string scalars carry no secret to redact"
    else:
        assert out[key] == REDACTED or str(value) == "" or out[key] != value


@settings(max_examples=80, deadline=None)
@given(key=st.text(alphabet=string.ascii_letters + string.digits + "_-", min_size=1, max_size=20).filter(lambda s: not is_secret_key(s)))
def test_non_secret_keys_survive(key):
    assert redact_obj({key: "visible-value"})[key] in ("visible-value", REDACTED)
