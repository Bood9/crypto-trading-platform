"""Tests for the secrets filter.

Spec §15: no secrets in logs. The processor itself is tested because
it is last in the chain and must catch everything that reached output,
regardless of which module put it there.
"""

from __future__ import annotations

from src.core.logging import REDACTED, redact_processor


def redact(event: dict) -> dict:
    return redact_processor(None, "info", event)


def test_secret_keys_are_replaced():
    out = redact({"event": "auth", "api_key": "abc123", "api_secret": "s3cr3t"})
    assert out["api_key"] == REDACTED
    assert out["api_secret"] == REDACTED
    assert out["event"] == "auth"


def test_key_matching_is_case_insensitive():
    out = redact({"API_KEY": "abc", "Password": "hunter2"})
    assert out["API_KEY"] == REDACTED
    assert out["Password"] == REDACTED


def test_nested_structures_are_cleaned():
    out = redact({"payload": {"headers": {"X-BAPI-SIGN": "deadbeef"}, "ok": True}})
    assert out["payload"]["headers"]["X-BAPI-SIGN"] == REDACTED
    assert out["payload"]["ok"] is True


def test_secrets_inside_lists_are_cleaned():
    out = redact({"items": [{"token": "aaa"}, {"safe": "bbb"}]})
    assert out["items"][0]["token"] == REDACTED
    assert out["items"][1]["safe"] == "bbb"


def test_inline_secret_in_free_text_is_cut():
    """A secret that ended up inside the message body cannot be cut out by key."""
    out = redact({"event": "request failed: api_key=abc123def and more"})
    assert "abc123def" not in out["event"]
    assert REDACTED in out["event"]


def test_bearer_token_in_text_is_cut():
    out = redact({"event": "Authorization: Bearer abcdef1234567890"})
    assert "abcdef1234567890" not in out["event"]


def test_ordinary_values_pass_through_untouched():
    event = {"event": "order.filled", "symbol": "SOLUSDT", "qty": 0.9, "price": 73.8}
    assert redact(event) == event


def test_non_string_keys_do_not_crash():
    out = redact({1: "a", None: "b", "event": "ok"})
    assert out[1] == "a"
    assert out["event"] == "ok"
