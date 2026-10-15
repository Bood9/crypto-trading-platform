"""Interrupting collection must not lose progress.

A multi-hour backfill must survive Ctrl+C and SIGTERM so that
a rerun continues from the point of interruption rather than starting over.

Invariant: the checkpoint moves ONLY after a successful write. So
an interruption can cost at most one window (symbol, stream), and that
window will be re-downloaded, not lost.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.collectors import base as base_module
from src.collectors.base import BaseCollector
from src.core.models import DataStream


class Recorder:
    """Fake in-memory checkpoints."""

    def __init__(self) -> None:
        self.successes: list[tuple] = []
        self.failures: list[tuple] = []
        self.state: dict[tuple, tuple] = {}

    async def get(self, symbol, stream):
        entry = self.state.get((symbol, stream))
        if entry is None:
            return None
        from src.collectors.checkpoints import Checkpoint

        first, last = entry
        return Checkpoint(symbol, stream, first, last, last, 0)

    async def success(self, symbol, stream, last_ts, first_ts=None):
        self.successes.append((symbol, stream, first_ts, last_ts))
        if last_ts is not None:
            prev = self.state.get((symbol, stream))
            new_first = min(filter(None, [first_ts, prev[0] if prev else None]), default=first_ts)
            new_last = max(filter(None, [last_ts, prev[1] if prev else None]), default=last_ts)
            self.state[(symbol, stream)] = (new_first, new_last)

    async def failure(self, symbol, stream, error):
        self.failures.append((symbol, stream, error))


@pytest.fixture
def recorder(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(base_module, "get_checkpoint", rec.get)
    monkeypatch.setattr(base_module, "record_success", rec.success)
    monkeypatch.setattr(base_module, "record_failure", rec.failure)
    return rec


class InterruptingCollector(BaseCollector):
    """A collector that is interrupted at a given stage."""

    stream = DataStream.KLINE_1H
    interval = timedelta(hours=1)

    def __init__(self, *, fail_at: str, exc: BaseException) -> None:
        super().__init__(client=None)
        self.fail_at = fail_at
        self.exc = exc
        self.stored: list[dict] = []

    async def fetch(self, symbol, start, end):
        if self.fail_at == "fetch":
            raise self.exc
        return [
            {"symbol": symbol, "ts": start + timedelta(hours=i), "value": i}
            for i in range(3)
        ], 1

    async def store(self, rows):
        if self.fail_at == "store":
            raise self.exc
        self.stored.extend(rows)
        return len(rows)


T0 = datetime(2026, 8, 1, tzinfo=UTC)
T1 = datetime(2026, 8, 2, tzinfo=UTC)


@pytest.mark.parametrize("exc", [KeyboardInterrupt(), SystemExit()])
async def test_interrupt_during_fetch_leaves_checkpoint_untouched(recorder, exc):
    """An interruption before the write must not move the boundary.

    A shift here would mean silently losing a window: the next pass would start
    from a point it never reached.
    """
    collector = InterruptingCollector(fail_at="fetch", exc=exc)

    with pytest.raises(type(exc)):
        await collector.run("SOLUSDT", start=T0, end=T1)

    assert recorder.successes == []
    assert collector.stored == []


@pytest.mark.parametrize("exc", [KeyboardInterrupt(), SystemExit()])
async def test_interrupt_during_store_leaves_checkpoint_untouched(recorder, exc):
    """The write is a single transaction: all or nothing.

    The checkpoint does not move, so the window will be re-downloaded in full.
    """
    collector = InterruptingCollector(fail_at="store", exc=exc)

    with pytest.raises(type(exc)):
        await collector.run("SOLUSDT", start=T0, end=T1)

    assert recorder.successes == []


async def test_completed_symbols_keep_their_progress(recorder):
    """An interruption costs at most one window.

    Symbols completed before the interruption keep their checkpoint — otherwise
    a multi-hour backfill would start over after every Ctrl+C.
    """
    done = InterruptingCollector(fail_at="", exc=KeyboardInterrupt())
    await done.run("SOLUSDT", start=T0, end=T1)

    interrupted = InterruptingCollector(fail_at="fetch", exc=KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        await interrupted.run("ETHUSDT", start=T0, end=T1)

    saved = {s[0] for s in recorder.successes}
    assert "SOLUSDT" in saved
    assert "ETHUSDT" not in saved


async def test_resume_after_interrupt_does_not_refetch(recorder):
    """A rerun continues from the point of interruption."""
    first = InterruptingCollector(fail_at="", exc=KeyboardInterrupt())
    await first.run("SOLUSDT", start=T0, end=T1)
    covered_last = recorder.state[("SOLUSDT", DataStream.KLINE_1H)][1]

    second = InterruptingCollector(fail_at="", exc=KeyboardInterrupt())
    windows = second.plan_windows(
        T0, T1, *recorder.state[("SOLUSDT", DataStream.KLINE_1H)]
    )

    assert all(start > covered_last for start, _ in windows)


async def test_ordinary_error_is_recorded_not_raised(recorder):
    """A regular failure is a rejection of one stream, not a halt of the traversal.

    It differs from an interruption: BybitError is recorded and the traversal goes
    on, KeyboardInterrupt propagates outward.
    """
    from src.exchange.bybit_client import BybitNetworkError

    collector = InterruptingCollector(
        fail_at="fetch", exc=BybitNetworkError("connection dropped")
    )
    result = await collector.run("SOLUSDT", start=T0, end=T1)

    assert not result.ok
    assert len(recorder.failures) == 1
