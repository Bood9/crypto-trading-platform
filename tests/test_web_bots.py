"""Trade path on the panel (DEC-049): per-symbol outcome, night retry, rejection tally."""

from __future__ import annotations

from datetime import UTC, datetime

from src.web.bots import Step, assemble, next_runs


def step(i, run, symbol, stage, verdict, code=None, origin="rule", **detail):
    return Step(i, run, symbol, stage, verdict, code, origin, detail)


def test_outcome_per_symbol_and_split_of_rule_and_platform():
    steps = [
        step(1, 10, "AUSDT", "signal", "pass"),
        step(2, 11, "AUSDT", "risk", "pass"),
        step(3, 12, "AUSDT", "execution", "pass"),
        step(4, 13, "AUSDT", "verify", "pass"),
        step(5, 10, "BUSDT", "signal", "pass"),
        step(6, 11, "BUSDT", "risk", "reject", "MAX_POSITIONS"),
        step(7, 12, "BUSDT", "execution", "reject", "MAX_POSITIONS"),
        step(8, 10, "CUSDT", "signal", "reject", "STALE_DATA", "platform"),
        step(9, 10, "DUSDT", "signal", "pass"),
        step(10, 11, "DUSDT", "risk", "pass"),
        step(11, 12, "DUSDT", "execution", "reject", "RISK_MISMATCH", "platform"),
        step(12, 12, "EUSDT", "execution", "info", "TIME_STOP", pnl="-1.5"),
    ]
    day = assemble(steps, anchor=10)
    kinds = {r["symbol"]: r["outcome"] for r in day["rows"]}
    assert kinds == {
        "AUSDT": {"kind": "entered", "stage": "execution", "code": None},
        "BUSDT": {"kind": "rule", "stage": "risk", "code": "MAX_POSITIONS"},
        "CUSDT": {"kind": "platform", "stage": "signal", "code": "STALE_DATA"},
        "DUSDT": {"kind": "platform", "stage": "execution", "code": "RISK_MISMATCH"},
    }
    assert day["outcomes"] == {"entered": 1, "flagged": 0, "rule": 1, "platform": 2, "pending": 0}
    assert day["funnel"] == {"signal": 3, "risk": 2, "execution": 1, "verify": 1}
    assert day["closes"] == [{"symbol": "EUSDT", "reason": "TIME_STOP", "pnl": "-1.5", "ts": None}]
    assert [r["symbol"] for r in day["rows"]][0] == "AUSDT", "entered ones on top"


def test_retry_shows_only_the_last_attempt():
    """First attempt: the Risk Manager disagreed with the Analyst. A retry an
    hour later was approved and executed. The retry is shown; all closes are kept."""
    steps = [
        step(1, 10, "AUSDT", "signal", "pass"),
        step(2, 11, "AUSDT", "risk", "reject", "SIGNAL_MISMATCH", "platform"),
        step(3, 12, "AUSDT", "execution", "reject", "RISK_NOT_APPROVED", "platform"),
        step(4, 12, "BUSDT", "execution", "info", "STOP", pnl="-1"),
        step(5, 20, "AUSDT", "signal", "pass"),
        step(6, 21, "AUSDT", "risk", "pass"),
        step(7, 22, "AUSDT", "execution", "pass"),
    ]
    day = assemble(steps, anchor=20)
    assert day["rows"][0]["outcome"]["kind"] == "entered"
    assert day["outcomes"]["platform"] == 0
    assert [c["symbol"] for c in day["closes"]] == ["BUSDT"]


def test_entry_the_controller_did_not_confirm_is_flagged():
    steps = [
        step(1, 1, "AUSDT", "signal", "pass"),
        step(2, 3, "AUSDT", "execution", "pass"),
        step(3, 4, "AUSDT", "verify", "fail", "RISK_NOT_APPROVED", "platform"),
    ]
    day = assemble(steps, anchor=None)
    assert day["rows"][0]["outcome"] == {"kind": "flagged", "stage": "verify",
                                         "code": "RISK_NOT_APPROVED"}


def test_signal_without_later_stages_is_pending():
    day = assemble([step(1, 1, "AUSDT", "signal", "pass")], anchor=1)
    assert day["rows"][0]["outcome"] == {"kind": "pending", "stage": "signal", "code": None}


def test_next_runs_are_the_next_05_minutes():
    now = datetime(2026, 9, 29, 22, 30, tzinfo=UTC)
    assert next_runs(now) == {"night": "2026-09-30T00:05:00+00:00",
                              "day": "2026-09-29T23:05:00+00:00"}
    early = datetime(2026, 9, 30, 0, 3, tzinfo=UTC)
    assert next_runs(early) == {"night": "2026-09-30T00:05:00+00:00",
                                "day": "2026-09-30T00:05:00+00:00"}


async def test_bad_moment_is_a_client_error_not_a_crash():
    import pytest
    from fastapi import HTTPException

    from src.web.bots import paths

    with pytest.raises(HTTPException) as err:
        await paths(book="setup_e:paper", moment="yesterday", nights=30)
    assert err.value.status_code == 400
