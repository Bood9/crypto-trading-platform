"""Bot platform conductor (DEC-049).

    python -m src.bots night            # night shift: the day's decision
    python -m src.bots day              # day shift: stops on open positions
    python -m src.bots night --dry      # same without execution (preview)
    python -m src.bots role risk --shift night --now 2026-09-30T00:05:00+00:00

A shift is the roles in turn, each in its own process: a hung role can be
killed on timeout, a crashed one does not bring down the others, and a
memory leak lives no longer than one role. Work is passed via the board in the DB.

One "now" for the whole shift: the Risk Manager and the Executor decide at
the same moment, otherwise paper has different prices and the second key fails.

Exit code — as in trade-cycle (0, 1, 3, 4): the schedule
(scripts/scheduled.sh) does not change.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from src.bots.roles import EXIT_FAILED, SHIFTS, RoleResult, shift_code

TITLES = {
    "collector": "Collector",
    "analyst": "Analyst",
    "risk": "Risk Manager",
    "executor": "Executor",
    "controller": "Controller",
}

#: Limit per role, seconds. The Collector — with headroom for building the
#: month's universe; the Executor — with headroom for a slow venue. An Executor
#: killed on timeout is like a crashed one: the trade record hits disk before
#: the order, and the paper venue is saved after every order.
TIMEOUTS = {"collector": 3600, "analyst": 900, "risk": 900, "executor": 1800,
            "controller": 900}

LOGS = Path("var/live")


def _stamp() -> str:
    return f"{datetime.now(UTC):%H:%M:%S}"


def _abandon(name: str, args: argparse.Namespace, now: datetime, reason: str) -> None:
    """A run without a result — goes to the board as a failure, so it does not hang "running"."""
    from src.bots.board import DbBoard
    from src.core.db import dispose_engine
    from src.live.signals import decision_moment

    setup = {"d": "setup_d", "e": "setup_e"}[args.setup]

    async def run() -> None:
        try:
            await DbBoard().abandon(name, f"{setup}:{args.venue}", decision_moment(now),
                                    args.shift, reason)
        finally:
            await dispose_engine()

    try:
        asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 — board unavailable: the result is in the log anyway
        print(f"!!! could not mark run {name} on the board: {exc}", flush=True)


def _kill(proc: subprocess.Popen) -> None:
    """A role — with all its processes: a Collector killed on timeout must not
    leave data collection writing to the DB while the next roles run."""
    with contextlib.suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGKILL)
    proc.wait()


def _role_process(name: str, args: argparse.Namespace, now: datetime) -> RoleResult:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "result.json"
        cmd = [sys.executable, "-m", "src.bots", "role", name, "--shift", args.shift,
               "--now", now.isoformat(), "--setup", args.setup, "--venue", args.venue,
               "--result", str(out)]
        if args.dry:
            cmd.append("--dry")
        # Own process group: killing the role means killing its children too.
        proc = subprocess.Popen(cmd, process_group=0)
        try:
            code = proc.wait(timeout=TIMEOUTS[name])
        except subprocess.TimeoutExpired:
            _kill(proc)
            reason = f"killed on timeout {TIMEOUTS[name]} s"
            _abandon(name, args, now, reason)
            return RoleResult(status="failed", summary=reason, exit_code=EXIT_FAILED)
        except BaseException:
            _kill(proc)  # the conductor was stopped (SIGTERM, Ctrl-C) — do not abandon the role
            raise
        if not out.exists():
            reason = f"role process crashed (code {code})"
            _abandon(name, args, now, reason)
            return RoleResult(status="failed", summary=reason, exit_code=EXIT_FAILED)
        raw = json.loads(out.read_text())
        return RoleResult(status=raw["status"], summary=raw["summary"],
                          exit_code=raw["exit_code"])


def _stop(signum: int, _frame) -> None:  # noqa: ANN001
    raise SystemExit(128 + signum)


def shift(args: argparse.Namespace) -> int:
    # SIGTERM (launchd, kill) — like Ctrl-C: kill the current role and exit.
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGHUP, _stop)
    now = datetime.fromisoformat(args.now) if args.now else datetime.now(UTC)
    print(f"=== {_stamp()} shift {args.shift}: setup {args.setup}, venue {args.venue}, "
          f"now {now:%Y-%m-%d %H:%M:%S} UTC{' · PREVIEW' if args.dry else ''}", flush=True)
    results: dict[str, RoleResult] = {}
    for name in SHIFTS[args.shift]:
        result = _role_process(name, args, now)
        results[name] = result
        print(f"=== {_stamp()} {TITLES[name]}: {result.status} — {result.summary}", flush=True)
    code = shift_code(args.shift, results)
    watch = results.get("controller")
    # Notify only for a real shift: a manual preview or a run at a given
    # moment must not use up the schedule's daily notification.
    scheduled = not args.dry and not args.now
    alerts = []
    if watch is not None and watch.status == "failed":
        # The Controller is in shadow: it does not change the shift code, but a human learns.
        alerts.append(("controller", "Controller", watch.summary))
    collected = results.get("collector")
    if args.shift == "night" and collected is not None and collected.status == "failed":
        # A Collector failure does not fail the night (exits and reconciliation do
        # not depend on data), but a failed build of the month's universe would
        # otherwise stay silent until the next night (night audit 2026-09-30).
        alerts.append(("collector", "Collector", collected.summary))
    for key, title, summary in alerts:
        print(f"!!! {title}: {summary}", flush=True)
        if scheduled:
            LOGS.mkdir(parents=True, exist_ok=True)
            (LOGS / f"{key}_failed_{now:%Y%m%d}").write_text(summary + "\n")
    print(f"=== {_stamp()} shift {args.shift}: code {code}", flush=True)
    return code


def role(args: argparse.Namespace) -> int:
    from src.bots.board import DbBoard
    from src.bots.roles import RoleContext, run_role
    from src.core.config import get_env, get_settings
    from src.core.db import dispose_engine
    from src.core.logging import configure_logging

    env = get_env()
    configure_logging(env.log_level, env.log_format)
    setup = {"d": "setup_d", "e": "setup_e"}[args.setup]
    ctx = RoleContext(settings=get_settings(), board=DbBoard(), setup=setup,
                      venue=args.venue, shift=args.shift,
                      now=datetime.fromisoformat(args.now), execute=not args.dry)

    async def run() -> RoleResult:
        try:
            return await run_role(args.name, ctx)
        finally:
            await dispose_engine()

    result = asyncio.run(run())
    if args.result:
        Path(args.result).write_text(json.dumps({
            "status": result.status, "summary": result.summary,
            "exit_code": result.exit_code}, ensure_ascii=False))
    return result.exit_code


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.bots", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--setup", default="e", choices=["d", "e"])
        p.add_argument("--venue", default="paper", choices=["paper", "testnet"])
        p.add_argument("--dry", action="store_true", help="without execution")
        p.add_argument("--now", default=None, help="shift moment, ISO (for checks)")

    for name in SHIFTS:
        p = sub.add_parser(name, help=f"shift {name}")
        common(p)
        p.set_defaults(shift=name, func=shift)
    r = sub.add_parser("role", help="a single role (called by the conductor)")
    r.add_argument("name", choices=sorted(TITLES))
    r.add_argument("--shift", required=True, choices=sorted(SHIFTS))
    r.add_argument("--result", default=None)
    common(r)
    r.set_defaults(func=role)
    args = parser.parse_args(argv)
    if args.command == "role" and not args.now:
        parser.error('a role needs --now: one "now" for the whole shift')
    return args.func(args)


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    sys.exit(main())
