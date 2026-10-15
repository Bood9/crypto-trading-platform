"""Validation on preregistered windows. DEC-028, DEC-030, DEC-034.

The plan is written before the data exists. Here it turns from an
agreement into a construct: four prohibitions that cannot be bypassed
by forgetfulness:

1. **Trading is frozen.** The config sections and code files that define
   the setup's trading are compared with their state at the frozen
   commit. Any difference is a refusal.

2. **Order.** A window is run only after all previous windows of the
   plan's sequence have passed. A failed window closes all subsequent
   ones forever.

3. **Once per setup.** Every window of every setup is run once; the
   result is written to a ledger under git control.

4. **Out-of-sample is spent once for ALL setups** (DEC-034). Looking at
   an out-of-sample period turns it into in-sample for any subsequent
   hypothesis. An `out_of_sample` window that overlaps an already spent
   out-of-sample window of any setup is rejected.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml

#: Common config sections that define the trading of any setup. The
#: setup's own section is added in `frozen_sections`.
COMMON_SECTIONS: tuple[tuple[str, ...], ...] = (
    ("strategy", "management"),
    ("strategy", "min_target_r"),
    ("strategy", "min_move_pct"),
    ("risk",),
    ("costs",),
)

#: Common trading code. The setup's own file is added in `frozen_code`.
COMMON_CODE: tuple[str, ...] = (
    "src/backtest/engine.py",
    "src/backtest/runner.py",
    "src/backtest/portfolio.py",
    "src/backtest/accounting.py",
    "src/backtest/indicators.py",
    "src/backtest/resample.py",
    "src/risk",
)

LEDGER = Path("config/validation_ledger.json")


def frozen_sections(setup: str) -> tuple[tuple[str, ...], ...]:
    return (("strategy", setup), *COMMON_SECTIONS)


def frozen_code(setup: str, extra: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Common trading code, the setup's own file and the code holding its logic.

    Other setups' files are not included: a fix in the rejected
    Setup A does not change hypothesis D and must not block its test.
    The exception is `extra`: a setup that reuses another setup's rule
    freezes that file too.
    """
    letter = setup.removeprefix("setup_")
    return (*COMMON_CODE, f"src/strategy/setup_{letter}.py", *extra)


#: DEC-030 compatibility: what was frozen for Setup C.
FROZEN_SECTIONS = frozen_sections("setup_c")
FROZEN_CODE = frozen_code("setup_c")


def _dig(tree: Any, path: tuple[str, ...]) -> Any:
    for key in path:
        if not isinstance(tree, dict) or key not in tree:
            return None
        tree = tree[key]
    return tree


def config_at(commit: str, *, root: Path = Path(".")) -> dict:
    """The config as it was at the commit."""
    raw = subprocess.run(
        ["git", "show", f"{commit}:config/settings.yaml"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if raw.returncode != 0:
        raise ValueError(
            f"commit {commit} not found or it has no config/settings.yaml: "
            f"{raw.stderr.strip()}"
        )
    return yaml.safe_load(raw.stdout)


def frozen_drift(frozen: dict, current: dict, setup: str = "setup_c") -> list[str]:
    """Config sections that differ from the frozen state."""
    return [
        ".".join(path)
        for path in frozen_sections(setup)
        if _dig(frozen, path) != _dig(current, path)
    ]


def code_drift(
    commit: str,
    *,
    setup: str = "setup_c",
    extra: tuple[str, ...] = (),
    root: Path = Path("."),
) -> list[str]:
    """Trading code files changed since the frozen commit.

    The working tree is compared, not just commits: an uncommitted
    edit changes the run just the same. Failing to compare is an error,
    not "matched" (P-001).
    """
    diff = subprocess.run(
        ["git", "diff", "--name-only", commit, "--", *frozen_code(setup, extra)],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if diff.returncode != 0:
        raise ValueError(f"could not compare the code with {commit}: {diff.stderr.strip()}")
    return [line for line in diff.stdout.splitlines() if line.strip()]


@dataclass
class Ledger:
    """Ledger of validation runs."""

    entries: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path = LEDGER) -> Ledger:
        if not path.exists():
            return cls()
        return cls(entries=json.loads(path.read_text(encoding="utf-8")))

    def save(self, path: Path = LEDGER) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.entries, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8",
        )

    def entries_of(self, setup: str) -> list[dict]:
        # Records before DEC-034 had no "setup" field: it was only C.
        return [e for e in self.entries if e.get("setup", "setup_c") == setup]

    def find(self, period: str, commit: str, setup: str = "setup_c") -> dict | None:
        for entry in self.entries_of(setup):
            if entry["period"] == period and entry["commit"] == commit:
                return entry
        return None

    def spent_out_of_sample(self) -> list[tuple[str, str, str]]:
        """All spent out-of-sample windows: (setup, start, end).

        Records before DEC-034 had no window kind: they were Setup C's
        test windows, that is, out-of-sample.
        """
        out = []
        for entry in self.entries:
            if entry.get("kind", "out_of_sample") == "out_of_sample":
                start, end = entry["window"]
                out.append((entry.get("setup", "setup_c"), start, end))
        return out


#: The DEC-030 sequence, for calls without a plan.
_LEGACY_SEQUENCE = ("deep_history", "recent_oos")


def refusal(
    period: str,
    commit: str,
    ledger: Ledger,
    *,
    setup: str = "setup_c",
    plan: Any = None,
) -> str | None:
    """Why the run is forbidden. None means allowed.

    `plan` is a ValidationPlan; the type is not annotated so as not to pull
    the config into a module that the config itself imports.
    """
    if plan is None:
        sequence = list(_LEGACY_SEQUENCE)
        kinds = dict.fromkeys(sequence, "out_of_sample")
        bounds: dict[str, tuple[str, str]] = {}
    else:
        sequence = list(plan.sequence)
        kinds = {name: w.kind for name, w in plan.windows.items()}
        bounds = {name: (w.start, w.end) for name, w in plan.windows.items()}

    if period not in sequence:
        return f"unknown window {period!r}"
    if ledger.find(period, commit, setup) is not None:
        return (
            f"window {period} was already run on configuration {commit}. "
            "Each window is spent once"
        )

    for earlier in sequence[: sequence.index(period)]:
        entry = ledger.find(earlier, commit, setup)
        if entry is None:
            if earlier == "deep_history":
                return "the recent OOS cannot be spent until the deep history has passed"
            return f"window {period} cannot be run until window {earlier} has passed"
        if not entry["passed"]:
            if earlier == "deep_history":
                return (
                    "the deep history failed: the setup is rejected, the recent OOS "
                    "is kept for the next setup (DEC-028, rule 2)"
                )
            return (
                f"window {earlier} failed: the setup is rejected, window {period} "
                "is kept for the next setup"
            )

    if kinds.get(period) == "out_of_sample" and period in bounds:
        start, end = bounds[period]
        for other, s, e in ledger.spent_out_of_sample():
            if s < end and start < e:
                return (
                    f"period {start} — {end} was already out-of-sample for {other} "
                    f"({s} — {e}). Looking at an out-of-sample period turns it "
                    "into in-sample for any subsequent hypothesis (DEC-034)"
                )
    return None


def record(
    ledger: Ledger,
    *,
    period: str,
    commit: str,
    window: tuple[str, str],
    passed: bool,
    thresholds: list[dict],
    extra: dict,
    setup: str = "setup_c",
    kind: str = "out_of_sample",
) -> dict:
    entry = {
        "setup": setup,
        "kind": kind,
        "period": period,
        "commit": commit,
        "window": list(window),
        "run_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "passed": passed,
        "thresholds": thresholds,
        **extra,
    }
    ledger.entries.append(entry)
    return entry


def decimal_or_none(value: Decimal | float | None) -> float | None:
    return None if value is None else float(value)


__all__ = [
    "COMMON_CODE",
    "COMMON_SECTIONS",
    "FROZEN_CODE",
    "FROZEN_SECTIONS",
    "LEDGER",
    "Ledger",
    "code_drift",
    "config_at",
    "decimal_or_none",
    "frozen_code",
    "frozen_drift",
    "frozen_sections",
    "record",
    "refusal",
]
