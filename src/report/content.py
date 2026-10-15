"""Content blocks of the report: phase, quality, decisions, guarantees.

The texts live here, not in the template: a human reads them half a year later,
and they matter more than the layout. The numbers come from the DB rather than
being typed by hand — otherwise the report diverges from reality on the very first run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import func, select

from src.collectors.validation import validate_ohlcv
from src.core.config import PROJECT_ROOT, get_settings
from src.core.db import Ohlcv, session_scope
from src.report.status import UNKNOWN, Block, Raw, esc


@dataclass(frozen=True)
class Guard:
    """A mechanical guarantee of a principle.

    A rule without a guard is a note. The difference is visible only when
    it is written out, so a missing guard is stated explicitly.
    """

    principle: str
    what: str
    guard: str | None
    where: str | None

    @property
    def enforced(self) -> bool:
        return self.guard is not None


GUARDS = [
    Guard(
        "P-001",
        "Unknown is rejected",
        "Closed enumerations that forbid default values; "
        "a missing value is not replaced with zero",
        "src/core/models.py::RejectionCode, tests for every code",
    ),
    Guard(
        "P-002",
        "A check counts only by its exit code",
        "pre-commit hook: a commit physically does not go through on a non-zero code",
        ".githooks/pre-commit → scripts/check.sh",
    ),
    Guard(
        "P-003",
        "Correlation of a new setup with accepted ones is below 0.3",
        None,
        None,
    ),
    Guard(
        "P-004",
        "Regularity in the data = a collection defect",
        "Coefficient of variation of intervals between gaps; CV < 0.25 → "
        "a warning in the validation report",
        "src/collectors/validation.py::ValidationReport.regularity",
    ),
    Guard(
        "P-005",
        "Target and time stop are compatible",
        "A run with target hit rate below 10% is aborted by a "
        "RunInvalid exception before statistics are computed",
        "src/backtest/report.py::assert_run_is_valid",
    ),
    Guard(
        "P-006",
        "The target pays for the stop and costs",
        "A run with a median distance to target below min_target_r or "
        "min_move_pct is aborted by a RunInvalid exception",
        "src/backtest/report.py::assert_target_is_payable",
    ),
    Guard(
        "P-007",
        "Risk per trade and loss limits are compatible",
        "The config does not load if eight losses in a row disable "
        "the week or two losses disable the day",
        "src/core/config.py::RiskConfig._risk_and_limits_are_compatible",
    ),
    Guard(
        "DEC-004",
        "Page window arithmetic lives in one place",
        "A test reads the sources of all collectors and fails if the boundary "
        "is computed in place",
        "tests/test_collectors.py::test_all_collectors_use_the_shared_window_function",
    ),
    Guard(
        "Acceptance thresholds",
        "Do not change after results appear",
        "A test that the values are unchanged",
        "tests/test_config.py::test_acceptance_thresholds_are_unchanged_by_walk_forward",
    ),
    Guard(
        "DEC-015",
        "The universe composition is reproducible",
        "Tradability is computed at the selection month's price, the period is tied "
        "to the data, not to now(); a rebuild gives the same composition",
        "src/strategy/universe_builder.py::tradable_in_month",
    ),
    Guard(
        "DEC-007/009/012/015",
        "Every universe composition knob has a sweep",
        "A test requires a sensitivity list for each; the config "
        "rejects a base value outside the sweep",
        "tests/test_config.py::test_three_universe_knobs_all_have_sweeps",
    ),
    Guard(
        "Lookahead",
        "The strategy does not see future bars",
        "Guaranteed by construction: the strategy gets the slice bars[:i+1], "
        "accessing further is an IndexError",
        "src/backtest/engine.py::run, tests/test_lookahead.py",
    ),
]


def guards_block() -> Block:
    rows = []
    for g in GUARDS:
        if g.enforced:
            mark = Raw('<span class="pill ok">yes</span>')
            where = Raw(f"<code>{esc(g.where)}</code>")
            what = g.guard
        else:
            mark = Raw('<span class="pill warn">no</span>')
            where = Raw('<span class="dim">—</span>')
            what = Raw(
                '<span class="warn">no guard</span> '
                '<span class="dim">— the check will appear in Phase 2, '
                "for now this is a note, not a rule</span>"
            )
        rows.append(
            [
                Raw(f"<b>{esc(g.principle)}</b><br><span class=dim>{esc(g.what)}</span>"),
                mark,
                what,
                where,
            ]
        )

    enforced = sum(1 for g in GUARDS if g.enforced)
    return Block(
        title="Mechanical guarantees",
        headers=["principle", "guard", "how exactly", "where it lives"],
        table=rows,
        note=(
            f"Enforced {enforced} of {len(GUARDS)}. "
            "A rule without a guard is a note, and telling one from the other "
            "matters more than having a long list of rules. Three of the four "
            "principles were born the same way: from a bug that went "
            "unnoticed because it looked normal."
        ),
    )


def decisions_block() -> Block:
    """List of DEC-XXX from DECISIONS.md — headings, not a retelling."""
    path = PROJECT_ROOT / "DECISIONS.md"
    if not path.exists():
        return Block(title="Decisions", table=[], note=UNKNOWN)

    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^## (DEC-\d+) — (\S+) — (.+)$", line.strip())
        if m:
            rows.append([m.group(1), m.group(2), m.group(3)])
    rows.sort(key=lambda r: r[0])

    return Block(
        title="Architecture decisions",
        headers=["number", "date", "essence"],
        table=rows,
        note=Raw("Full text with options and rationale — in <code>DECISIONS.md</code>."),
    )


def limitations_block() -> Block:
    """Known backtest limitations from PROGRESS.md."""
    path = PROJECT_ROOT / "PROGRESS.md"
    if not path.exists():
        return Block(title="Backtest limitations", table=[], note=UNKNOWN)

    text_all = path.read_text(encoding="utf-8")
    marker = "## Known backtest limitations"
    if marker not in text_all:
        return Block(title="Backtest limitations", table=[], note=UNKNOWN)

    section = text_all.split(marker, 1)[1]
    rows = [
        [m.group(1), m.group(2)]
        for m in re.finditer(r"^### (\d+)\.\s+(.+)$", section, re.MULTILINE)
    ]
    return Block(
        title="Known backtest limitations",
        headers=["#", "limitation"],
        table=rows,
        note=(
            "Read before interpreting any results. Limitations 8 and 9 "
            "act in the same direction — both make out-of-sample cheaper."
        ),
    )


async def quality_block() -> Block:
    """Data quality: gaps, P-004, bars without trades."""
    settings = get_settings()
    symbols = await _universe_symbols()

    rows: list[tuple[str, object]] = []
    table: list[list[object]] = []

    for tf in ("15m", "1h", "4h"):
        reports = [await validate_ohlcv(s, tf) for s in symbols]
        present = [r for r in reports if r.rows]
        gaps = sum(r.missing_bars for r in present)
        longest = max((r.longest_gap for r in present), default=0)
        with_gaps = sum(1 for r in present if r.gaps)
        table.append(
            [
                tf,
                len(present),
                Raw('<span class="ok">0</span>')
                if gaps == 0
                else Raw(f'<span class="bad">{gaps}</span>'),
                with_gaps,
                longest,
            ]
        )

    async with session_scope() as s:
        zero = (
            await s.execute(
                select(func.count(), func.count(func.distinct(Ohlcv.symbol)))
                .where(Ohlcv.timeframe == "15m", Ohlcv.volume == 0, Ohlcv.turnover == 0)
            )
        ).one()
        total_15m = (
            await s.execute(select(func.count()).where(Ohlcv.timeframe == "15m"))
        ).scalar_one()

    share = zero[0] / total_15m * 100 if total_15m else 0
    rows.append(
        (
            "Bars without trades (15m)",
            f'{zero[0]:,} across {zero[1]} symbols · {share:.3f}%'.replace(",", " "),
        )
    )
    rows.append(("Stream density threshold", f"{settings.data.min_stream_coverage:.0%}"))

    return Block(
        title="Data quality",
        headers=["timeframe", "symbols", "bars missing", "with gaps", "max gap"],
        table=table,
        rows=rows,
        note=Raw(
            "<b>Bars without trades are NOT gaps.</b> The row exists, "
            "there was just no trading in it: thin pairs during night hours. "
            "In Phase 2 such a bar gives a zero true range and enters the ATR computation — "
            "not an error, but worth knowing.<br><br>"
            "P-004: there are no gaps at all, the regularity check is silent. "
            "Before the page boundary bug was fixed it found CV = 0.000 "
            "and a period of 1001 bars for all 115 symbols."
        ),
    )


async def _universe_symbols() -> list[str]:
    from src.core.db import UniverseSnapshot

    async with session_scope() as s:
        rows = (
            await s.execute(
                select(UniverseSnapshot.symbol)
                .where(UniverseSnapshot.included.is_(True))
                .distinct()
            )
        ).scalars().all()
    return sorted(rows)


def phase_block(tests_passed: int | None, tests_at: str | None) -> Block:
    return Block(
        title="Phase and progress",
        rows=[
            (
                "Phase 1a — infrastructure and Instrument Filter",
                Raw('<span class="pill ok">closed</span>'),
            ),
            ("Phase 1b — data, universe, validation", Raw('<span class="pill ok">closed</span>')),
            ("Phase 2 — backtest", Raw('<span class="pill">next</span>')),
            ("Phases 3–7", Raw('<span class="dim">not started</span>')),
            ("Tests passing", tests_passed if tests_passed is not None else UNKNOWN),
            ("Test run", tests_at or UNKNOWN),
        ],
        note=(
            "The Phase 2 order is fixed: first Setup B (the data is clean, "
            "no ambiguity — an honest test of the engine), then Setup A."
        ),
    )


def readiness_block(readiness: dict[str, object]) -> Block:
    rows = list(readiness.items())
    ready = readiness.get("ready", 0)
    total = readiness.get("Total in universe", 0)
    complete = ready == total and total > 0

    note = (
        "Streams kline_1h, kline_4h, oi, funding are required simultaneously. "
        "A symbol with full candles but missing OI drops out entirely — "
        "the MULTIUSDT case.<br><br>"
    )
    if complete:
        note += (
            '<span class="ok">No dropouts.</span> '
            "The only candidate for dropping out, <code>XPLUSDT</code>, "
            "turned out to be a gap in our collection, not a fact about the data: "
            "the exchange has OI for it, the run was interrupted exactly on that "
            "stream. 8,420 points were backfilled — the symbol is back in the set."
        )
    else:
        note += (
            '<span class="warn">There are dropped symbols.</span> '
            "Check whether the stream is missing on the exchange side or it is a "
            "collection gap: the latter is cured by a targeted backfill."
        )
    return Block(title="Setup B readiness", rows=rows, note=Raw(note))


def read_test_marker() -> tuple[int | None, str | None]:
    """Test count and run time from the marker file.

    Written by the check script. If there is no marker, the report says
    "not computed" instead of making up a number.
    """
    marker = PROJECT_ROOT / "var" / "last-check.txt"
    if not marker.exists():
        return None, None
    try:
        raw = marker.read_text(encoding="utf-8").strip().split("|")
        return int(raw[0]), raw[1]
    except (ValueError, IndexError):
        return None, None


__all__ = [
    "GUARDS",
    "Guard",
    "decisions_block",
    "guards_block",
    "limitations_block",
    "phase_block",
    "quality_block",
    "read_test_marker",
    "readiness_block",
]
