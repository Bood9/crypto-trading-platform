"""Human-readable run journal. Phase 5, DEC-041.

The `live_journal` table is for the machine and the panel: one record per decision.
This file is for a human who opens it a few days later and wants to understand
in a minute what happened: one block per run, the summary on top, details
below. Night-run failures (the database did not come up, the cycle crashed)
are written here too by `scripts/scheduled.sh` — everything in one place.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from src.live.cycle import CycleReport
from src.live.signals import Skip

JOURNAL = Path("var/live/journal.md")
MSK = timedelta(hours=3)

#: Skip codes in plain words. An unknown code is printed as is.
REASONS = {
    "MAX_POSITIONS": "all slots taken",
    "MAX_MARGIN": "total margin ceiling",
    "CORRELATION": "correlated with an open position",
    "DAILY_STOP": "daily loss limit",
    "WEEKLY_STOP": "weekly loss limit",
    "MODE_OFF": "trading disabled",
    "MIN_NOTIONAL": "below the exchange minimum order",
    "LIQ_BUFFER": "stop too close to liquidation",
    "PRICE_DIVERGENCE": "price moved more than 3% from the close",
    "NO_PRICE": "no fresh price",
    "NO_INSTRUMENT": "instrument not trading",
    "STALE_SIGNAL": "signal on a stale bar",
    "ENTRY_WINDOW_MISSED": "run later than the entry window (02:00 UTC)",
    "INSUFFICIENT_BALANCE": "equity exhausted",
}


def _stamp(moment: datetime) -> str:
    utc = moment.astimezone(UTC)
    return f"{utc:%Y-%m-%d %H:%M} UTC ({(utc + MSK):%H:%M} MSK)"


def _money(value) -> str:  # noqa: ANN001
    return f"{value:+.2f} $"


def render(
    *,
    book: str,
    now: datetime,
    execute: bool,
    universe: int,
    signals: int,
    skips: dict[str, Skip],
    report: CycleReport,
    events: list[str],
    as_of: datetime | None = None,
    problem: str | None = None,
    note: str | None = None,
) -> str:
    """One run block in markdown."""
    mode = "execution" if execute else "preview (without --execute)"
    stale = sorted(s for s, k in skips.items() if k is Skip.STALE_DATA)

    headline = []
    if problem:
        headline.append(f"**{problem}**")
    if report.halted:
        headline.append("**TRADING DISABLED — a human is needed**")
    if report.closed:
        total = sum((pnl for *_, pnl in report.closed), Decimal(0))
        headline.append(f"closed {len(report.closed)} ({_money(total)})")
    if report.opened:
        headline.append(f"opened: {', '.join(report.opened)}")
    if not headline:
        headline.append("no trades")

    lines = [
        f"## {_stamp(now)} · {book} · {mode}",
        "",
        f"**Summary:** {'; '.join(headline)}. "
        f"Equity **{report.equity:.2f} $**, mode {report.mode.value}.",
        "",
        f"- decision on the daily close of {report.moment - timedelta(days=1):%Y-%m-%d}; "
        f"universe {universe}, signals {signals}",
    ]
    if note:
        lines.append(f"- {note}")
    if as_of is not None and as_of < now:
        lines.append(
            f"- run later than scheduled: decision on prices as of {_stamp(as_of)}"
        )
    if stale:
        lines.append(f"- **stale data** ({len(stale)}): {', '.join(stale)}")
    for event in events:
        lines.append(f"- exchange stop: {event}")
    for symbol, reason, pnl in report.closed:
        why = {"STOP": "by stop", "TIME_STOP": "by time exit (7 days)",
               "EMERGENCY": "emergency"}.get(reason, reason)
        lines.append(f"- closed {symbol} {why}: {_money(pnl)}")
    for note in report.notes:
        head = note.split(" ", 1)[0]
        if head in {"OPENED", "WOULD_OPEN", "HALT", "STOP_PLACED", "STOP_REPAIR_FAILED",
                    "EMERGENCY_CLOSE", "ENTRY_FAILED", "CLOSE_FAILED", "VOID"}:
            lines.append(f"- {note}")
    if report.skipped:
        counts = Counter(code for _, code in report.skipped)
        parts = [f"{REASONS.get(code, code)} — {n}" for code, n in counts.most_common()]
        lines.append(f"- signals skipped: {'; '.join(parts)}")
    lines += ["", "---", ""]
    return "\n".join(lines)


def failure(*, book: str, now: datetime, reason: str) -> str:
    """Block for a run that did not reach a decision."""
    return f"## {_stamp(now)} · {book} · NOT EXECUTED\n\n**Reason:** {reason}\n\n---\n\n"


def monitor_block(*, book: str, now: datetime, report: CycleReport, events: list[str]) -> str:
    """Day watch block — written only when something happened."""
    lines = [f"## {_stamp(now)} · {book} · day watch", ""]
    if report.halted:
        lines.append("**TRADING DISABLED — a human is needed**")
    for event in events:
        lines.append(f"- exchange stop: {event}")
    for symbol, reason, pnl in report.closed:
        why = {"STOP": "by stop", "TIME_STOP": "by time exit", "EMERGENCY": "emergency"}.get(
            reason, reason
        )
        lines.append(f"- closed {symbol} {why}: {_money(pnl)}")
    lines.append(f"- equity {report.equity:.2f} $, mode {report.mode.value}")
    lines += ["", "---", ""]
    return "\n".join(lines)


def append(text: str, path: Path = JOURNAL) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    new = not path.exists()
    with path.open("a", encoding="utf-8") as fh:
        if new:
            fh.write(
                "# Run journal\n\n"
                "One block per run, newest at the bottom. Details of each decision are "
                "in the panel (`python -m src.cli web`) and in `trade-status`.\n\n---\n\n"
            )
        fh.write(text)


__all__ = ["JOURNAL", "REASONS", "append", "failure", "monitor_block", "render"]
