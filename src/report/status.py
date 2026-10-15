"""A snapshot of the project state as one self-contained HTML file.

A draft of the Phase 4 monitoring screen: the block structure is designed
so that backtest results and setup statistics fit in without reworking the
page.

Block rule: a value that has not been computed is shown as
"not computed", not as zero (P-001). A report that shows zero where
there is no data is worse than a missing one — it creates false confidence.
"""

from __future__ import annotations

import html
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text

from src.core.db import (
    FundingHistory,
    Instrument,
    Ohlcv,
    OpenInterest,
    UniverseSnapshot,
    session_scope,
)
from src.core.logging import get_logger

log = get_logger(__name__)

#: The value was not computed. Not zero and not an empty string: zero reads
#: as a measured result, emptiness as a forgotten block.
UNKNOWN = "not computed"


@dataclass
class Block:
    """A report section. An empty block is printed with a note, not hidden."""

    title: str
    rows: list[tuple[str, Any]] = field(default_factory=list)
    note: str | None = None
    table: list[list[Any]] | None = None
    headers: list[str] | None = None


def esc(value: Any) -> str:
    return html.escape(str(value))


class Raw(str):
    """Ready-made markup that must not be escaped.

    An explicit type rather than a "the string contains an angle bracket"
    heuristic: symbol names and error messages come from outside, and
    deciding by content whether something is markup or data is a way to
    one day render someone else's input as HTML.
    """

    __slots__ = ()


def _cell(value: Any) -> str:
    if isinstance(value, Raw):
        return str(value)
    return esc(fmt_num(value))


def fmt_num(value: Any) -> str:
    if value is None:
        return UNKNOWN
    if isinstance(value, (int, float, Decimal)):
        if isinstance(value, float) and value != value:  # NaN
            return UNKNOWN
        return f"{value:,.0f}".replace(",", " ") if abs(value) >= 1000 else str(value)
    return str(value)


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------
async def gather_data_block() -> Block:
    async with session_scope() as s:
        tf_rows = (
            await s.execute(
                select(
                    Ohlcv.timeframe,
                    func.count(func.distinct(Ohlcv.symbol)),
                    func.count(),
                    func.min(Ohlcv.ts),
                    func.max(Ohlcv.ts),
                ).group_by(Ohlcv.timeframe)
            )
        ).all()
        oi = (
            await s.execute(
                select(func.count(func.distinct(OpenInterest.symbol)), func.count())
            )
        ).one()
        fund = (
            await s.execute(
                select(func.count(func.distinct(FundingHistory.symbol)), func.count())
            )
        ).one()
        instruments = (await s.execute(select(func.count()).select_from(Instrument))).scalar_one()
        db_size = (
            await s.execute(text("SELECT pg_size_pretty(pg_database_size(current_database()))"))
        ).scalar_one()

    table = [
        [tf, syms, rows, first.date() if first else UNKNOWN, last.date() if last else UNKNOWN]
        for tf, syms, rows, first, last in sorted(tf_rows)
    ]
    table.append(["open_interest", oi[0], oi[1], "", ""])
    table.append(["funding", fund[0], fund[1], "", ""])

    return Block(
        title="Data",
        headers=["stream", "symbols", "records", "from", "to"],
        table=table,
        rows=[
            ("Instruments in cache", instruments),
            ("DB size", db_size),
        ],
    )


async def gather_universe_block() -> Block:
    async with session_scope() as s:
        months = (
            await s.execute(
                select(
                    UniverseSnapshot.month,
                    func.count().filter(UniverseSnapshot.included.is_(True)),
                )
                .group_by(UniverseSnapshot.month)
                .order_by(UniverseSnapshot.month)
            )
        ).all()
        unique = (
            await s.execute(
                select(func.count(func.distinct(UniverseSnapshot.symbol))).where(
                    UniverseSnapshot.included.is_(True)
                )
            )
        ).scalar_one()
        rejections = (
            await s.execute(
                select(UniverseSnapshot.rejection, func.count())
                .where(UniverseSnapshot.rejection.is_not(None))
                .group_by(UniverseSnapshot.rejection)
                .order_by(func.count().desc())
            )
        ).all()

    sizes = [n for _, n in months if n >= 20]
    return Block(
        title="Universe",
        rows=[
            ("Unique symbols over the period", unique),
            ("Months in the snapshot", len(months)),
            (
                "Full-month width",
                f"{min(sizes)}–{max(sizes)}" if sizes else UNKNOWN,
            ),
        ],
        headers=["rejection reason", "symbol-months"],
        table=[[code, n] for code, n in rejections],
        note=(
            "Width is exactly top_n in every full month — it binds the rank, "
            "not a liquidity threshold (DEC-009). This makes in-sample "
            "and out-of-sample comparable in the number of symbols."
        ),
    )


def git_info() -> dict[str, str]:
    def run(*args: str) -> str:
        try:
            return subprocess.run(
                args, capture_output=True, text=True, timeout=5, check=True
            ).stdout.strip()
        except (subprocess.SubprocessError, OSError):
            return UNKNOWN

    return {
        "Commit": run("git", "rev-parse", "--short", "HEAD"),
        "Branch": run("git", "rev-parse", "--abbrev-ref", "HEAD"),
        "Total commits": run("git", "rev-list", "--count", "HEAD"),
        "Latest": run("git", "log", "-1", "--format=%s"),
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------
_CSS = """
:root{--bg:#0f1115;--panel:#171a21;--line:#262b36;--fg:#d7dbe3;--dim:#8b93a3;
--ok:#4ade80;--warn:#fbbf24;--bad:#f87171;--acc:#60a5fa}
*{box-sizing:border-box}
body{margin:0;padding:32px 24px 64px;background:var(--bg);color:var(--fg);
font:14px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1100px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px;font-weight:600}
h2{font-size:15px;margin:0 0 14px;font-weight:600;letter-spacing:.02em}
.sub{color:var(--dim);font-size:13px;margin-bottom:28px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:16px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:18px 20px}
.card.wide{grid-column:1/-1}
dl{display:grid;grid-template-columns:1fr auto;gap:6px 16px;margin:0}
dt{color:var(--dim)}
dd{margin:0;text-align:right;font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse;margin-top:12px;font-size:13px}
th{text-align:left;color:var(--dim);font-weight:500;padding:6px 8px;
border-bottom:1px solid var(--line)}
td{padding:6px 8px;border-bottom:1px solid rgba(38,43,54,.5);font-variant-numeric:tabular-nums}
td:not(:first-child),th:not(:first-child){text-align:right}
tr:last-child td{border-bottom:none}
.note{color:var(--dim);font-size:12.5px;margin-top:12px;padding-top:12px;
border-top:1px solid var(--line)}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}.dim{color:var(--dim)}
.pill{display:inline-block;padding:1px 8px;border-radius:20px;font-size:12px;
background:rgba(96,165,250,.12);color:var(--acc)}
.pill.ok{background:rgba(74,222,128,.12);color:var(--ok)}
.pill.warn{background:rgba(251,191,36,.12);color:var(--warn)}
.pill.bad{background:rgba(248,113,113,.12);color:var(--bad)}
code{background:rgba(255,255,255,.05);padding:1px 5px;border-radius:4px;font-size:12.5px}
.heat{display:flex;flex-wrap:wrap;gap:2px;margin-top:10px}
.heat span{width:26px;height:20px;border-radius:3px;font-size:10px;display:flex;
align-items:center;justify-content:center;color:#0b0d11}
"""


def render(blocks: list[Block], *, generated: datetime) -> str:
    parts: list[str] = []
    for b in blocks:
        cls = "card wide" if (b.table and len(b.table) > 6) or b.headers else "card"
        inner = [f"<h2>{esc(b.title)}</h2>"]

        if b.rows:
            items = "".join(
                f"<dt>{esc(k)}</dt><dd>{_cell(v)}</dd>" for k, v in b.rows
            )
            inner.append(f"<dl>{items}</dl>")

        if b.table is not None:
            if b.table:
                head = (
                    "<tr>" + "".join(f"<th>{esc(h)}</th>" for h in (b.headers or [])) + "</tr>"
                    if b.headers
                    else ""
                )
                body = "".join(
                    "<tr>"
                    + "".join(f"<td>{_cell(c)}</td>" for c in row)
                    + "</tr>"
                    for row in b.table
                )
                inner.append(f"<table>{head}{body}</table>")
            else:
                inner.append(f'<p class="dim">{UNKNOWN}</p>')

        if b.note:
            body = b.note if isinstance(b.note, Raw) else esc(b.note)
            inner.append(f'<div class="note">{body}</div>')

        parts.append(f'<section class="{cls}">' + "".join(inner) + "</section>")

    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        "<meta name=viewport content='width=device-width,initial-scale=1'>"
        "<title>Project status</title>"
        f"<style>{_CSS}</style></head><body><div class=wrap>"
        "<h1>Multi-agent trading system · Bybit USDT Perpetual</h1>"
        f'<div class="sub">Snapshot at {generated:%d.%m.%Y %H:%M} UTC · '
        "deposit $40 · data read from the DB at run time</div>"
        f'<div class="grid">{"".join(parts)}</div>'
        "</div></body></html>"
    )
