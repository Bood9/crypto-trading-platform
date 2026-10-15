"""The funding interval changes over time. DEC-025, DEC-036.

Two defects of the same nature — "a symbol's interval is constant and equal
to today's":

* pages were sliced by today's interval, and in periods when
  funding ran more often, the exchange returned the window's 200 newest records while the older
  ones were lost. For SOLAYERUSDT — up to three quarters of the records;
* rates were normalized to 8 hours by today's interval, in places
  off by a factor of four.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.collectors.funding import (
    PAGING_INTERVAL,
    VALID_INTERVALS,
    FundingCollector,
    infer_intervals,
    normalize_to_8h,
)

T0 = datetime(2025, 1, 1, tzinfo=UTC)
H = timedelta(hours=1)


def ms(d: datetime) -> int:
    return int(d.timestamp() * 1000)


class ShiftingIntervalBybit:
    """Funding once an hour before `switch`, once every 4 hours after — like SOLAYER.

    Returns records in [start, end] inclusive, the newest on truncation,
    like the real Bybit.
    """

    def __init__(self, switch: datetime, until: datetime) -> None:
        self.switch = switch
        self.until = until
        self.calls = 0
        stamps, t = [], T0
        while t < until:
            stamps.append(t)
            t += H if t < switch else H * 4
        self.stamps = stamps

    async def get_funding_history(self, symbol, *, start, end, limit=200, category=None):  # noqa: ANN001
        self.calls += 1
        inside = [t for t in self.stamps if start <= ms(t) <= end]
        return [
            {"fundingRate": "0.0001", "fundingRateTimestamp": str(ms(t))}
            for t in reversed(inside[-limit:])
        ]


# ---------------------------------------------------------------------------
# Inferring the interval
# ---------------------------------------------------------------------------
def test_interval_is_taken_from_the_previous_gap():
    stamps = [T0, T0 + H, T0 + H * 2, T0 + H * 6, T0 + H * 10]
    assert infer_intervals(stamps, 480) == [60, 60, 60, 240, 240]


def test_first_record_borrows_the_next_gap():
    stamps = [T0, T0 + H * 8, T0 + H * 16]
    assert infer_intervals(stamps, 60) == [480, 480, 480]


def test_hole_in_the_data_is_not_mistaken_for_an_interval():
    """A 21-hour gap is not a Bybit interval but a hole."""
    stamps = [T0, T0 + H * 8, T0 + H * 29, T0 + H * 37]
    out = infer_intervals(stamps, 480)
    assert out[2] == 480  # 21 h does not fit, the next gap is taken
    assert all(v in VALID_INTERVALS for v in out)


def test_lonely_record_falls_back_to_the_declared_interval():
    assert infer_intervals([T0], 240) == [240]
    assert infer_intervals([], 240) == []


# ---------------------------------------------------------------------------
# A collector on an exchange whose interval changed
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_no_record_is_lost_when_the_past_interval_was_shorter():
    """The SOLAYERUSDT scenario: hourly before, once every 4 hours today.

    The old slicing by today's interval lost three quarters of the
    records of the hourly period here.
    """
    switch = T0 + timedelta(days=60)
    until = T0 + timedelta(days=120)
    exchange = ShiftingIntervalBybit(switch, until)
    collector = FundingCollector(exchange, interval_minutes=240)

    rows, _ = await collector.fetch("SOLAYERUSDT", T0, until)

    got = sorted(r["ts"] for r in rows)
    assert got == exchange.stamps, (
        f"got {len(got)} of {len(exchange.stamps)} accruals"
    )


@pytest.mark.asyncio
async def test_normalization_follows_the_interval_in_force_at_the_time():
    """The hourly rate ×8, the four-hour one ×2 — not both by today's."""
    switch = T0 + timedelta(days=10)
    until = T0 + timedelta(days=20)
    collector = FundingCollector(ShiftingIntervalBybit(switch, until), interval_minutes=240)

    rows, _ = await collector.fetch("SOLAYERUSDT", T0, until)
    early = next(r for r in rows if T0 + H * 5 <= r["ts"] < switch)
    late = next(r for r in rows if r["ts"] > switch + H * 8)

    assert early["interval_minutes"] == 60
    assert early["rate_8h"] == normalize_to_8h(early["rate"], 60)
    assert late["interval_minutes"] == 240
    assert late["rate_8h"] == normalize_to_8h(late["rate"], 240)


def test_paging_uses_the_shortest_possible_interval():
    """At an hourly step no more than 200 accruals fit into a window
    at any interval — so the exchange cuts nothing off."""
    assert timedelta(minutes=min(VALID_INTERVALS)) == PAGING_INTERVAL
