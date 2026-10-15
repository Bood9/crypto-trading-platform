"""trade-cycle exit code without a universe is a failure, so that the night script
retries (and the month build) on the same day."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from typer.testing import CliRunner

import src.live.report as report_mod
import src.live.runner as runner
from src import cli
from src.live.cycle import CycleReport
from src.live.runner import Prepared
from src.risk.governor import TradingMode


def fake_run(problem: str | None, halted: bool = False):
    async def run_daily(*_a, **_k):
        moment = datetime(2026, 10, 2, tzinfo=UTC)
        rep = CycleReport(moment=moment, dry_run=False, halted=halted)
        rep.equity, rep.mode = Decimal(100), TradingMode.ON
        return Prepared(moment, [], [], problem=problem), rep, []
    return run_daily


def invoke(monkeypatch, tmp_path, problem, halted=False):
    monkeypatch.setattr(runner, "run_daily", fake_run(problem, halted))
    monkeypatch.setattr(report_mod, "JOURNAL", tmp_path / "journal.md")
    orig = report_mod.append
    monkeypatch.setattr(report_mod, "append", lambda text, path=None: orig(text, tmp_path / "j.md"))
    return CliRunner().invoke(cli.app, ["trade-cycle", "--venue", "paper", "--execute"])


def test_missing_universe_exits_failed_so_it_is_retried(monkeypatch, tmp_path):
    result = invoke(monkeypatch, tmp_path, "no universe for 2026-10: no new entries.")
    assert result.exit_code == cli.EXIT_FAILED, result.output
    assert "no universe for 2026-10" in (tmp_path / "j.md").read_text()


def test_halt_wins_over_missing_universe(monkeypatch, tmp_path):
    result = invoke(monkeypatch, tmp_path, "no universe", halted=True)
    assert result.exit_code == cli.EXIT_HALTED


def test_normal_day_exits_zero(monkeypatch, tmp_path):
    assert invoke(monkeypatch, tmp_path, None).exit_code == 0
