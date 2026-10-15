# DECISIONS

Architecture decision log. Format: date, question, options, choice, rationale.

New entries are added at the top.

---

## Cross-cutting principles

They apply in all modules and need no re-justification in individual
decisions. A deviation from a principle is recorded as a separate DEC entry.

### P-001 — The unknown is rejected

**Wherever data may not match expectations, the default behaviour
is to reject and log. Never "let it through
and hope".**

Scope: the whole system, not just instrument selection:

| situation | behaviour |
|---|---|
| unfamiliar `symbolType` | reject, `UNKNOWN_INSTRUMENT_TYPE` |
| MMR missing from the API | reject, `MISSING_MMR` |
| gap in the data stream for a month | symbol excluded, `MISSING_STREAM` |
| symbol younger than the threshold | reject, `TOO_YOUNG` |
| LLM response failed validation | the agent is treated as having stayed silent |
| reason code outside the enumeration | error, not free text |
| state diverges from the exchange | the exchange is the source of truth, mode OFF |

**Why.** Asymmetry of consequences. A missed trade costs nothing; a
wrongly opened one costs real money. The system trades real money
unattended, and any "probably good enough" will sooner or later play out
in the worst possible way.

The second consideration is debugging. A rejection with a reason code lands in the DB
and in the decision chain on the logs panel. A silent skip leaves no
trace, and later it is impossible to tell why the bot opened a position it
should not have opened.

**Practical consequence.** Enumerations are closed, defaults are
restrictive, a missing field is not replaced with zero. A new class of data
from the exchange breaks the filter loudly — that is the desired behaviour, not a problem.

It started as a specific decision about `symbolType` (DEC-005) after
a permissive-by-default filter let 175 tokenized stocks
into the trading universe.

---

### P-002 — A check counts only by its exit code

**No check is considered passed based on its output text. Only
by its exit code.**

The rule appeared after three identical cases in a row, in three different
tools:

| tool | how it misled | fix |
|---|---|---|
| shell pipe | `pytest \| tail` returned 0 with failing tests | `set -euo pipefail` |
| ruff | "Found 19 errors" scrolled off into the tail of the output, read as success | exit code in `check.sh` |
| psql | `-f` without `ON_ERROR_STOP` continued after an error and printed "SUCCESS" | `ON_ERROR_STOP=1` |

In all three the tool honestly reported the error; what misled was the way
the result was read. So the thing to fix is not the case but the way of reading.

**Obligations when adding any new tool** — fulfilled
on the very first use, not after the first error:

* shell scripts: `set -euo pipefail`;
* `psql`: `ON_ERROR_STOP=1` in every call, including one-off ones;
* any external command: the exit code is checked explicitly, and the output
  is used only for diagnostics;
* pipelines: the last command does not determine the result — either
  `pipefail` or parsing `PIPESTATUS`/`pipestatus` (in zsh the array
  has a different name and is 1-indexed);
* self-check: before trusting a new check, break it on purpose
  once and make sure it turns red.

**A barrier, not a reminder.** The rule was broken right after it was
written: `check.sh` honestly printed "THERE ARE ERRORS" and returned 1,
but the output was eyeballed from the tail, where the tests were green, and the commit
went through. So `.githooks/pre-commit` runs the check —
a commit physically cannot go through with a non-zero code. A deliberate bypass
remains possible via `--no-verify`.

The lesson from this case: a rule that relies on attentiveness is not a rule.
Only what cannot be bypassed by forgetfulness works.

**Why this matters more than it seems.** A false-positive check is worse than
a missing one: a missing one is visible, while a false one creates confidence.
In a system that trades unattended, "tests are green" must
mean exactly what it says.

Related to P-001: there the unknown is rejected, here the unconfirmed
is not counted as confirmed. The same principle from different sides.

---

### P-003 — A new setup is accepted only with low correlation to existing ones

**A setup counts as an independent source of signals only if
the correlation of its returns with each already accepted setup is below 0.3.**

The threshold is initial and will be refined on the first data.

**Why this is an acceptance criterion, not a wish.** Three setups
firing on similar conditions give not triple the trade frequency
but one bet of triple size. This is exactly the error that
position correlation control (spec §5) guards against, only at the
logic level, where it is harder to notice: each setup on its own looks
independent.

**How it is checked.** Per-trade return series are brought onto a common
time grid, and pairwise correlation is computed over the common period.
The check is mandatory before the setup goes live, not after.

**Why it is fixed in advance.** When a setup falls short on statistics,
there is a temptation to add a variation of it and count the combined trades.
The right answer is an independent source of signals; the wrong one is
diluting the criterion.

---

### P-004 — Regularity where randomness is expected is a collection defect

**If a quantity that should be random turns out to be suspiciously
even, the cause is in our code, not in the nature of the data.**

The rule appeared after the candle collector had for half a year been losing exactly
one bar out of every thousand. The only visible trace was that the
median and the 95th percentile of gaps coincided: 2.9 and 2.9. Random gaps
never look like that.

Further checking showed that the gaps are **synchronous across symbols**
(71 symbols lost the same bar) and spaced at a constant step
of 1001 bars — exactly the page size plus one. Cause: the window
`[cursor, cursor + N*interval]` spans N+1 slots because both
bounds are inclusive.

**Why this is a separate principle.** Neither row counts, nor checkpoints, nor
duplicate checks showed anything: the series looked complete. The
defect could only be detected from the shape of the distribution, not from the volume of data.

**Implementation.** `ValidationReport.regularity()` computes the coefficient
of variation of the intervals between gaps. A CV below 0.25 flags a symbol as
suspicious, and a warning is printed in the validation report.

The thresholds are grounded in fact: the bug found had CV = 0.000 (intervals
matched to the bar), market gaps have a CV close to one. 0.25
leaves headroom for a partially regular pattern. Fewer than four gaps
do not give enough intervals, and no judgement is made (P-001:
the unknown is not passed off as known).

**The scope is wider than candles.** The same sign will fire on funding,
OI, liquidations, order book snapshots and generally on any stream collected
in pages. Check for it when connecting each new data
source, not only where we have already been burned.

**Link to P-002.** There a check is not counted as passed by its output text,
here data is not counted as complete by its row count. One principle
from different sides: volume does not prove correctness.

---

### P-005 — Target and time stop must be compatible

**Before running any setup, the share of trades closed
by reaching the target is computed. Below 10%, the run is invalid: it is not
the hypothesis that is being measured.**

Setup B gave 1.1% target hits and 90.2% time-stop exits.
This means that instead of "trend continuation to 3R" what was measured was
a random 48-hour hold, whose mean result is statistically
indistinguishable from zero.

**Why this is a guard, not a note.** The result looked complete:
1494 trades, a 47% win rate, all five report blocks computed. Nothing
in the final numbers said that the wrong hypothesis was being tested.
The defect could only be found by breaking down the exit reasons —
a quantity that was not in the report.

**The 10% threshold is grounded in asymmetry.** With a 3R target the breakeven win rate
is 25%. If the target is reached less than once in ten trades,
the contribution of target exits to E is smaller than that of the time-stop noise, and the result
describes the holding window rather than the setup.

**What to do when it fires.** Do not tune the target for reachability
on the same data — that is curve fitting. Rebuild the setup so that the holding
window matches the claimed move, and treat it as a new
hypothesis with a new sample.

**The minimum sample for a verdict is 30 trades.** Below it the guard
issues no verdict at all: neither "PASS" nor "FAIL", but "NOT DECIDED".
Added by DEC-016 after a sweep produced a target-hit rate
of 100%, a 100% win rate, E = +2.475R and a "PASS" verdict — on ONE
trade. A share estimated from a handful of observations says nothing
about the design: three hits out of twenty-six give an interval that
contains both 3% and 30%.

This is a direct consequence of P-002: the unconfirmed is not counted
as confirmed. The "not computed" cell and the "PASS" cell are different,
and lumping them together brings back the very reading error the
guard was introduced for.

**Link to P-001.** There the unknown is not passed off as known; here
what was measured is not passed off as what we meant to measure.

---

### P-006 — The target must not only be reached but also pay off

**Besides the target-hit rate (P-005), the distance to the target is computed
before the statistics. If the median is below `strategy.min_target_r` or below
`strategy.min_move_pct`, the run is invalid.**

P-005 catches one side of the incompatibility: the target is farther than the market moves
over the holding horizon. Nobody caught the opposite side, and it looks
far more convincing.

Measurement on Setup A with exit at the cascade VWAP: target-hit rate 70.8%,
win rate 73.9%, P-005 passed with an eightfold margin. Yet the median
distance to the target is **0.38R**, and E is **exactly zero**. The breakeven
win rate with a 0.38R target and a 1R stop is 72.5%; the actual 73.9% is
not an edge but a coincidence within noise on 23 trades.

**Such a run is not flagged by anything and reads as a success.** A high win rate,
the target is hit in three cases out of four, the guard is silent. That is exactly
what makes the error dangerous: a failed run gets examined, a successful one
does not.

**Both thresholds were already written in the config** (`min_target_r: 2.0`,
`min_move_pct: 1.6` — "round-trip cost ≈ 0.16%, target at least ten
times larger") and **were not checked by a single line of code**. This is not a new
requirement but enabling what was already written.

**Why the median and not the share of violators.** A share would need yet
another threshold — what share is acceptable — that is, one more knob. The median
sets the same thing without a new number: if half of the targets do not pay off,
the target is wrong as a design, not as an individual case.

**Link to P-005.** One principle from two sides: the target, the stop and the holding
window are not three independent parameters but one design.
They cannot be set separately, and either threshold on its own
lets half of the errors through.

---

### P-007 — Per-trade risk, loss limits and frequency must be compatible

**Eight losses in a row must not shut the system down on the weekly
limit. The check is done BEFORE the run, by arithmetic, not
by the result.**

The requirement is direct: `max_loss_per_trade_pct × 8 <= weekly_loss_limit_pct`.

**Where it came from.** The spec set a 6% risk per trade and a weekly limit
of 20%. The numbers looked cautious separately and are incompatible together:
**four losses in a row are enough to shut down the week.** With
a 38% win rate and eight trades a week, this event is almost certain
within the very first month.

The portfolio run of Setup C showed exactly that: of 1125 candidates 27 were taken,
the account went $40 → $22.11 in two months, then the weekly stop
and 1036 rejections in a row. The signal's edge (+0.089R on the population)
never reached the account at all.

**Why eight, and not four or twenty.** At any realistic
win rate for a trend setup (35–50%) and eight trades a week, a streak
of eight losses is roughly a one-percent tail. Four losses
in a row happen monthly; twenty require a risk at which
the position fails the exchange's minimum notional.

**Link to P-005 and P-006.** The same class of error for the third time: two
numbers, each reasonable on its own, set independently of each
other and incompatible together. There — the target and the holding window, the target
and costs; here — risk and the loss limit. Each time the defect
does not show up in the report: the system honestly computes and honestly prints.

**Consequence for the backtest.** The weekly stop is an absorbing state:
it is lifted only manually, and in a run there is no one to lift it. Without an explicit
resume policy, a portfolio backtest measures not return
but the time to the first bad week.

---

## DEC-050 — 2026-09-30 — Setup E paper: 4 slots instead of 2

**User request:** "let the slots expand". On the platform's first
night 19 of 22 signals got "all slots taken"; with 2 slots, 60
forward trades would take ~35 weeks to accumulate.

**User's choice — 4 slots** (`max_concurrent_positions: 4`,
`max_total_margin_pct: 87.5`) out of options 2/3/4/5, after the measurement below.

### How the options were chosen

First, arithmetic without a run: the largest N with the other
parameters unchanged (margin N × 17.5% ≤ 100% → N ≤ 5). An independent check (three
agents and skeptics for each important finding) showed this was not enough:

1. **A margin cap of exactly N × 17.5% eats the last slot.** The margin
   of held positions is computed from equity at the time of their entry; after any
   net loss the N-th slot is rejected with MAX_MARGIN (in the run, the main
   rejection code at N = 3–5). The cap is now N × 17.5% / 0.8: the slot
   holds until equity has fallen by more than the weekly limit (20%), after
   which trading is off anyway. For 5 slots that is 109% — more than 100%
   is impossible, so 100% is used for 5.
2. **The argument "all N stops in a day < the daily limit" holds almost
   nothing:** the daily limit is an entry gate, checked only at 00:10;
   it does not cap the loss within the day.
3. **The deciding threshold is the same one by which the 1.5% risk was once chosen
   (DEC-019): permutation p95 drawdown < 40% (spec §7).** It played no part
   in the arithmetic — it was measured by a portfolio run.

### Measurement (in-sample 2022-02-01 — 2026-02-07, not logged to the validation ledger)

The baseline reproduces DEC-039 to the cent ($135.14, 359 trades,
p95 21.54%). The order of simultaneous signals is set by a seed; besides the
official one (20260924), 20 others.

| slots (margin cap) | trades | 60 trades in | p95, official | p95 ≥ 40% out of 20 | median equity |
|---|---|---|---|---|---|
| 2 (35%) | 359 | ~35 wk | 21.5% | 2 | $105 |
| 3 (65.6%) | 648 | ~19 wk | 35.0% | 10 | $113 |
| **4 (87.5%)** | **845** | **~15 wk** | **36.1%** | **12** | **$118** |
| 5 (100%) | 1020 | ~12 wk | **44.3%** | 16 | $115 |

Neither the daily nor the weekly limit triggered in any run. The money
outcome barely depends on the number of slots — it is determined by which of the
simultaneous signals become trades (spread across seeds with 2
slots: $76–181). 5 slots fail the threshold already on the official order.

### What this means

* **Forward paper is now a different portfolio than in DEC-039.** The per-trade
  E estimate is not biased (the order of simultaneous signals is a fixed
  shuffle, unrelated to signal quality), and the sample
  accumulates faster. The portfolio figures of DEC-039 (359, $135.14, 21.5%)
  are labelled "with 2 slots" on the panel.
* **The p95 threshold at 4 slots passes on the official order but is worse than 40%
  in 12 of the 20 others.** Before thinking about real money, the risk per
  trade will have to be re-derived for 4 slots by the same DEC-019 rule.
* **The preregistration machinery is untouched.** The `risk` section is frozen for all
  setups; for C/D/E it already diverges (DEC-038, DEC-040), and now for F/G/H
  too — but all of them have already failed their first window, so the change
  closes no open windows. The recent OOS (2026-02-07 — 2026-09-01) is not
  touched; `validate-setup` was not run.
* **Config guards:** the "all stops on one day" check counts by N rather
  than by two; a new check — the margin cap must fit all N slots.
  Test: the fourth slot opens even after losses in previous weeks, but with
  a cap of exactly 4 × 17.5% it does not.
* The correlation filter (1 position per |corr| > 0.7) was not changed; with more
  slots there are more CORRELATION rejections (in the run 166 → 669).

---

## DEC-049 — 2026-09-30 — Bot platform: design and stage 0

**User request.** A platform of several bots: each is responsible
for its own area, checks another's work, and together they run trades
automatically every day. Clarified: bots are ROLES (separate processes);
trades only on a verified signal; on this Mac for now. Plus a window for
each bot with charts and the path of each trade through the bots on the panel.

**Design — a panel of judges.** Three independent architectures (A — a board
in Postgres, B — a Redis Streams event bus, C — a dispatcher with a checker)
and two judges (safety; a solo operator on a laptop). Result: A 37–40,
C 40, B 30–32. A synthesis was adopted:

* **Postgres is the only board**, no Redis: a second stateful store
  on a laptop that goes to sleep is an extra failure surface;
* **roles are one-shot processes**, no persistent daemon: the existing
  launchd at :05 runs them in turn; each writes its run to the board
  — what it received, what it did, what it handed on;
* **two keys for entry:** the Risk Manager, via a `try_enter` rehearsal on a
  copy of the book, issues "approved/rejected with code" for each candidate;
  the Executor enters only when its own decision and the approval match;
* **Controller** independently recomputes the data, the signals over the full
  history and the accounting; for the first 14 nights it only reports (false alarms
  must not stop trading);
* **panel:** a window for each bot, the path of each trade, and separately "rule
  rejections" and "platform rejections" — otherwise the 60-trade sample gets distorted.

**Stage 0 — fixes to running code found by the judges (verified
against the code and the real database):**

1. **Book lock** (`src/live/lock.py`, flock on a local file):
   the night cycle, the day watch and `trade-resume` do not run concurrently;
   the second one gets a rejection, and the schedule does the retry. A preview without
   `--execute` does not hold the book.
2. **Sticky mode and atomic enabling:** a regular save does not clear
   OFF_MANUAL/OFF_WEEKLY; `trade-resume` changes the mode and the baselines
   in a single transaction without touching equity (previously the row was overwritten
   whole — a race with the end of the cycle).
3. **Entry window by the live clock** for the real exchange — and checked again right
   before the order (previously by the cycle start clock).
4. **Retrying an entry after a failure** revives the removed write-ahead record instead of
   inserting a duplicate (UNIQUE entry key); the paper exchange is saved
   before the cycle and after each order — accounting does not get ahead of the exchange file.
5. `caffeinate` keeps the Mac awake for the whole script, including bringing up the database.

**Stage 1 — roles and conductor (`src/bots/`).** `python -m src.bots night|day`
runs the roles in turn as separate processes with timeouts; one
"now" for the whole shift. Exit codes are the same as for trade-cycle; `scheduled.sh`
calls the conductor, and the fallback path `daily.sh` is enabled by the file
`var/live/use_daily_sh`.

* **Board** (`bot_runs`, `decision_trace`, `order_intents`): each bot's
  run, each symbol's path through the stages signal → risk →
  execution → verify with a code and origin (rule/platform),
  the day's approvals.
* **The Risk Manager's rehearsal is a full cycle on a copy**
  (`run_daily(rehearse=True)`: the exchange copy is not saved, the book is a
  `ReadOnlyStore`), not a preview. In a preview a time exit does not change
  equity, so on a day with closes the entry size would come out different from the
  Executor's — a false RISK_MISMATCH (found during reconciliation with the cycle, test
  `test_rehearsal_sizes_after_todays_exits_like_the_executor`). On
  testnet a trial execution is impossible — there it is a preview, and only the side is reconciled.
* **Second key:** on paper, size, price and stop must match (tolerance
  1e-9 for database rounding); no approval — RISK_NOT_APPROVED. Exits
  always work without approvals.
* **Night code:** the Executor's code; but if the Analyst crashed or the Risk Manager
  gave no valid answer, the night does not count (1) — retry in an hour.
  The Controller does not change the code (shadow, 14 nights); a failure triggers a notification once
  a day.
* **Verified on real data** (a copy of the exchange with marks as of 29.09
  00:00): Analyst 22 signals out of 27; Controller over the full history —
  all 22 matched; Risk Manager and Executor independently — identical
  verdicts for all 22 (19 MAX_POSITIONS, 3 MIN_NOTIONAL); the real
  exchange file was not changed. Day shift via the conductor — 5 s, code 0.
* **Panel:** the "Bots" section — last night's pipeline and each
  bot's window (what it received, what it did, what it handed on; a chart by night; run
  history); the "Trade paths" section — each symbol of the night through the stages with
  the outcome, a funnel, rejection reasons and a chart by night: entered / rule
  rejection / platform rejection. A retried night is shown by its last
  attempt (`src/web/bots.py`).
* **Two independent reviews before the first night** (the core; integration into the
  schedule). The main finding was the same in both: without valid approvals
  the Executor counted the day's decision and on a late attempt advanced the exchange
  forward, so a retry could no longer enter. Now this is an attempt without a decision
  (`run_daily(decide=False)`), and approvals come only from the last
  successful Risk Manager run. Also: a role is killed on timeout together with
  all its descendants; equity is written right after each close (a later crash
  used to lose the trade's result — a long-standing hole in the cycle); a late cycle stopped by
  reconciliation does not advance the exchange (a long-standing hole, DEC-048); the Controller
  knows about a stop executed after the cycle and computes freshness from the six
  daily bars, like the Analyst; the preview (`--dry`) writes to the board under its own
  book. Each fix is pinned by a test that fails without it.
* **The platform's first night, 2026-09-30** — on time (00:05:07 UTC, shift
  20 s, code 0), 22 signals out of 27, 0 approved (19 MAX_POSITIONS, 3
  MIN_NOTIONAL — both slots taken by AAVE and SOL), Controller 7/7, day
  shifts every hour with no gaps. An independent audit (4 lenses, each
  anomaly passed through a skeptic) recomputed the signals and sizes from raw
  data: all correct. Added: a night failure of the Collector (e.g. the month's
  universe was not collected) — a notification once a day. Known
  limitations, not fixed:
  - funding is collected with an 8 h delay (`complete_before` of bars for
    the rate): an accrual at the moment of a time exit will not make it into the trade
    result — on the order of $0.001, ~0.001R;
  - the AAVE/SOL stops were placed before the ATR fix (39 days of history): wider
    by 0.4–0.65 pp, AAVE size 0.07 instead of 0.08 — flag this when reconciling
    against the backtest;
  - BTRUSDT/TUTUSDT have no MMR, the sizer substitutes 0.005;
  - the night ran on the charger — the battery scenario (late runs on 27 and 29.09)
    was not tested that night;
  - from 26.10 (winter time) the pmset wake at 02:05 local = 01:05
    UTC: for a sleeping Mac nights will become late — move it to 01:05.

---
## DEC-048 — 2026-09-29 — Day watch over paper; decision invariant

**Problem.** Between night cycles the paper exchange stood still: a stop hit
during the day was booked the following night, and the panel showed midnight prices.

**Decision.** Every hour after the night decision (`scripts/scheduled.sh`,
the "day already done" branch): hourly bars and funding for open positions,
a stop run, reconciliation against the accounting — triggered stops are booked at once.
No entries during the day: one decision per day, as in the backtest.

**Invariant, derived from six review rounds on the night of 28.09:** paper decides
"as of 00:10" only while it has seen nothing later than midnight. It is held by three
marks in the state file: `replayed_until` (minimum across symbols — from
which bar to continue the stop check), `seen_until` (maximum — whether
the exchange has seen time after the decision), `decided_for` (the day's decision is made;
only after it does the watch move the exchange forward). An attempt without a decision (no
universe, all data stale, halted by reconciliation) does not move the exchange
forward and does not set the decision mark.

**Autostart at :05**, the pmset wake-up minute: on battery the Mac
wakes for about 40 seconds, and the :10 job did not make it in time (nights of 27.09, 29.09).

**Known limitation.** Funding is booked over (entry, accounting moment],
while the collector stores an accrual only one interval after it. A stop
booked during the day does not see the last accrual; one booked at night
sees accruals after the stop. Magnitude: 1e-4 of notional (fractions of a cent
on $10). The exact boundary is the stop's execution time, which `Fill` does not carry.
Not fixed: the cost of the fix (collector and `Fill`) is out of proportion to the size.

---

## DEC-047 — 2026-09-26 — Setup H rejected; outcome of the three ideas F, G, H

**Rejected by the rule**, four of seven conditions failed. OOS untouched.

In-sample 2022-02-01 — 2026-02-07, configuration `6c84306`, deposit $100.

| condition | required | actual | |
|---|---|---|---|
| E | > +0.15R | **−0.097R** (E/SE −1.0) | FAIL |
| trades | ≥ 60 | 65 | PASS |
| drawdown p95 (portfolio) | < 40% | 18.4% | PASS |
| survives at 0.10% | E > 0 | −0.101R | FAIL |
| without the two best | E > 0 | −0.213R | FAIL |
| above the null hypothesis | ≥ 2 SE | **−0.28 SE** (−0.097 vs −0.067R) | FAIL |
| median move over the holding | ≥ 1.6% | 10.0% | PASS |

Few trades: a 3 ATR move within a day on daily bars is rare. The sample is weak
(SE 0.097), but the direction of the result also goes against the hypothesis.

### Outcome of the three ideas, in order

| setup | idea | E | over coin flip |
|---|---|---|---|
| E (for comparison) | time-series momentum | +0.024R | **+2.63 SE** |
| F | funding against the crowd | −0.047R | −1.85 SE |
| G | rank momentum | +0.011R | +0.74 SE |
| H | reversal after an extreme day | −0.097R | −0.28 SE |

**On this data the picture is consistent:** the only thing that differs from a random
direction is a bet that the market will keep going where it went
over the last month (E), and it is small. Bets against the move (F, H) are worse
than or no better than a coin flip; the relative strength of coins (G) is zero.

**None of the eight setups (A–H) passed the E > +0.15R threshold.** The threshold
is not revised: it was recorded before the first run (DEC-028), and lowering
it after eight failures would mean fitting the bar to the result.

### What follows from this

Simple rules on daily and hourly bars of a single exchange, available
with this data, **do not give an edge sufficient
for the set thresholds** over 2022–2026. Further variations of the same ideas (other windows,
other yardsticks) amount to a brute-force search, and each further test on the same
data costs more: the more attempts, the more likely a pass by chance.

E's forward paper run continues — it is the only thing accumulating new data.

---

## DEC-046 — 2026-09-26 — Setup H preregistration: reversal after an extreme day

**Recorded BEFORE implementation and BEFORE any run.**

### Hypothesis

After a day with an extreme move (panic, stop run, news),
part of the move retraces over the following days: forced selling
and buying ends, and the price comes back. This is a bet AGAINST
the move on a short horizon — not a week (there, per E, F and G,
the move tends to continue), but three days.

### Rule (daily bars, decision at the UTC daily close)

1. **Day's move** — close minus the previous day's close. The previous
   day must be exactly yesterday (P-001), otherwise there is no signal.
2. **Yardstick** — ATR(14, 1d) at the PREVIOUS close: the range known
   before this day. Measuring the day by an ATR it is already part of would mean
   measuring the extreme by itself.
3. **Extreme** — |move| ≥ `k = 3` × yardstick.
4. **Direction against the move:** up — **short**, down — **long**.
5. **Stop** 2 × ATR(14) at the current close (with this day included —
   after a stop run the range is wider), cap 30%.
6. **Time exit only**, after `H = 3` days. No breakeven.
7. Portfolio, sizer, limits, candidate shuffling — shared.

Sweeps (report only): `k` ∈ {2, 2.5, 3, 4}, `H` ∈ {1, 3, 7},
stop multiplier ∈ {1.5, 2, 3}.

### Null hypothesis

Same days, same exits — direction by coin flip.

### Pass conditions — the same seven. Same windows, one OOS for all.

### What I know in advance and an honest forecast

Three results on this data say "the move continues":
E (time-based, +2.63 SE over the coin flip), F (against the crowd — worse
than the coin flip), G (rank-based — zero). H is also a bet against the move, but
after a single extreme day and for three days, not after a month for
a week. That is a different horizon, and effects on different horizons can be
opposite.

**Forecast:** low, one in eight. The most likely outcome is around
zero or worse than the coin flip, like F.

---

## DEC-045 — 2026-09-26 — Setup G rejected: no relative strength

**Rejected by the rule**, three of seven conditions failed. OOS untouched.

In-sample 2022-02-01 — 2026-02-07, configuration `1ba6499`, deposit $100.

| condition | required | actual | |
|---|---|---|---|
| E | > +0.15R | **+0.011R** (E/SE 0.44) | FAIL |
| trades | ≥ 60 | 1 408 | PASS |
| drawdown p95 (portfolio) | < 40% | 30.5% | PASS |
| survives at 0.10% | E > 0 | +0.008R | PASS |
| without the two best | E > 0 | −0.008R | FAIL |
| above the null hypothesis | ≥ 2 SE | **+0.74 SE** (+0.011 vs −0.010R) | FAIL |
| median move over the holding | ≥ 1.6% | 8.85% | PASS |

Portfolio $100 → $95.85, 309 trades taken out of 1 408.

### What this means

The rank-based side is no better than a coin flip. The relative strength of coins a week
ahead **predicts nothing** on this data. Together with E this
splits the effect into parts: E had +2.63 SE over the coin flip, G, where
market direction is subtracted, has +0.74 SE. **E's weak signal is almost entirely
a bet on market direction**, not on which coin is stronger.

For the forward paper run this means: E effectively trades "the market will keep
going where it went for a month". With two slots in the portfolio this is one bet
in two copies — the correlation limit partly catches this.

Next in order is H, reversal after an extreme day. Two
results in a row (F, G) argue against reversals on a one-week horizon;
H tests a shorter horizon.

---

## DEC-044 — 2026-09-26 — Setup G preregistration: rank momentum

**Recorded BEFORE implementation and BEFORE any ranking run.**

### Hypothesis

Coins that rose MORE than the rest over the last four weeks
will also do better than the rest over the next week; the weakest ones will do
worse. This is **relative** strength, not market direction.

How G differs from E. E bets that a coin will continue ITS OWN
move — and so, mostly, on market direction: in a bull year
almost all coins "rise", and E is long almost all the time. G opens
equal numbers of longs and shorts every day: the overall market move averages out
across trades, and the only question left is whether the strong coin outpaces
the weak one.

### Rule (daily bars, decision at the UTC daily close)

1. **Day's candidates** — symbols in the universe of the bar's month
   with a known `L = 28`-day return at this close
   (the same function as D's: time-based, no neighbouring bars).
   Fewer than 10 candidates — no signals that day.
2. **Rank** — by `L`-day return. The `K = 3` strongest go
   **long**, the `K = 3` weakest go **short**. Equal returns
   are ordered by symbol name — deterministically.
3. **Stop** 2 × ATR(14, 1d), cap 30%. **Time exit only**,
   after `H = 7` days. No move to breakeven.
4. Portfolio, sizer and limits — shared; simultaneous candidates are
   shuffled with a fixed seed, as for E and F.

Market neutrality is a property of the trade POPULATION (three on each
side every day), on which the thresholds are computed. A portfolio with two
slots does not guarantee it; portfolio drawdown is computed honestly, as is.

Sweeps (report only): `L` ∈ {7, 14, 28, 56}, `K` ∈ {1, 3, 5},
`H` ∈ {3, 7, 14}, stop multiplier ∈ {1.5, 2, 3}.

### Null hypothesis

Same days, same symbols, same exits — **direction by coin flip**.
If rank means nothing, the rank-based side is no better than a random one.

### Pass conditions — the same seven

Five acceptance thresholds, beating the null hypothesis by ≥ 2 SE, median
move over the holding ≥ 1.6%. Same windows; one OOS for all setups.

### What I know in advance

E (time-series momentum) on this data: +0.024R, 2.63 SE over the coin flip.
F (against the crowd on funding): −0.047R, worse than the coin flip. Both results
say "the move continues", but both mix relative strength
with market direction. Nobody has tested pure relative strength
on this data. If E's signal is almost entirely market direction,
G will show around zero.

**Forecast:** passing the E > +0.15R threshold is unlikely — one in five.
The most likely outcome is a weak positive result below the threshold.

---

## DEC-043 — 2026-09-26 — Setup F rejected: against the crowd is worse than random

**Rejected by the rule**, four of seven conditions failed. The recent
OOS was not spent.

In-sample 2022-02-01 — 2026-02-07, configuration `6df0373`, deposit $100.

| condition | required | actual | |
|---|---|---|---|
| E | > +0.15R | **−0.047R** (E/SE −3.6) | FAIL |
| trades | ≥ 60 | 1 840 | PASS |
| drawdown p95 (portfolio) | < 40% | 34.4% | PASS |
| survives at 0.10% | E > 0 | −0.050R | FAIL |
| without the two best | E > 0 | −0.052R | FAIL |
| above the null hypothesis | ≥ 2 SE | **−1.85 SE** (−0.047 vs −0.020R) | FAIL |
| median move over the holding | ≥ 1.6% | 5.52% | PASS |

Portfolio $100 → $81.32 over four years, 468 trades taken out of 1 840.
Exits: time exit 86.5%, stop 13.3%.

### What this means

The hypothesis "after an extreme skew the price moves against the crowd" was
**not confirmed even in a weak form** on this data: betting against the crowd is worse
than a coin flip. This is consistent with what is already visible in E: in crypto over these
four years moves tended to continue rather than reverse.

**What I am NOT doing with this.** The temptation is to flip the rule ("on funding,
with the crowd") and declare it a new setup. That would be fitting:
a hypothesis invented after looking at the result, on the same data.
Besides, −1.85 SE is not significant in the reverse direction either. If such a
hypothesis is ever tested, it will be only on data that
this run has not seen.

Next in order is G, rank momentum.

---

## DEC-042 — 2026-09-26 — Setup F preregistration: funding skew, against the crowd

**Recorded BEFORE implementation and BEFORE any look at how price behaves
after extreme funding.** I had seen funding data only as a stream
(completeness, intervals — DEC-025, DEC-036), not linked to returns.

### Why

Setup E (DEC-039): the signal is real but small — +0.024R against a +0.15R threshold.
Waiting months for a forward sample without looking for anything is a bad plan.
The first of three ideas recorded in order: F — funding, G — rank
momentum, H — reversal after an extreme day.

### Hypothesis

Funding is a payment that one side of a perpetual contract pays
the other. Extremely high funding means the crowd is long
and paying for it; extremely negative means the crowd is short.
**Hypothesis: after an extreme skew the price over the following days moves
against the crowd** — positions unwind.

This is a directional bet, like D. The null hypothesis is therefore the same:
**same entries and exits, direction by coin flip.**

### Rule (daily bars, decision at the UTC daily close)

1. **Skew measure** — the average payment per 8 hours over the last `W = 3`
   days: the sum of RAW rates accrued in (close − 3 days, close],
   × 8 h / 72 h. Raw, not `rate_8h`: when the interval changes, an average
   over records would overweight hourly accruals eightfold.
   Window coverage by accruals below 90% — no signal (P-001).
2. **Extreme** — the percentile of the current measure among its values at the 180
   previous closes of the same symbol (at least 120 values,
   otherwise no signal). The symbol's own history, not a common threshold: different
   coins have different normal funding levels.
3. **Direction against the crowd:**
   * top `P = 10%` AND measure above the base rate of 0.01% per 8 h → **short**;
   * bottom 10% AND measure below zero → **long**.
   A percentile extreme at an ordinary level (e.g. "the highest
   in half a year" = 0.008%) is not a skew and does not count as a signal.
4. **Stop** 2 × ATR(14, 1d), cap 30% — as for E, with the same formula.
5. **Time exit only**, after `H = 3` days. Unwinding a skew is a matter
   of days, not weeks. No move to breakeven.
6. Simultaneous candidates — shuffled with a fixed seed;
   portfolio, sizer and limits — shared, as for E.

Sensitivity sweeps (for the report only; the base variant is not
chosen by them): `W` ∈ {1, 3, 7}, `P` ∈ {5, 10, 20}, `H` ∈ {1, 3, 7},
stop multiplier ∈ {1.5, 2, 3}.

### Pass conditions — the same seven as for E

Five acceptance thresholds (DEC-028), beating the null hypothesis by at least
2 SE, median move over the holding at least 1.6%. Same windows: in-sample
2022-02-01 — 2026-02-07; out-of-sample 2026-02-07 — 2026-09-01 —
one for all setups, spent once.

### Honest forecast

Against F: high funding often accompanies a strong
trend, and trends in crypto continue (that is E's signal): shorting
against the crowd in the 2023–2024 bull market may systematically get caught
by the continuation. For F: the stop and the three-day holding limit
the worst case, and the skew is independent information, not price.
I consider passing the E > +0.15R threshold unlikely, about one in six.
The test is cheap and does not spend OOS.

---

## DEC-041 — 2026-09-25 — Phase 5: daily cycle, paper on market prices and testnet

**Question.** Setup E was rejected on the E threshold (DEC-039), but the signal is real
(2.63 SE over the coin flip), and the recent OOS cannot test it — its power is
9%. There is nowhere to get new out-of-sample data except the future. How do we
collect it without risking money and without breaking the tested rule?

**Chosen: a daily cycle with two execution exchanges.**

* `paper` — a paper exchange on MAIN-market prices from the collected hourly
  bars. No keys needed. This is a forward test: the rule is frozen,
  data arrives after the freeze. Execution is not in our favour: entry
  at market with slippage from the config, the stop is checked on every
  hourly bar, on a gap — at the bar's open, commission on every
  fill.
* `testnet` — Bybit testnet. Tests the mechanics: signatures, response
  formats, idempotency, conditional stops. Testnet prices have nothing to do
  with the market; trade results there do not test the rule.

The main exchange is unreachable by construction: the trading client refuses
any address other than testnet (`assert_testnet`).

### What is enforced by construction, not by agreement

1. **The live signal is the same code as in the backtest**: `to_daily`,
   `build_atr_series`, the signal function via `PastView`. A test runs
   the engine over a random series and requires that on EVERY entry day the
   live signal matches the backtest entry — side, price, stop.
2. **Candidate queue** — the same hash as in the portfolio replay.
   The test checks the selection against `portfolio.replay` itself, not a copy of the formula.
3. **Universe — of the signal bar's month**, as in the backtest: the decision on the 1st
   of the month is made on the previous month's bar.
4. **Entry window — 2 hours after 00:00 UTC.** The backtest enters at the daily
   close; an entry nine hours later is a different trade. Exits do not depend
   on the window.
5. **Freshness (P-001).** No last complete day — the symbol is skipped
   (STALE_DATA). A held position without hourly bars up to the current hour —
   the cycle does not start: it is unknown whether the stop was hit. Execution-exchange
   price more than 3% from the signal close — skip (PRICE_DIVERGENCE).
6. **Equity is accounting-based**: $100 from the config plus the realized result
   from FILLS (price, both commissions, signed funding). The testnet
   balance is a toy and does not enter sizing.
7. **A rerun is safe.** Order keys derive from the symbol and the decision
   moment; the trade is written to the DB BEFORE the entry is sent (`pending`).
   A process that died between the write and the outcome is resolved on the next
   run from the exchange's fills: no entry — the record is removed;
   an entry — it is promoted to `open`.
8. **Reconciliation first.** A position that vanished from the exchange is explained only
   by a fill of its own stop, confirmed by the exchange. Anything else
   unexplained (a foreign position, a size mismatch) disables trading
   until a manual `trade-resume`. A position without a stop gets a new stop
   immediately, then trading is disabled.
9. **Books are separate** per execution exchange (`setup_e:paper`,
   `setup_e:testnet`): otherwise reconciliation with testnet would see paper
   positions as phantoms.

All nine cycle checks were broken on purpose (P-002) — each is caught by
its own test.

### Found and fixed along the way

* The Executor reconciled only against the JUST opened position: the second
  position saw the first as foreign and disabled trading. Now
  `open_position(held=...)`.
* `backfill --symbols universe` failed with "attached to a different loop":
  the symbol list was read from the DB in one event loop, loading ran
  in another. The DB engine is now reset between them.

### How to use it

`scripts/daily.sh` after 00:00 UTC and before 02:00 UTC: refresh data for the
universe and open positions → `trade-cycle --execute`. Status —
`trade-status`, re-enabling after a halt — `trade-resume --reason`.
Once a month, on the 1st — `build-universe` (the script will remind you).

### What this does NOT change

Setup E is rejected, and the forward paper run does not cancel that. It accumulates
the sample that was lacking. A decision on real money is a separate
record and only by a criterion named in advance: the same acceptance thresholds
(DEC-028) on a forward sample of at least 60 trades.

---
## DEC-040 — 2026-09-25 — Position size cap: 30% of the deposit

**User's request:** a position within $5–30 with a $100 deposit.

The lower bound is held by the exchange's minimum order size ($5). There was no upper one:
with a very tight stop, the 1.5% loss limit allowed a position of up to $175
with leverage — $17.50 collateral × 10.

**Added `risk.max_position_pct: 30`.** A position larger than 30% of the deposit
is TRIMMED to the cap rather than rejected: the signal is not lost, and the loss
at the stop can only shrink from the trimming.

Check on 4 134 Setup E trades: position from $5.00 to $29.99,
median $8.67, loss at the stop no more than $1.50. Ordinary trades
(stop ~18%, position ~$8) are not affected by the cap.

There were no dedicated sizer tests until now — only the collapse stress
fixtures. They arrived together with the cap.

---

## DEC-039 — 2026-09-25 — Setup E: the signal is real but small; OOS cannot confirm it

**Rejected by the rule.** The recent OOS has not been spent.

### Result

In-sample 2022-02-01 — 2026-02-07, configuration `f65d67f`, deposit $100.

| condition | required | actual | |
|---|---|---|---|
| E | > +0.15R | **+0.024R** (E/SE 1.80) | FAIL |
| trades | ≥ 60 | 4 118 | PASS |
| drawdown p95 (portfolio) | < 40% | 21.5% | PASS |
| survives at 0.10% | E > 0 | +0.021R | PASS |
| without the two best | E > 0 | +0.012R | PASS |
| **above the null hypothesis** | **≥ 2 SE** | **+2.63 SE** | **PASS** |
| median move over the holding period | ≥ 1.6% | 8.15% | PASS |

Portfolio $100 → $135.14 over four years, 359 of 4 118 trades taken.

### A real signal, for the first time

**The preregistered null hypothesis is passed.** The sign of the four-week
return gives +0.024R, a coin flip with the same entries and exits gives
−0.014R, over 20 865 trades. This is the first evidence in five setups
that predicting direction is worth something on its own.

Exactly as predicted in DEC-038 before the run: D at $40 lacked
power (740 trades, 1.27 SE); at $100 there are 5.6 times more trades,
and the excess grew to 2.63 SE — roughly as the square root of the number of trades.

### But the effect is small, and the threshold is not passed

+0.024R against a threshold of +0.15R. The threshold is not revised: lowering it after
seeing the result is exactly what the whole construction protects against
(DEC-028, rule 4).

Separately, I record a methodological observation without changing anything because of it:
a threshold in units of R is not equally strict for different setups. For E the stop is
a sizing tool, not an exit, and it is wide (median 17.8% of price);
R is therefore large, and E in R is small. As a share of price, +0.024R is about 0.4%
per week. For future preregistration the criterion should be set
with this in mind — but BEFORE the run, and not for E.

### OOS cannot confirm an effect of this size

Power calculation, made before any access to the OOS:

| | |
|---|---|
| expected trades in the OOS (6.8 months) | ~580 |
| expected E/SE given the true effect | **0.64** |
| expected excess over the coin flip | ~1.0 SE |
| probability of seeing E/SE ≥ 2 if the effect is real | **9%** |
| months for E/SE = 2 with the same effect | ~66 |

Spending the last clean OOS on E would almost certainly yield
"not significant" — and lose the OOS forever, having learned nothing. So it
is not spent, even if the threshold had been passed.

### Outcome of five setups

| setup | outcome |
|---|---|
| A | hypothesis refuted |
| B | the construction did not test the hypothesis |
| C | did not survive a different regime |
| D | not tested as intended — the deposit cut off 84% of trades |
| **E** | **the signal is real (2.63 SE above the coin flip), but small: +0.024R** |

The only path to new out-of-sample data is time moving forward.
Paper trading on testnet gives exactly that without risking money, and
also tests the whole "collection → analysis → order" chain live.

---

## DEC-038 — 2026-09-25 — $100 deposit; preregistration of Setup E

### Deposit

**Minimum deposit — $100.** The user's decision, made on the
numbers of DEC-037: at $40 the exchange's minimum order cut off 84% of the days
of a daily strategy with a normal stop, at $100 — 9%.

All risk percentages are unchanged (P-007, DEC-019): 1.5% per trade, loss
limits, margin. What changes is the absolute size: risk per trade $1.50,
stop cap 30%, margin per trade $17.50.

**The universe is rebuilt.** ETHUSDT entered all 60 months — at $40
it could not be split into three parts, now it can; because of the 30-symbol cap
five borderline symbol-months were pushed out. BTCUSDT still
cannot be split (`NO_PARTIAL_EXIT`).

The instrument filter arithmetic tests silently took the deposit from the working
config and broke when the value changed. Their input is now pinned explicitly:
the test checks the arithmetic, not the current business value.

### Setup E — rule D, tested the way it was intended

**Recorded BEFORE implementation.**

DEC-035 and DEC-037 showed: at $40 what was tested was not rule D but "rule
D on days where a 2×ATR stop does not exceed 12%" — 16% of days. That is a different
hypothesis, and its failure says nothing about the rule as intended.

**Setup E is rule D without changing a single parameter:** window
28 days, holding 7 days, time exit only, stop 2 × ATR(14, 1d),
no move to breakeven, shuffling of simultaneous candidates.

Exactly one thing changes — the stop cap, and not by choice but by the same formula
that was preregistered in DEC-033: risk / minimum order =
$1.50 / $5 = **30%**.

Pass conditions — the same seven as for D: five acceptance thresholds,
an excess over the null hypothesis (coin-flip direction) of at least 2 SE,
a median move over the holding period of no less than 1.6%.

Windows: in-sample 2022-02-01 — 2026-02-07; out-of-sample 2026-02-07 —
2026-09-01 — **the last clean OOS, spent once for all setups.**

### What I know in advance and what may distort the result

**D's in-sample result is known**: E +0.044R, excess over the coin flip
1.27 SE — on 16% of days, which will also enter E's sample. The remaining 75% of days
have never been seen by anyone for rule D. E's in-sample is therefore not independent
of what I know — it is precisely the OOS that protects against this.

**Why E may pass the null hypothesis without being "better" than D.** If
the true effect is the same size (~0.05R) and there are several times more trades,
the excess will grow as the square root of the number of trades: at 740 → ~4000
that is 1.27 → ~3 SE. This is not fitting — it is power that D did not have.

**Why E will most likely fail the E > +0.15R threshold.** D had +0.044R on its
subsample. To pass the threshold, E on the previously excluded days
of high volatility must be noticeably higher. There is no basis for that.
The threshold is not revised.

**Honest forecast:** passing the E > +0.15R threshold is unlikely, one chance
in six to eight. The test is still worth it: the code is almost ready,
and a failure on in-sample does not spend the OOS.

---

## DEC-037 — 2026-09-24 — Data is complete relative to the exchange; account size as a constraint

### Check after the backfill

| stream | before | after |
|---|---|---|
| 4h candles, gaps | 1 209 | **0** (1 209 closed) |
| 1h candles, gaps | 16 | 1 |
| funding, records | ~380 k | **1 033 852** |
| funding, gaps > 8 h | 780 across 213 symbols | **1** |
| `rate_8h` inconsistent with the interval | not checked | **0** |
| intervals outside {60, 120, 240, 480} | not checked | **0** |

Funding turned out almost three times more complete than before. The only remaining
gap — BNXUSDT 2023-02-16…28 — was checked with a query: the exchange has neither
funding nor candles for those days, the contract was halted. **The data is complete
relative to the exchange itself.**

### Account size: a measurement without looking at trade results

Setup D showed that the stop cap derived from the deposit cut off
most of the signals (DEC-035). Here is the same thing — separately from any
strategy and without a single trade result: what share of days
in the universe allows a 2 × ATR(14, 1d) stop at different deposits.

38 109 symbol-days, 2022-02 — 2026-02. The median stop is **17.8%
of price** (p25 13.6%, p75 23.0%, p90 29.3%).

| deposit | 1.5% risk | stop cap | feasible |
|---|---|---|---|
| **$40** | $0.60 | 12.0% | **16.3%** |
| $100 | $1.50 | 30.0% | 90.9% |
| $200 | $3.00 | 33.3% | 94.1% |
| $1000 | $15.00 | 33.3% | 94.1% |

Cap = risk / the exchange's $5 minimum order, bounded above by 33%:
with a wider stop the distance to liquidation is less than three stops even without leverage.

**At $40 the exchange's minimum order cuts off 84% of the trades of any daily
strategy with a normal stop.** The constraint stops shaping the
strategy at around $100–200.

### What this means and what it does NOT mean

This is a measurement of which strategies are even **testable and executable**
at a given account size. It says nothing about whether
any of them is profitable: the four tested setups showed no edge,
and a larger account does not create one.

The second consequence is scale. Even if a setup passes all thresholds
with room to spare (E = +0.15R), at $40 that is $0.60 × 0.15 ≈ $0.09 per trade.

The capital decision is not a technical one, and it is not mine to make. Recorded
so that it is made on numbers, not on a feeling.

### What survived for the next hypothesis

The recent OOS 2026-02-07 — 2026-09-01 has not been touched by any setup.
It is the only clean test left, and it is spent
once for all setups (DEC-034).

---

## DEC-036 — 2026-09-24 — Data defects: my diagnosis was wrong, and the real ones turned out bigger

As agreed after D's failure — fixing data defects. The work
started with checking my own diagnoses and refuted one of them.

### The age defect (DEC-031) — almost nonexistent

What DEC-031 recorded, "BTC and ETH rejected as too young", was an inference,
not a check. The check showed: from the very start the code takes the listing date
from the exchange, and the censoring affects 3 delisted symbols in 9 symbol-months,
all in 2021-10…12. No analysis window uses these months.
The code's behaviour here is correct per P-001 (age unknown → rejection);
only the reason label is inaccurate. **Nothing to fix in the code.** A correction was made
in the text of DEC-031 itself, so that the wrong reason is not read as fact.

### The real defects — more than expected

Checking funding found data loss instead of one latent normalization
defect. All four defects were confirmed by a direct query to the exchange,
not inferred:

| stream | defect | scale |
|---|---|---|
| 4h candles | one bar at each page boundary of the old download | 1 209 bars |
| funding | one record at each page boundary of the old download | ~1 in 200 |
| funding | pages sized by today's interval | up to 75% of records for some symbols |
| funding `rate_8h` | normalization by today's interval | up to 4× |
| OI 1h | — | clean, 1 gap |

**Candles.** Hundreds of symbols lost THE SAME bar: 2025-01-21 12:00 —
379 symbols, 2025-07-07 08:00 — 332, 2025-12-21 04:00 — 265. The step is
~166.5 days, exactly a page of 999 × 4h from the start of the two-year download.
This is data collected before the DEC-004 fix: the code was fixed, but what had been
collected was not re-downloaded. The exchange does return these bars.

**Funding, lost records.** Pages were sliced by the symbol's TODAY'S
interval. SOLAYERUSDT is at 4 hours today, but in 2025 funding
was charged every hour: a window held ~800 accruals, the exchange returned the 200
newest, the older 600 were lost. The data shows an even pattern: 199 hours
of records, 605 hours of nothing, cycle after cycle (P-004).

**Why the tests did not catch this.** On truncation the fake exchange returned the
OLDEST records, while the real one returns the newest; and the fake's timestamps
were counted from the start of the request instead of lying on the grid. The fake
is now fixed to behave the way the exchange does. The old test required stamping
the declared interval in spite of the data — that is, it locked in the DEC-025 defect.

### Fixes

* funding is sliced by the hour — Bybit's shortest interval;
  no window holds more than 200 records at any interval;
* the interval of each record is derived from the spacing to its neighbour; spacing
  that is not a Bybit interval is treated as a gap;
* `repair-gaps` reports the outcome via its exit code — it printed "Failed"
  and returned 0, the same disease as `backfill` in DEC-026;
* backfill: 4h candles for 815 candidates and 1h for the universe via
  `repair-gaps`, funding — a full re-download with the fixed collector,
  then a rebuild of the universe.

The regression test was broken on purpose per P-002: with the old slicing — 721
accruals out of 1800.

### Why the C and D verdicts are not revisited

Missing funding means the holding cost was UNDERSTATED —
the real E of C and D is even lower than measured. D's null hypothesis pays
the same cost, so the excess over it barely changes.
The lost bars are 3 moments per symbol over five years. No defect
could turn a failure into a pass. Rerunning the rejected setups
on the fixed data would be a search for a better number, not a test —
and the validation ledger would not allow it anyway.

### A lesson, repeated for the third time

DEC-029, DEC-031 and here: every time a hypothesis was checked by a direct
query to the exchange or a table, the first version of the explanation turned out
wrong. What is inferred from indirect signs must not be recorded in this log as fact —
only after verification.

---

## DEC-035 — 2026-09-24 — Setup D rejected on in-sample; account size shapes the strategy

**Final, per the DEC-033 rule.** The recent OOS has not been spent —
`validate-setup` rejected the attempt, verified.

### Result

In-sample 2022-02-01 — 2026-02-07, configuration `6668201`.

| condition | required | actual | |
|---|---|---|---|
| E | > +0.15R | **+0.044R** (E/SE 1.22) | FAIL |
| trades | ≥ 60 | 740 | PASS |
| drawdown p95 (portfolio) | < 40% | 18.4% | PASS |
| survives at 0.10% | E > 0 | +0.037R | PASS |
| without the two best | E > 0 | +0.011R | PASS |
| **above the null hypothesis** | **≥ 2 SE** | **+1.27 SE** | **FAIL** |
| median move over the holding period | ≥ 1.6% | 6.24% | PASS |

Portfolio $40 → $53.82 over four years.

**D is better than C in almost everything**: positive E over the whole window, drawdown
at half the threshold, survives slippage, does not rest on a couple of
symbols. But the condition written precisely after the lesson of C is not met:
**sign prediction beats the coin flip by 1.27 SE.** Coin-flip
direction gives −0.007R, the return sign gives +0.044R. The difference is real
in sign and indistinguishable from noise in magnitude.

### Diagnostics after the verdict — for knowledge, not for amending D

| year | trades | E | longs | shorts |
|---|---|---|---|---|
| 2022 | 87 | +0.021 | −0.088 | +0.067 |
| 2023 | 378 | +0.038 | +0.029 | +0.045 |
| 2024 | 124 | +0.118 | +0.241 | −0.014 |
| 2025 | 111 | +0.066 | +0.125 | +0.013 |
| 2026 | 40 | −0.149 | −0.548 | +0.678 |

Momentum behaved the way momentum should: shorts in the falling 2022,
longs in the rising 2024. The prediction in DEC-033 ("2022 will almost certainly
look good") was only weakly confirmed: +0.021R.

### The main finding: account size shaped the strategy

| funnel stage | remaining | passed |
|---|---|---|
| days considered | 115 506 | — |
| sign non-zero | 115 345 | 99.9% |
| **stop within the 12% cap** | **7 683** | **6.7%** |
| Risk Manager funded | 4 169 | 54.3% |
| symbol was in the universe | 740 | 17.8% |

**The stop cap let through 6.7% of candidates.** Two daily ATRs are almost
always more than 12% for crypto — so D traded only in rare
calm moments, mostly in large coins.

And 12% is not a strategy parameter. It is deposit arithmetic: $40 × 1.5% risk
= $0.60; with the exchange's $5 minimum order, a stop beyond 12% gives a position
below the minimum. **Account size decided which trades exist at all**,
and what was tested was not time-series momentum but time-series momentum in moments
of low volatility.

This is not a reason to tweak the cap and retest: D is rejected. But it is
the first direct measurement of what DEC-032 stated as a supposition:
a $40 deposit narrows the search space more than any hypothesis.

### Outcome of four setups

| setup | idea | outcome |
|---|---|---|
| A | reversion after OI compression | hypothesis refuted |
| B | trend, structural stop | the construction did not test the hypothesis |
| C | trend, ATR stop | did not survive a different regime |
| D | time-series momentum, daily bars | did not beat the coin flip by 2 SE |

None gave an edge distinguishable from market beta and noise. Done
as agreed: moving on to fixing data defects (DEC-025,
DEC-031), then to revisiting the initial conditions, not a fifth hypothesis
on the same data.

---
## DEC-034 — 2026-09-24 — Setup D implementation; the out-of-sample window is spent once for all setups

Implemented strictly per the DEC-033 preregistration (commit 121349f). Not a
single run on data before the freeze.

### What was added

* **time-only exit**: `Signal.target = None`. Not "a target somewhere
  far away" — a distant target would look real in the report, and the target
  guards would take it seriously. Behaviour of A/B/C is unchanged: they always
  set a target;
* **daily bars** from 4h: UTC days, complete ones only — an incomplete day
  is dropped, not filled in (P-001), and the unclosed current
  day does not enter the sample;
* **window return by time, not by bar index**: a dropped
  day would shift the index, and "28 bars ago" would become 29 days;
* **null hypothesis** with the same code: direction by coin flip, everything
  else shared; each symbol has its own seed;
* **order of simultaneous candidates** — a shuffle with a fixed
  seed; the default is the old order, so as not to rewrite Setup C's numbers
  retroactively;
* **shared window evaluation** (`evaluation.py`): the verdict for C and D is
  computed by the same code.

### A rule that was not needed before

**The out-of-sample window is spent once for ALL setups.** The recent OOS is
shared. If D spends it and fails, a future Setup E can no longer
call those months "out-of-sample": I have looked at them, and any next
hypothesis will know about them what I know.

The rule is enforced by `validate-setup`: an `out_of_sample` window that overlaps
an already spent out-of-sample window of any setup is rejected.
In-sample may overlap — that is where looking is allowed;
the 2022–2024 period, formerly OOS for C, legitimately serves as in-sample for D.

### Freeze — per setup

What is frozen: the setup's own config section, the shared trading sections
and the code: engine, runner, portfolio, accounting, indicators, resampling, risk —
plus the setup's own file. Files of OTHER setups are not included: a fix
in the rejected A does not change D's hypothesis.

Noticed during the switch: the C freeze now shows a divergence
from `a6ff492` — the engine, portfolio and runner were changed for D. C's behaviour
does not change because of this, but the guarantee should not take it on trust
either. The C check is closed, so this affects nothing — and it is good that it shows.

### Known bias, recorded before the run

`Trade.opened_at` and `closed_at` are the bar's OPEN time, while the trade
happens at its close. The funding accrual window is therefore shifted
one bar earlier than the actual holding. The number of accruals is correct;
the shift is one day for daily D. This error has no systematic sign;
recorded so that nobody has to hunt for it if the result is borderline.

---

## DEC-033 — 2026-09-24 — Setup D preregistration: time-series momentum on daily bars

**Recorded BEFORE a single line of Setup D code was written.**
Not a single number for D has been computed. Everything below is fixed now
and does not change based on results — neither in-sample nor out-of-sample.

### Why a different horizon

The combined result of the three setups (DEC-032): signals on horizons from 15 minutes
to a week carried ~0.05–0.1R of information at a trade cost of ~0.07R.
The main lever is the signal-to-cost ratio. On daily bars
and a weekly hold, the move is many times larger than the 0.21% round trip.

### Hypothesis

**The sign of the return over the last four weeks predicts the sign
of the return over the next week.** Time-series momentum (TSMOM)
is the most documented effect in the crypto market;
studies find it on horizons from one to four weeks.

Setup D is an explicit bet on market direction over the holding window.
DEC-032 showed that Setup C was implicitly such a bet. Here it is not a side
effect but the hypothesis itself, and the null hypothesis is built exactly for it.

### Rule

| parameter | value | source |
|---|---|---|
| bars | daily, UTC, from six 4h | — |
| signal window | 28 days | top of the 1–4 week range; longer means less noise |
| signal | sign of the window return; zero means no trade | classic TSMOM |
| direction | long and short | classic TSMOM |
| hold | 7 days, time exit only | weekly rebalancing |
| target | none | the time exit is the hypothesis itself |
| stop | 2 × ATR(14, 1d) | the Turtles' "2N" stop — as a sizing tool, not an exit |
| stop cap | 12% | $40 × 1.5% = $0.60 of risk; $0.60 / $5 min. notional = 12% |
| move to breakeven | NOT applied | contradicts the "hold for a week" hypothesis |

The cap filters out the signal in the setup, not in the Risk Manager (DEC-015).

**Sweeps** are a result, not a selection method (DEC-016): signal window
[7, 14, 28, 56], hold [3, 7, 14], stop [1.5, 2.0, 3.0] × ATR.

### Simultaneous signals

All symbols get a signal at the same daily close. The portfolio
replay has so far ordered simultaneous candidates by symbol
name — for Setup C this was almost harmless (breakouts at different
times), for D it is a systematic alphabetical selection.

**Simultaneous candidates are ordered by a shuffle with a fixed
seed.** Not by signal strength: ranking by strength is a separate
hypothesis (cross-sectional momentum), and two hypotheses must not be mixed
in one test.

### Guards

P-005 and P-006 were written for setups with a target: "the target is hit in at
least 10% of trades", "the target is no closer than 2R". A time exit has no target,
and P-005 by its own logic does not apply — it says that with an
incompatible target "the holding window is measured, not the hypothesis"; here
the holding window IS the hypothesis.

The P-006 equivalent for a time exit: **the median absolute return
over the holding window is at least `min_move_pct` = 1.6%.** Otherwise the move
is comparable to costs, and there is nothing to test.

P-007 (risk and loss limits) applies unchanged.

### Null hypothesis — a mandatory condition, not a reference figure

**Same entries, same exits, direction by coin flip.** This isolates
exactly what the hypothesis claims: predicting the sign. Everything else —
frequency, size, costs, funding, limits — is shared.

**Setup D must beat its null hypothesis by at least two
standard errors in-sample.** Otherwise the result describes market beta,
not the signal — exactly what DEC-032 found for Setup C. The condition
is stricter than the acceptance thresholds and is added to them, not instead of them.

### Windows

| window | period | use |
|---|---|---|
| in-sample | 2022-02-01 — 2026-02-07 | 4 years: 2022 bear, 2023 range, 2024–25 rally |
| out-of-sample | 2026-02-07 — 2026-09-01 | once, only after passing in-sample |

In-sample starts in February 2022: before that the universe is distorted
by age censoring (DEC-031).

### Order and prohibitions

1. Setup D code is written and frozen by a commit BEFORE the first run
   on data. Parameters do not change after that.
2. In-sample: the five "preliminary" thresholds + beating the null
   hypothesis by ≥ 2 SE. Any failure — D is rejected, OOS is not spent.
3. If it passes — OOS once via `validate-setup`, with the same
   code-level prohibitions as for Setup C.

### What is already known and may bias in-sample

I am honestly recording what I know about the period before the run: DEC-032 showed
that in 2022 Setup C's shorts made money and its longs lost. Momentum
by definition goes short after declines — so 2022 will almost certainly
look good for D. This is not fitting (the rule is taken
from the literature and was not tuned), but it is a reason why D's in-sample
may turn out better than its out-of-sample. OOS is exactly what protects against this.

### Honest forecast

About one chance in four. If D does not pass — as agreed
with the user, we fix the data defects (DEC-025, DEC-031) and revisit
the starting assumptions, rather than look for a fifth hypothesis on the same data.

---

## DEC-032 — 2026-09-24 — Setup C rejected on deep history

**Final, by the rule recorded before seeing the data (DEC-028).**
The recent out-of-sample is NOT spent: the `validate-setup` command
now rejects it by itself — verified.

### Result

Window 2022-02-01 — 2024-08-01 (DEC-031), configuration `a6ff492`; the trading
code and config match the frozen ones.

| threshold | required | actual | |
|---|---|---|---|
| E on OOS | > +0.15R | **−0.017R** | FAIL |
| trades | ≥ 60 | 1506 | PASS |
| drawdown p95 (portfolio) | < 40% | **53.0%** | FAIL |
| survives at 0.10% | E > 0 | −0.033R | FAIL |
| without the two best | E > 0 | −0.038R | FAIL |

E/SE = −0.52. Portfolio $40 → $41.88 over 30 months.

### Comparison with in-sample

| | in-sample 2024–26 | deep history 2022–24 |
|---|---|---|
| E | +0.062R | −0.017R |
| correlation of E with BTC range | +0.487 | **−0.058** |
| above random entry | +2.27 σ | +1.25 σ |
| portfolio | $40 → $69.96 over 18 mo | $40 → $41.88 over 30 mo |

**The only hope named in advance did not materialise.** DEC-022
found that E grows with market trendiness, and DEC-028 recorded that
deep history with its trending years could lift E above the threshold.
Out of sample the link vanished completely: −0.058. A textbook example of structure
found in-sample that did not survive leaving it.

### Diagnostics — for the next preregistration, not for this setup

Everything below was computed AFTER the verdict and does not affect it. Using
it to adjust Setup C and re-test it on the same data
is forbidden — that would be fitting.

| | trades | E |
|---|---|---|
| 2022 (bear) | 457 | +0.075R |
| 2023 (range, recovery) | 689 | **−0.089R** |
| 2024 (through August) | 360 | +0.007R |
| **longs** | 795 | **−0.137R** |
| **shorts** | 711 | **+0.118R** |

**The result is determined by market direction, not by the signal.** In the falling
2022, shorts made money and longs lost; in the rising in-sample it was
the other way round. Setup C is largely a bet on market beta over the holding
window, not on the breakout.

**The trigger carries a little information, but less than the costs.** A random
entry with the same construction loses −0.069R in both windows; the setup beats it
by +0.11R in-sample and by +0.05R on deep history. The direction
is the same, the magnitude is below the trade cost.

### Summary of the three setups

| setup | idea | outcome |
|---|---|---|
| A | reversion after OI compression | hypothesis refuted (DEC-016) |
| B | trend, structural stop | the construction did not test the hypothesis (DEC-013) |
| C | trend, ATR stop | the in-sample edge did not survive a different regime |

Common to all three: signals on OI, funding and breakouts on horizons
from 15 minutes to a week carry no more than ~0.05–0.1R of information, while a
trade costs about 0.07R. **A system that placed orders right now
would lose money.** The platform sees this and does not allow trading — this is
not a malfunction but exactly what it was built for.

### What survived

* **the recent OOS** 2026-02-07 — 2026-09-01 — untouched for any setup;
* five years of history for 209 symbols and 815 candidates, loaded without errors;
* all the infrastructure: collection, analysis, risk, order plan, executor,
  reconciliation, validation with prohibitions in code — 715 tests.

---

## DEC-031 — 2026-09-24 — Deep-history window shifted before the run: a censoring artefact

**Recorded BEFORE the `validate-setup --period deep_history` run.** Not a single
Setup C number has been computed on this window yet.

### What was found

The deep-history universe turned out thinner: 8 symbols at the end
of 2021, 11–23 in 2022, the full 30 only from late 2023. Breaking down the
rejection codes separated two different causes.

**`LOW_LIQUIDITY` in 2022–2023 is a property of the market.** 143–160 rejections
a month: in a bear year fewer symbols kept $25M of daily turnover.
The threshold is procyclical, as DEC-009 predicted. Left as is: this
is exactly the point-in-time composition of that period.

**`TOO_YOUNG` in 2021-11 is an artefact.** 31 of 37 symbols were rejected as
younger than 90 days, including BTCUSDT (exchange listing 2020-03), ETHUSDT
(2021-03), XRPUSDT (2021-05). Age is counted from the first bar
IN OUR DATABASE, and it is the same for all of them — 2021-09-24, the start of the download.
History is left-censored, and the censoring passes itself off as youth.

> **CORRECTION (DEC-036): the paragraph above is wrong.** It was an inference from
> the first bar being 2021-09-24 for all symbols, not a check. From the very start
> the code takes the listing date from the exchange (`launch_time`), and only when
> it is missing — the first bar. Checked against the table: BTCUSDT and ETHUSDT were
> rejected with `NO_PARTIAL_EXIT` (lot step), XRPUSDT and DOGEUSDT were included. Of the 31
> "young" ones, 22 really are young: Bybit listed SOL, ATOM, NEAR,
> SHIB and dozens of other USDT perpetuals in August–October 2021.
> Censoring is real only for three delisted symbols without a listing date
> (FTM, EOS, MATIC) — 9 symbol-months in 2021-10…12. The window shift
> remains correct: the market really was thin back then.

The same error had already eaten the first months of in-sample: with the two-year
download all symbols were "born" in 2024-08, and the August–November
2024 universe was thin. Hence the shift of E from +0.067R to +0.062R
noticed in DEC-028: after the history was deepened those months became
complete, and the number became MORE ACCURATE, not worse.

### Decision

Start of the deep-history window: **2021-11-01 → 2022-02-01**. The censoring
is gone by February: 2021-09-24 + 90 days of age + a month of selection
with the lag.

**Why this is legitimate.** Preregistration protects against choosing a window
based on the result. There is no result on this window — the correction is driven
by a data defect found before the run, and is recorded before the run.

**The cost is recorded honestly: the correction works AGAINST Setup C.** November
2021 is the market peak and one of the most trending months of the period, and
the setup's only named hope (DEC-028) is precisely trending
months. I am throwing out months that would more likely help the hypothesis
under test.

### Why the age calculation itself was not fixed

The correct fix is to take the listing date from the exchange
(`instruments.launch_time`) rather than the first bar in the database. But delisted
symbols do not have this date, and the fix would rebuild the universe for all
periods, including in-sample — and the test would compare two different
systems. Recorded as a known defect; it gets fixed before the next
setup, not in the middle of testing this one.

---

## DEC-030 — 2026-09-24 — The validation plan was never loaded; now it is enforced by construction

### The preregistration that wasn't

DEC-028 recorded the validation plan in `config/settings.yaml`: the frozen
commit, the test windows, the drawdown definition. It looked like
a preregistration. **By default Pydantic silently drops unknown
keys** — the `backtest.validation` section was never loaded at all, and no
code read it.

Discovered when it actually needed to be read. A scan of the whole
config against the models showed it was the only such key —
but it could just as well have been a typo in a threshold name: the value
from the file is ignored, the default is used, and nothing reports it.

**Fixed for the whole config**: a shared `StrictModel` base
with `extra="forbid"`. An unknown key is a load error (P-001).
A test with a deliberate typo covers this.

### Three prohibitions in code

At the very start of this work the user put the requirement
more precisely than I did: "the guarantee must remain by construction, not
by agreement". The validation plan had so far rested on agreement.
The `validate-setup` command turns it into construction:

1. **The configuration is frozen.** The sections that define trading
   (`strategy.setup_c`, `strategy.management`, `min_target_r`,
   `min_move_pct`, `risk`, `costs`) are compared with their state
   at the frozen commit via `git show`. Any divergence means
   rejection: tweaking a parameter between two tests and calling it the same
   hypothesis is not allowed. Verified both ways: against `a6ff492` there are
   no divergences, against `d29cb5f` (before the risk edits) it catches `risk`.

2. **Order.** The recent OOS is rejected until deep history has
   passed, and rejected forever if deep history has failed (DEC-028,
   rule 2).

3. **Once.** The result is written to `config/validation_ledger.json`
   under git control; a repeat run of a window on the same configuration
   is rejected. The ledger can be deleted by hand — but that will remain in
   the history, not just in someone's memory.

Passing on a DIFFERENT configuration does not count: that is a test of
a different hypothesis.

### Along the way

The `validate` command already existed — a data integrity check.
A new one with the same name would have silently overwritten the old one; the
linter caught the redefinition. The new one is called `validate-setup`.

---

## DEC-029 — 2026-09-24 — The cursor crept one bar at a time: 957 requests for a single row

**A second case of DEC-004, in the same place and for the same reason.**

### How it was found

A light pass over 815 candidates — 4h candles only, about 11 requests
per symbol — ran at 0.4 requests per second against a limit of 20.
Not the network, not the limit, not the database. A breakdown of the completion
lines showed what P-004 calls a collection defect — regularity where there
should be none:

| symbol | requests | rows |
|---|---|---|
| most | ~10 | hundreds–thousands |
| 10000NEXUSDT | 220 | 287 |
| 0GUSDT | 745 | 287 |
| 1000000BABYDOGEUSDT | **957** | **1** |

### Cause

Bybit's window end is INCLUSIVE, ours is exclusive. This is
written in the `page_window_end` docstring — for the LEFT edge of the window. For
advancing the cursor the same subtlety was not taken into account.

When a backward window hits the first bar of a symbol's history, the exchange
returns exactly that boundary bar, and the collector drops it as
`ts >= end`. The page is not empty — so the "empty page, jump
over the window" branch does not fire. The "progress guarantee" fired instead:
the cursor moved ONE interval forward. The next request returned the same bar.
Up to a thousand requests for every symbol listed after the start of the
window — that is, for every young one.

### Fix

Cursor advancement had been copied into three collectors and was broken the
same way in all three — exactly the story already seen with the window edge
in DEC-004. Extracted into `next_cursor` next to `page_window_end`:
if nothing was accepted from a page — whether it was empty or everything was
dropped — the window has been fully covered, and the cursor jumps to its end.

The guard was extended: a test requires all collectors to use the shared
function and not to advance the cursor in place.

### Testing the test

The regression test runs a collector against a fake exchange
with an inclusive window end — like the real one. Per P-002 a new check
must be broken deliberately once: with the old advancement the test goes red
at **410 requests for an empty range**. It was separately verified that the
fix loses nothing: across 2501 bars each one lands in the sample
exactly once.

### Result

| | before | after |
|---|---|---|
| symbols per minute | ~2.3 | **~75** |
| max requests per symbol | 957 | 12 |
| light pass over 815 candidates | ~6 hours | ~11 minutes |

The parallel download from DEC-026 gained almost nothing for exactly this reason:
parallelising a crawling cursor is pointless. The speed was bound not
by waiting on the network, but by thousands of unnecessary requests.

### Lessons learned twice

The first hypothesis — that Bybit returns fresh bars for a range before the
listing — was tested with a direct request and turned out wrong: the exchange
returns nothing. The second — a slow forward window — was tested
by replaying the loop step by step and was also wrong. The answer came from
the log lines with the number of requests per symbol. Hypotheses were tested,
not taken for granted.

---
## DEC-028 — 2026-09-24 — Setup C fails acceptance even in the best case; validation plan

**Recorded BEFORE the deepened data arrives.** The Setup C configuration
is frozen at commit `a6ff492`; nothing stated below may be changed
based on the results of the check.

### Correction to my own reports

In the DEC-019…DEC-025 reports I wrote "all in-sample thresholds passed".
That was wrong. Threshold #1 — **E on out-of-sample > +0.15R** — is defined
on OOS and therefore was never compared against in-sample. The report printed
the thresholds that were computed, and the first of the five was silently missing.

Fixed in the report: the "acceptance thresholds — preliminary" block prints
all five ALWAYS, under the assumption that OOS repeats in-sample.

### Result: two of five thresholds fail

| threshold | required | actual in-sample | |
|---|---|---|---|
| E on OOS | > +0.15R | **+0.062R** | FAIL |
| trades on OOS | ≥ 60 | ~366 expected | PASS |
| drawdown p95 | < 40% | **45.5%** | FAIL |
| survives at 0.10% | E > 0 | +0.050R | PASS |
| does not rest on 1–2 symbols | E > 0 without the best two | +0.032R | PASS |

**The assumption "OOS repeats in-sample" is the best realistic case,
not the expected one.** Out-of-sample results are usually worse.

Even by the E of trades taken by the portfolio (+0.140R) the 0.15R threshold is not met —
there is no ambiguity that would allow picking a convenient definition.

### The drawdown definition question — decided now, before the data

In the DEC-019…DEC-025 reports I called the drawdown "24.6%, threshold passed".
That is the PORTFOLIO drawdown — over the ~300 trades the account actually took.
The threshold in the acceptance report (`build_full_report`) is computed over the ENTIRE
signal population — by that measure it is 45.5%.

Which of the two is correct is decided here and now, not after
it becomes clear which one is more favourable:

**The drawdown threshold is computed on the PORTFOLIO.** The reason has been written
in the function itself since it was written: the permutations answer the question
"will we survive it on a real account". The account trades the subsample
carved out by the limits, not the whole population; the population drawdown
describes an account without a position limit, which does not exist.
The population number is printed alongside, for comparison.

The decision favours the less strict definition — this is visible,
and that is why the rationale is recorded before a single number
from the deepened data exists.

### Validation plan — order and rules

Two periods remain untouched for Setup C:

* **deep history** 2021-11-01 — 2024-08-01, about 33 months,
  including the 2022 bear market — a different regime not seen in-sample;
* **recent OOS** 2026-02-07 — 2026-09-01, about 7 months.

Rules:

1. **Deep history first**, with configuration `a6ff492` without a single
   change. The five thresholds are applied as for OOS.
2. If deep history fails any threshold, Setup C is
   rejected, the recent OOS is NOT spent and is kept for the next setup.
3. If it passes, the recent OOS is run ONCE with the same
   configuration. No changes between the two checks.
4. The E > +0.15R threshold is not revised. If there is an urge to lower it,
   that is a separate decision about acceptable risk, with a rationale, made
   BEFORE the run, not after.

### What to honestly expect

With in-sample E of +0.062R and two thresholds failing already in the best
case, Setup C will most likely be rejected. It does appear to have
an edge — +2.27 standard errors over random
entry — but it is less than half of what the project declared
sufficient in advance.

There is exactly one hope, and it was named in advance: DEC-022 showed that E grows
with market trendiness (correlation +0.487 with BTC range). Deep
history contains 2021–2022, the years with the strongest trends of the period.
If E comes out above the threshold there, that will not be curve-fitting but a test
of a hypothesis already stated.

### Caveat about the transitional state of the database

The numbers above were computed on a universe table rebuilt by the broken
run (DEC-026). E moved from +0.067R to +0.062R. Part of the shift
is apparently legitimate: with deeper history the listing date is known
more precisely, and symbols that previously looked "young" in the first
months of the window now pass the age filter. Final numbers come
after a correct rebuild.

---

## DEC-027 — 2026-09-24 — Executor: one decision — one position; no position lives without a stop

**Question.** The chain "collect → analyse → place order"
broke at the last link: there was an order plan and reconciliation with the exchange,
but nothing to send orders with.

### Exchange interface and the paper exchange

The Executor does not know whether it trades on Bybit, on testnet or on paper.
Otherwise it could only be tested with real money —
that is, nobody would test it.

The paper exchange is not a stub but a model with the properties on which
executors break:

* a retry with the same `link_id` is rejected as a duplicate rather than executed
  a second time — this is how Bybit behaves;
* reduce-only cannot open or increase a position;
* a submission can be forced to **execute and lose the response** — the
  nastiest failure.

### First guarantee: one decision — at most one position

The exchange response can be lost AFTER execution. The exchange rejects a retry
with the same key as a duplicate — and that is a **confirmation, not an error**.
An Executor that answers a duplicate with a new key opens a second
position on top of the first.

Verified with three scenarios: the entry response is lost, the stop
response is lost, the whole opening process is restarted with the same decision. In all
three there is one position.

### Second guarantee: a position without a stop does not live longer than one step

The stop is placed right after the entry is confirmed and **before the target**:
the target is money we may fail to earn, the stop is money we
can lose.

If the stop could not be placed, the position is closed at market, and only
then is trading switched off. This is the only order the Executor
sends without a plan: a position without a stop is worse than any closing price.

If the emergency close also fails, the report shouts about it
("POSITION WITHOUT STOP, human needed"), and reconciliation shows the position
as foreign.

### The Executor trusts the exchange, not its own responses

After every step the state is re-read from the exchange, and at the end there is
a mandatory reconciliation. The entry counts as done only if the exchange
shows a position on the right side; the stop only if it is visible
among open orders.

### What is missing

A real Bybit adapter for submission. That is Phase 5 (testnet) and
is done only after out-of-sample: trading keys appear when
there is something to trade. Everything above the adapter is ready and tested.

---

## DEC-026 — 2026-09-24 — The download lied about success, and the pipeline corrupted the universe

**P-002 violated for the fourth time, and this time with consequences.**

### What happened

The deepening pipeline passed `backfill` a path to a file with the list of
symbols. All other CLI commands read files; `backfill` did not:
it parsed the argument only as comma-separated, and the string
`var/reports/universe_symbols.txt` was sent to the exchange as a SYMBOL NAME.

Bybit answered "Symbol Invalid" for every stream. The command honestly
printed "ERROR" — and **returned exit code 0**. `set -euo pipefail`
in the script is powerless if the command itself lies about success. The script carried
on, ran all four stages in a minute and printed "DONE".

Along the way `build-universe --years 5` **rebuilt the universe table
on incomplete data**: deep 4h history existed for only ~53 symbols
out of those already selected for 2024–2026. The 2021–2024 universe
ended up assembled from today's survivors — exactly the bias
DEC-003 guards against, and exactly the reason the pipeline
was built in a specific order.

The table will be rebuilt by a correct run. **Until it finishes,
Setup C results on the database cannot be trusted** — they were computed
on a corrupted composition.

### Three fixes

**1. `backfill` reads files**, like all other commands, via
the shared `_resolve_symbols`. A name with paths and dots is rejected before
any exchange call, as a usage error. Case from a file is normalised
the same way as from a string: otherwise a single symbol would arrive in two
spellings.

**2. The exit code reflects the result.** Three outcomes, because they
have different costs:

| code | meaning |
|---|---|
| 0 | everything downloaded |
| 3 | some streams failed to download |
| 1 | nothing downloaded, or the circuit breaker tripped |

A partial failure on a bulk download is expected — some delisted
pairs lack one stream or another — and the decision whether to tolerate it stays
with the caller.

**Exactly 3, not 2.** The first version used 2 and was
caught in review: Typer reports usage errors with exit code 2.
A script tolerating partial failure would also tolerate a typo in the arguments.
There is now a test for this.

**3. The script checks the code explicitly** via a wrapper that tolerates only
0 and 3 and prints the number of failed streams instead of swallowing it.

### How it was caught — and how I fell for the same thing myself

Detected by timing: four stages expected to take hours finished
in a minute. Not by a check — there was no check.

While verifying the fix, I ran the command through `| tail -3` and got
"code 0" — the code of `tail`, not of the command. Exactly the case P-002
started with: `pytest | tail` returned 0 with failing tests. I know the
rule, I wrote it down myself — and broke it ten minutes after
finding a violation of it in someone else's code. Re-checked without the pipe.

This confirms the wording of P-002 better than any example: a rule
that relies on attentiveness is not a rule.

### Side fix: parallel download

The reason for the restart in the first place was speed. The download ran strictly
sequentially: about 290 requests per symbol at an actual 2.5
per second against a limit of 20 — nine tenths of the throughput
sat idle waiting on the network. The 147 symbols alone would have taken
eight hours.

Symbols are parallelised (`--concurrency`), not streams within a symbol:
checkpoints per (symbol, stream) pair are independent. Safety rests
on three things that already exist: a token bucket under `asyncio.Lock`,
the circuit breaker as shared client state, idempotent writes.

---

## DEC-025 — 2026-09-24 — The backtest held positions for free

**`Trade.funding_paid` existed from the very beginning and always stayed
zero.** The fourth case in a row: the field is defined, the reason code is declared,
the value is not computed. Setup C holds a position for up to 168 hours — that is
up to 42 funding charges.

### Measurement before the fix

| | |
|---|---|
| holding hours, median | 24 (p90 — 72, max 168) |
| charges per trade, median | 3 (max 42) |
| cost in R, mean | **+0.0023** (we pay) |
| p90 | +0.0126, max +0.1090 |

Small, but not zero: E dropped from +0.069R to +0.067R. Recorded as a number —
"probably immaterial" and "measured at 3% of E" are different
statements.

Median holding time turned out to be 24 hours, not 168: most trades
close at the stop or target long before the time stop. That is exactly why
the omission was small.

### Raw rates, not normalised to eight hours

To compute what was charged, the `rate` at the moment of the charge is used, not
`rate_8h`. Normalisation is needed to compare symbols with each other;
for the sum of charges it is unnecessary and dangerous on top of that.

**A normalisation defect found along the way.** `rate_8h` is computed
from the interval given to the collector ONCE for the whole run — that is,
from today's value in `instruments`. But for some symbols the interval
has changed: SOLAYERUSDT has five different steps in its history, from 60 to 480 minutes,
ZKUSDT — from 60 to 1260.

For such symbols the entire history is normalised incorrectly, in places by a factor of two.

**Why this does not affect anything yet.** The only consumer of
`rate_8h` is the funding filter in the setups, and it computes a PERCENTILE within
a single symbol. Multiplying the whole series by a single constant does not change
the percentile. The defect is latent: it will fire at the very first comparison
of rates across symbols or in absolute terms.

Deliberately not fixed now: the fix requires re-mapping
the interval by actual timestamps and rewriting history,
and there is no gain today. Recorded so that it does not have to be found again.

### A comparison bug that had to be fixed immediately

After funding was enabled, Setup C started paying for holding, while the random
entry from the null hypothesis test did not: the schedule was passed
to only one of the two runs. The comparison instantly stopped being
a comparison, and in favour of the random entry at that.

Excess over random entry after fixing both sides:
**+2.27 standard errors** (was 2.84 before funding was charged to both).

### The sign

A long pays when the rate is positive, a short receives. The sign is easy
to mix up, and the error looks harmless: the value is small, but the direction
flips its contribution. Both sides are covered by separate
tests, as is the fact that a charge exactly at the moment of entry is debited
from the previous holder, not from us.

### Setup C after funding charges

E +0.067R (SE 0.038, E/SE 1.74), portfolio $40 → $69.96, drawdown p95
24.6%, excess over random entry +2.27 σ.

---

## DEC-023 — 2026-09-24 — Tick size: measured, insignificant for the backtest, critical for orders

**Question.** The backtest enters at the exact close price and places the stop
at the exact computed level. The exchange has a tick size, and `tick_size`
is collected, stored in `instruments` and **not used in the backtest
even once**. How much does this inflate the result?

**Measurement on Setup C trades:**

| tick size | value |
|---|---|
| median, % of distance to stop | **0.105%** |
| p90 | 0.394% |
| worst symbol (BNBUSDT) | 1.289% |
| median, % of price | 0.0046% |

For comparison: round-trip costs are 0.21% of price, i.e. about 2.3%
of R. **The tick size is an order of magnitude smaller than the already modelled
slippage.** The backtest is not inflated by its absence, and there is no reason to introduce
it into the engine — that would add complexity for a value
drowning in noise.

Recorded as a number, not an assumption: "probably small" and "measured
at twenty times less than costs" are different statements.

**For orders it is critical**, and a separate layer appeared there.

---

## DEC-024 — 2026-09-24 — Order layer: rounding is provable, not hoped for

**`src/execution/` was empty.** Between a signal with levels, the Risk Manager
with a size, and the exchange there was nothing.

The module is pure: no network, no DB, no clock, no randomness. This is the code
that decides WHAT goes to the exchange, and it must be reproducible
to the last digit.

### Three rounding directions, all against us

* **entry** is placed AGAINST us: a long buys higher, a short sells
  lower. Assuming the favourable side of the tick is the same free
  profit as slippage in our favour;
* **stop** — CLOSER to the entry than specified. Further away would mean a loss
  larger than planned, i.e. a silent breach of the limit the
  Risk Manager has just computed;
* **target** — FURTHER from the entry. Closer would mean it is more reachable
  than in the backtest.

### The bug the test found

The first version rounded the three levels INDEPENDENTLY. A property test
"actual risk is not wider than specified" ran six tick sizes
at four offsets relative to the grid and found a case where two
independently rounded levels diverge by one tick — and the risk comes out
wider than planned.

Reworked: both exit prices are built from the ALREADY ROUNDED entry via
distances. Now the property is provable rather than hoped for, and the test
confirms it across the whole grid of cases.

Tellingly, the bug was not in a complex place but in three lines,
each of which looked correct on its own.

### Idempotency

`orderLinkId` is computed from the symbol, the decision moment and the order role —
and from nothing else. A rerun after a connection drop must produce
THE SAME key, otherwise the exchange will accept the second order as new and open
a second position on top of the first.

Randomness or the current time would be a bug here precisely because
they look more harmless: everything works until the connection drops.

### Exits are always reduce-only

Without this, a partial stop fill on an already closed position
would open an opposite one.

---
## DEC-022 — 2026-09-24 — Window spread: not "weak" but "wider than expected"

**Question.** The rolling window gives 7 positive out of 12. I myself made
it the main discriminator (DEC-018) and called it a weak spot three times.
Is it weak?

**"Seven out of twelve" means nothing by itself.** With a true
E = +0.07R and ninety trades per window, the per-window standard error is about
0.15R — windows MUST jump across zero. The right question: is the
spread wider than the window size implies.

| | value |
|---|---|
| positive windows | 7 of 12 |
| **expected at constant E** | **8.1** |
| observed spread | 0.246 |
| expected from sample size | 0.155 |
| ratio | **1.58x** |

**The share of positive windows is exactly as expected.** There was nothing
to worry about — on this criterion.

**The spread, however, is one and a half times wider.** The test windows do not
overlap (one month, one-month step, parameters not refit), so these
are twelve independent estimates, and the extra spread needs a cause.

### The cause was found, and it makes sense

Correlation of monthly E with **BTC's intra-month range: +0.487**
over 17 months.

Range specifically, not volatility (that gave only +0.220): volatility
is equally high in a trend and in chop, while a trend setup needs
price to go somewhere and not come back.

**The trend setup makes money in trending months.** This is not
an exposé but a sanity check, and the setup passed it:
a system equally profitable in a trend and in a range would be far
more suspicious.

### What I am NOT doing

Not adding a regime filter. A correlation of 0.487 over 17 months is
roughly 1.8 standard errors, and a filter derived from it
in-sample would be fitting of the same kind as the target search
from P-005.

If a filter does appear, it will be preregistered, and it will be tested
on the deepened history — which has three times as many months.

### Practical consequence

The month-to-month spread of E is not a measurement defect but a property of
the setup, and it is measured. In a range it does not make money; a smooth equity
curve should not be expected, and drawdowns will fall in non-trending
periods, not at random.

---

## DEC-021 — 2026-09-24 — Null hypothesis: does the trigger do anything at all

**A question that neither E nor its standard error answers.**

Setup C is not just an entry condition. It is also an ATR stop, a 2R target,
a holding window, a cap on stop distance, a move to breakeven
and portfolio limits. Any of these parts can produce
positive E on its own: the distribution of moves is asymmetric,
and a "1.5 ATR stop, 2R target" construction could make money with ANY
entry.

This is exactly the question Setup A burned on. There, no
dose-response to trigger strength was found; here its contribution
at all is tested.

### Method

ONE detail is replaced: the entry condition becomes a coin flip. Stop, target,
cap, timing of the ATR reading, engine, fees, universe, limits —
all exactly the same.

Entry frequency and long share are taken from the setup's actual run:
a random entry that fires three times as often or is skewed the other
way would be measuring a different quantity.

### Result

| | E | win rate |
|---|---|---|
| random entry, 1630 trades over 5 seeds | **−0.069R** | ~25% |
| Setup C, 1077 trades | **+0.069R** | 28.8% |

**Excess: +2.84 standard errors.**

Two consequences, and the second matters more than the first.

**The exit construction alone does not make money.** A random entry
loses 0.069R per trade — roughly the round-trip cost. So the
setup's positive E comes neither from the asymmetry of the move distribution
nor from a lucky choice of stop.

**This is not market drift.** Random entries with a long skew (57%,
as in the setup) over a rising period would have had to make money if
drift were the cause. They lose.

### The excess is computed over pooled trades

The first measurement gave +5.74 standard deviations, and that is wrong: it
used the spread of the MEANS across seeds. That describes the
generator's instability and ignores each estimate's own error,
which makes the excess come out roughly twice the true value.

The correct calculation is the pooled sample of random trades against the
setup's sample, summing the standard errors of both. It gives 2.84.
The difference between 5.74 and 2.84 is the difference between "proven"
and "plausible".

### What this still does not prove

2.84 is in-sample, on a single market regime, with 12 rolling windows
of which seven are positive. The null hypothesis is rejected for the TRIGGER'S
CONTRIBUTION, not for the persistence of the edge over time.

Out-of-sample is untouched.

---

## DEC-020 — 2026-09-24 — Moving the stop to breakeven: a trade-off, not an improvement

**Question.** `strategy.management.breakeven_at_r: 1.0` had been in the config
from the very start, `CloseReason.BREAKEVEN` was declared in the enum,
and the logic existed nowhere. The third case in a row: rule written down,
reason code created, no check.

**Implemented** in the engine, enabled via config, OFF by default:
enabling it changes the result of all past runs, and Setup B was rejected
with specific numbers that must not be rewritten after the fact.

### Two decisions about conservatism

**The moved stop takes effect from the NEXT bar.** Within a single
bar it is unknown what happened first — reaching the +1R threshold or
returning to the entry price. Assuming the threshold came first means passing
losing trades off as breakeven ones, and it looks harmless:
"the trade merely closed at zero instead of a loss". Covered by a test
on a bar that reaches both the threshold and the original stop.

**The R denominator does not change.** It is computed from the ORIGINAL stop; otherwise
the move would rewrite after the fact the risk we took at the moment of
entry, and R-multiples would no longer be comparable across trades.

**Slippage is not built into the level.** Breakeven covers the
round-trip fee (spec §6), and the actual exit ends up slightly worse than
zero — as everywhere, slippage works against us. Building it
in would mean moving the stop further from entry for the sake of a pretty number.

### Measurement: this is a TRADE-OFF

| | without move | with move |
|---|---|---|
| E per signal | **+0.084R** | +0.069R |
| win rate | 38.0% | 28.8% |
| breakeven exits | — | 265 of 1077 (24.6%) |
| drawdown p95 | 28.6% | **24.1%** |
| portfolio equity | $64.53 | $69.96 |

**E per signal drops.** The move cuts off some trades that would have
reached the target — this is not a side effect but the very nature of the rule.

**Enabled** because the spec says so and because it reduces
drawdown, whose threshold was fixed in advance. **Not because the
final equity on one path came out higher**: one path decides
nothing, and choosing by it is the same mistake that the permutation-based
drawdown calculation guards against.

### Setup C, in-sample, full picture

| check | value | threshold |
|---|---|---|
| trades | 1077 | ≥ 180 |
| P-005 | 27.9% at target + 24.6% at breakeven | ≥ 10% |
| P-006 | 2.00R / 9.02% | ≥ 2.0R / 1.6% |
| population E | +0.069R (SE 0.038, E/SE 1.80) | — |
| costs | survives up to 0.20% | at 0.10% |
| drawdown p95 | 24.1% | < 40% |
| concentration | +0.069 → +0.040 | > 0 |
| portfolio | $40 → $69.96 (+74.9%) | — |
| E of taken | +0.137R | — |

E/SE dropped to 1.80. Rolling window — 7 positive of 12.
Still a single regime, out-of-sample untouched.

---

## DEC-019 — 2026-09-24 — Portfolio run; risk per trade is derived, not assigned

**Question.** The per-symbol run of Setup C gave E = +0.089R over 1125
trades. That answers the question "does the signal have an edge" —
and it is not the question money lives on.

The setup produces 62 signals a month. With two slots and a seven-day
hold, the account can take about eight. So **which ones exactly** is
decided not by the strategy but by portfolio limits, and per-symbol E describes
the population from which the account takes a subsample.

### First run: the account died in two months

| | |
|---|---|
| candidates | 1125 |
| taken | **27 (2.4%)** |
| equity | $40 → **$22.11** (−44.7%) |
| E of taken | **−0.373R** vs +0.089R for the population |
| regime at end | OFF_WEEKLY |
| WEEKLY_STOP rejections | **1036** |

Twenty losses out of twenty-seven, the weekly limit, and then a thousand
rejections in a row. The signal's edge never reached the account at all.

### The cause is arithmetic and does not depend on the setup

The spec set risk at 6% per trade with a weekly limit of 20%. Each number
looked cautious on its own. Together: **four losses in a row
are enough to switch off the week.** With a 38% win rate and eight
trades a week, this is nearly certain within the very first month.

Formalized as P-007 with a guard in the config: the incompatibility is caught
at load time, not discovered from a dead account.

### Risk per trade is not a free parameter

It is derived from two already fixed thresholds:

* **P-007**: eight losses in a row do not switch off the week → risk ≤ 20/8 = 2.50%;
* **spec §7**: p95 drawdown over order permutations < 40% → measured.

| risk | taken | equity | p95 drawdown | threshold |
|---|---|---|---|---|
| 2.50% | 291 | $76.83 | 46.3% | FAIL |
| 2.00% | 294 | $63.37 | 42.0% | FAIL |
| **1.50%** | 283 | $52.23 | **34.4%** | PASS |
| 1.00% | 282 | $54.52 | 21.4% | PASS |

The largest value satisfying both is taken: **1.5%**.
Not the one giving the best return — at 1.0% the E of taken trades is higher.

**The realized path at 2.5% gave a 39% drawdown — and that was
luck.** That is exactly why the threshold is computed over permutations: one path
answers "how it turned out", permutations answer "how lucky we got".

### Acknowledged weakness

The second constraint is computed from the in-sample R distribution, so
the risk in this part is fitted to the data. The direction of fitting is
toward lower risk, and the value is rechecked on the deepened
history. Recorded here so that later it does not look like an after-the-fact
caveat.

### A literal found along the way

The drawdown calculation had a hardcoded `0.06` — the value of
`max_loss_per_trade_pct` at the time of writing. After the risk reduction the
literal would have overstated drawdown 2.4 times, and no test would
have noticed: the number looks plausible at any value.
Moved into an argument.

### The weekly stop is an absorbing state

It is lifted only manually (spec §5), and in a run there is no one to lift it.
Without an explicit resumption policy, a portfolio backtest measures not
return but the time until the first bad week — which is what happened.

What a human does is modeled: a review at the start of the next
ISO week and resumption from a new starting point. This is an ASSUMPTION,
and it is named in the code, not hidden in a flag: a run with it describes
a system with someone on duty, and without it — a system that was switched off and forgotten.

### Setup C result after fixes, in-sample

| check | value | threshold |
|---|---|---|
| trades | 1054 | ≥ 180 |
| P-005, target hit | 36.1% | ≥ 10% |
| P-006, distance | 2.00R / 9.04% | ≥ 2.0R / 1.6% |
| population E | +0.084R (SE 0.044, E/SE 1.91) | — |
| costs | survives up to 0.20% | survives at 0.10% |
| drawdown p95 | **28.3%** | < 40% |
| concentration | +0.084 → +0.051 without the two best | > 0 |
| **portfolio** | **$40 → $69.19 (+73.0%)** | — |
| E of taken | +0.152R | — |

E of taken trades is HIGHER than the population's: the correlation limit discarded 120
second bets on the same move, and it shows in the result.

**What this does not prove.** E/SE = 1.91 — less than two sigma.
The rolling window gives 7 positive of 12. Still a single market regime.
Out-of-sample untouched.

---

## DEC-018 — 2026-09-24 — Deepening the history and preregistration of Setup C

**Two entries in one, because the second must be made before
the data of the first arrives.**

### Discovery: we never had five years of history

The database listed rows from 2021, and it looked like a deep
history. A breakdown by year showed otherwise:

| year | symbols with 4h | bars |
|---|---|---|
| 2021 | 2 | 977 |
| 2022 | 3 | 3 563 |
| 2023 | 1 | 2 190 |
| 2024 | 492 | 371 893 |
| 2025 | 708 | 1 158 274 |
| 2026 | 604 | 704 467 |

Before 2024 the database holds two collapse fixtures (LUNAUSDT, FTTUSDT)
and one tail. `backfill_depth_years: 2` did its job honestly — the universe
starts in August 2024.

**A consequence nobody had noticed: both setups were tested on a single
market regime.** BTC's monthly volatility across the whole sample ranges from 7%
to 18%, without a single crisis, fading toward 2026. No bear
market, no panic. Setup A by design works on cascades of forced
closures — and was tested on a period where there are almost none.

### Decision: deepen the history to 5 years

Streams `kline_4h`, `kline_1h`, `oi`, `funding` for the 147 universe
symbols. `kline_15m` is NOT downloaded: it was needed for Setup A, which was
refuted, and its volume is four times that of the hourly stream.

What this gives, in decreasing order of importance:

1. **18 months of in-sample become roughly 48.** DEC-016
   ended with positive E appearing exactly where the sample
   fell below a hundred. Only data cures that.
2. **Other regimes appear** — 2022 and the tail of 2021.
3. **An untouched out-of-sample comes back.** Right now B's is
   spent, A's is formally intact, but it is the same calendar span:
   effectively one bullet for everything.

Caveat: not all of today's pairs existed in 2022. The universe
will become narrower but deeper.

### Preregistration of Setup C

Recorded 2026-09-24, **before** the deepened data arrived. The order
is not cosmetic: having seen new data, choosing parameters from it
and calling that a hypothesis is exactly the fitting P-005 guards against.

**The hypothesis is the same as Setup B's**, and deliberately so. DEC-013
stated: B was not refuted; its construction did not allow the
hypothesis to be tested. The question "does the trend continue with rising OI
and neutral funding" remains open.

**There is exactly one substantive difference — the stop.**

In B it sat beyond the 20-bar structural extreme, i.e. very
far away, and both of its problems followed from that at once:

* p75 of the move came out at 0.70R not because the market does not move but
  because R was huge. A 2R target with such a stop would mean a move
  of tens of percent;
* the risk manager rejected 885 signals on LIQ_BUFFER, and the remainder ended up
  skewed toward tight stops: the median distance fell from 11.83% to 8.88%
  (DEC-015).

In C the stop is set at 1.5 × ATR(14, 4h) with a hard cap of 8%
from entry. The cap cuts off the signal **in the setup**, not in the risk manager:
a risk-manager rejection is not visible in the setup logic and silently skews the sample.

**Where each number comes from — none was chosen by result:**

| parameter | value | source |
|---|---|---|
| trigger, OI, funding | as in B | the hypothesis under test, nothing to change |
| `stop_atr_mult` | 1.5 | spec value for Setup A |
| `max_stop_pct` | 8.0 | DEC-013: beyond this, unworkable at any leverage |
| `target_r` | 2.0 | P-006 floor `min_target_r`, no freedom |
| `time_stop_hours` | 168 | rule below |
| `oi_lookback_hours` | 48 | B's value, decoupled from the time exit |

**On the target.** It is NOT chosen by reachability. P-006 says: below
2R a target pays back neither the stop nor the round-trip fee, however reachable
it may be. There is exactly one open question — does the market give 2R within
the window, and P-005 answers it.

**On the holding window.** A rule written down before measuring: the holding window
is no shorter than the one the signal is defined over. The breakout is computed
over 20 bars (80 hours); a twofold margin is taken — 168 hours, 42 bars.
B had a 12-bar window with a 20-bar signal: a trade was given three times
less time than it took to detect it, and the DEC-014 measurement
confirmed it — the p90 move maximum fell on the last bar of the window.

**Every knob has a sweep** (DEC-015, DEC-016); the base value must
be part of its own sweep, otherwise the config does not load.

### Acknowledged weakness

The B family has already been looked at once, so out-of-sample evidence
for C will be weaker than for a truly fresh hypothesis. Compensation:
walk-forward within in-sample as the main discriminator — with 48
months there are enough windows — and OOS is spent once, at the end.

This is recorded here in advance so that later it does not look like an
after-the-fact caveat.

---
## DEC-017 — 2026-09-24 — Portfolio risk: six rejection codes with no implementation

**Question.** `RejectionCode` declared MAX_POSITIONS, MAX_MARGIN,
CORRELATION, DAILY_STOP, WEEKLY_STOP and MODE_OFF. The backtest counted rejections
by code, and the report printed them. Not a single one of these checks existed.

A missing check looks in the report exactly like a check
that never fired: an empty line. For a month and a half the reports
showed "correlation rejections: 0" — and that was true in the
same sense in which a switched-off counter's zero would be true.

**Done.** `risk/governor.py` — a portfolio layer between the signal
and the money. Pure: no network, no DB, no clock; everything comes in as arguments.
Same reason as for the sizer.

### Why this is a separate layer and not a condition in the sizer

The sizer answers the question "what size to enter THIS trade with"
and knows nothing about the portfolio. So it will equally approve two trades
that are flawless individually and together make up a single bet of double
size. That is exactly what P-003 guards against at the level of setup
logic — here it is the same thing at the level of positions.

### Three decisions that were not obvious

**1. Equity at the start of the period is stored, not the accumulated loss.**
An accumulated loss would have to be reset manually, and any missed reset
would switch trading off forever. Of the two errors, this one is quieter.

**2. The day rollover is checked before every decision, not on a timer.**
The system may stay switched off for three days, and the arrival of a new
day must not depend on whether the process was running at that moment.

**3. Manually switching back on after the weekly limit shifts the reference
points.** Otherwise the system would switch on and immediately switch off: the loss
relative to the old reference point has not gone anywhere.

### Correlation is applied more strictly than the config says

The config says "|corr| above the threshold → reject **when the directions
match**". That describes half the case. A long on one pair and a short on
another at a correlation of −0.9 win and lose together just as
two longs at +0.9 do. Implemented with the sign taken into account: a single bet is
matching directions with positive correlation OR opposite directions
with negative correlation.

**Unknown correlation rejects the trade** (P-001). Substituting zero
would mean passing off "we don't know" as "unrelated" — that is, as permission.

### Bug found: a single gap decouples identical series

Returns were computed between bars adjacent in the list. At the edges of a hole
adjacent bars are several intervals apart, and the move between them
is a four-bar move passed off as a one-bar move.

**Measured: one such outlier in three hundred points drops the correlation of two
IDENTICAL series from 1.00 to 0.79.** The threshold is 0.7, so the defect moved
the decision from a certain rejection to permission to open a second position
on the very same move. Caught by a test that expected 0.99.

A return across a gap is no longer computed: the series step is set explicitly
from the timeframe rather than inferred from the series itself — for a symbol with holes
the most frequent step may turn out to be the gap.

### Minimum overlap — 120 observations and half the window

Correlation over a couple dozen points cannot resolve 0.7, yet the decision
is made precisely on that threshold. An absolute minimum is not enough: 120
points out of 720 are not "the same thirty days" but two short pieces
that happened to coincide in time.

---

## DEC-016 — 2026-09-16 — Setup A: hypothesis refuted

**Final, on in-sample. Out-of-sample is NOT SPENT, and there is nothing
to spend it on.**

**Question.** DEC-015 showed that frequency is cut by two setup checks.
Can the frequency be recovered by calibrating them?

**The reading rule was written into config/settings.yaml BEFORE the run** — otherwise
a sweep turns into a search that picks the best E. In short: a sweep
is a result, not a way to choose a value; the frequency requirement
(>= 180 trades in-sample) was derived from the acceptance threshold ">= 60
on OOS", not assigned; choosing by maximum E is forbidden.

### Sweep over three knobs, baseline values for the rest

| knob | weaker → stronger | trades | E, R |
|---|---|---|---|
| candle size in ATR | 0.5 → 2.0 | 113 → 14 | −0.008 → +0.669 |
| volume spike, sigmas | 1.0 → 2.5 | 25 → 23 | +0.492 → +0.470 |
| funding, percentile | 40 → 10 | 77 → 25 | +0.202 → +0.492 |

The volume filter **does not bind at all**: at 1.0, 1.5 and 2.0 the result
is identical to the last digit. The funnel showed this too — it passes 82%.

The other two knobs look monotonic: the stricter, the higher E. Under
rule 4 this would mean "the filter is load-bearing; frequency cannot be recovered through it".

**But the monotonicity turned out to be a power artifact.** E rises exactly where
the sample drops below a hundred. A check at weak funding, where the sample
is sufficient:

| candle, ATR | trades | E, R | SE | E/SE |
|---|---|---|---|---|
| 0.5 | 756 | −0.035 | 0.041 | −0.85 |
| 1.0 | 305 | −0.093 | 0.064 | −1.45 |
| 1.5 | 81 | +0.183 | 0.130 | +1.41 |
| 2.0 | 47 | +0.213 | 0.161 | +1.32 |
| 2.5 | 32 | +0.172 | 0.191 | +0.90 |

There is no monotonicity: tightening from 0.5 to 1.0 makes E **fall**, and it only
turns positive once the sample collapses below a hundred observations. Not a single
point reaches two sigmas.

### The power threshold is reachable — and that settles it

The weakest corner of the declared grid yields **756 trades**
in-sample against the required 180. So the question "is there enough data"
is closed, and E is measurable with an error of 0.04R.

### Trigger sweep on a sufficient sample

This is a test not of tuning but of the hypothesis itself: if a cascade reverts,
then the sharper the OI contraction, the stronger the reversion.

| OI contraction per hour, % | trades | E, R | SE | E/SE | win rate |
|---|---|---|---|---|---|
| 3 | 756 | −0.035 | 0.041 | −0.85 | 41.3% |
| 5 | 232 | −0.045 | 0.068 | −0.66 | 44.0% |
| 8 | 69 | −0.147 | 0.114 | −1.29 | 37.7% |
| 12 | 23 | −0.056 | 0.192 | −0.29 | 30.4% |

**There is no dependence, and the sign is negative at every level.** A sharper
cascade does not produce a stronger reversion — it produces nothing. This is a direct
refutation of the hypothesis, not a shortcoming of tuning.

### Statement of the result

**The Setup A hypothesis is REFUTED.** The wording is deliberately different from
DEC-013's.

Setup B was not refuted: its construction did not allow the hypothesis to be
tested; what was measured was a random 48-hour hold. Here the construction
allowed the test — P-005 and P-006 are in place, exit and stop are computed
from one cascade window, the lookahead in ATR is eliminated — and the hypothesis
did not pass the test.

The distinction matters for the future: the question "does price revert after a cascade
of forced liquidations" is closed negatively on the top-30 crypto perpetuals
over two years, rather than left open.

### Why out-of-sample is not spent

The acceptance threshold requires E > +0.15R on OOS. In-sample, where parameters
may be tuned, E is negative on every sample on which it
is measurable at all. There is nothing to take there — the same reasoning as in DEC-013:
the key number is not OOS but in-sample.

The six months of OOS for Setup A remain untouched and will be useful for a third
setup.

### What is NOT a reason to continue

With the spec parameters (candle 1.5, funding 10) we get E = +0.492R
and a 68% win rate. That is 25 trades. Neighbouring, better-measured points
contradict it, there is no dose-response, and the positive values
appear only where the sample is under a hundred. The spec parameters
were not derived from the data — they were assigned before the first run.

Insisting on these 25 trades means picking the noisiest point of the grid
because it looks prettier than the rest.

### Side finding: P-005 had no minimum sample

The sweep produced a row with a target hit rate of 100%, a win rate of 100%,
E = +2.475R and a P-005 verdict of "PASS" — **on a single trade**. The guard
checked the rate and did not check what it was computed from.

Minimum 30 trades; below that the verdict is NOT DECIDED. Consequence: the verdict
"P-005 passed" from DEC-014 (11.5% on 26 trades) **is withdrawn**
and replaced with "not computed". DEC-014's conclusion does not change because of this —
it was about the exit construction, not about an edge — but the row
in the table was unbacked.

### What next

Both setup slots are free, plus the untouched OOS for A. The next setup
is designed taking into account what is now known and enforced in code by
guards: target, stop and holding window are set together (P-005, P-006);
every knob has a sweep; the sample is planned before the run,
not discovered after it.

---

## DEC-015 — 2026-09-16 — Frequency: three defects in universe composition

**Question.** Setup A gives 26 trades over 18 months. With a sample like that,
no number about it means anything: the standard error of E is on the order of
0.25R. Where are the signals being lost?

The hypothesis was the risk manager: DEC-013 showed that it filtered out 41%
of Setup B's signals. The hypothesis turned out to be wrong.

### Signal funnel, in-sample

| stage | remaining | passed the stage |
|---|---|---|
| bars considered | 4 719 323 | — |
| OI contraction per hour | 78 992 | 1.7% |
| **candle size in ATR** | **1 680** | **2.1%** |
| volume spike | 1 378 | 82.0% |
| **funding at an extreme** | **150** | **10.9%** |
| signal issued | 150 | 100% |
| risk manager funded | 139 | **92.7%** |
| **symbol was in the universe that month** | **26** | **18.7%** |

**The risk manager passes 92.7%.** It is not to blame. Signals are eaten by two
setup checks (candle size, funding) and — after the position is already open —
by universe composition: of 139 open positions, 26 are counted.

**What gets lost is not random.** The median stop distance across all
signals is 11.83%, across funded ones 8.88%. The risk manager
systematically knocks out distant stops, and the remainder is skewed toward tight ones. This is
a different population, not the same one only smaller, and E on it does not describe the setup.

### Defect 1. The rank cutoff was called LOW_LIQUIDITY

`_first_obstacle` returned `LOW_LIQUIDITY` when a symbol did not fit
into `top_n`. The docstring of this very function warns about exactly this three times:
"the reason code in the snapshot must point to the real obstacle".

The cost of the substitution: 1619 symbol-months pass the $25M turnover threshold, of which
906 have a rank above 30. So **the width of the universe was set by `top_n`,
not by the declared protection against illiquid symbols** — and it reported this under someone else's code.
DEC-009 lowered the threshold from $50M to $25M, the width did not change by a single
symbol, and the record read this as a sign of health ("rank binds,
not liquidity"). It should have been read the other way round.

The `RANK_TOO_LOW` code was added. Behaviour is unchanged, composition is unchanged —
only the truthfulness of the reason changes. The snapshot has 599 such symbol-months.

### Defect 2. Universe width had no sweep

The cross-cutting check requires a sweep for every composition knob, and the test
`test_three_universe_knobs_all_have_sweeps` guarded it — but it named
three knobs, required only that liquidity be positive, and did not count width
as a knob at all. Yet width was exactly what was binding.

`top_n_sensitivity` and `min_turnover_sensitivity` were added, with the same
"baseline value inside its own sweep" check as for
slippage and coverage. The test was rewritten for four knobs.

**Width is NOT fitted to the result.** The criterion is set in advance by the
acceptance thresholds: the sample must be sufficient for the test
(trades on OOS >= 60). This is a power calculation, not fitting.

### Defect 3. Universe composition depended on today's price

The tradability verdict was taken from the Instrument Filter at LIVE prices, and
the period was computed from `now()`. It surfaced on the very first rebuild:
ETHUSDT dropped out of the universe in all 24 months.

The check turned out to be right — ETH at a price of $2435 with a lot step of 0.01 splits
into two parts at a $70 notional, while `min_position_parts` requires three.
**The stored snapshot was what was wrong**: it had been built before this rule appeared
and had not been rebuilt since.

But the mechanism itself is broken both ways: a symbol that got more expensive drops
out of the year before last retroactively, and a repeat build gives a different
composition. This directly negates DEC-003, condition 4 — the snapshot was introduced for the sake of
reproducibility. The defect is silent: the composition changes, all backtest numbers
drift, and not a single check turns red.

Tradability is computed per month from the median close price of the selection
month (`tradable_in_month`), and the build period is anchored to the last day
with data, not to the calendar. Instrument parameters are still taken
as of today — that is known limitation 6, and it does not concern price.

### What changed in the composition

147 unique symbols instead of 142. Of 709 included symbol-months,
685 are the same; 24 left (ETHUSDT), 25 were added.

**The DEC-014 measurements were recomputed on the corrected universe.** The conclusions
held: A's median move is 1.19R versus 1.28R, p75 1.58R versus 1.73R;
for B the distribution matched to the second decimal. The 2.5R target hit rate is
11.5%, P-005 and P-006 passed.

### Decisions on the DEC-014 forks

**The stop stays beyond the cascade extreme.** That is what spec §6 and the
code's own comment say; a tight stop based on the entry bar would get through the
risk manager more often, and choosing the construction on that basis would mean
choosing by result.

**`exit_mode: fixed_r`, `target_r: 2.5`.** Not because E is higher —
choosing on that basis is forbidden — but because `vwap` failed P-006.
2.5R is the spec value, fixed before the first run. NOT 1.5R,
which the 75th percentile of the measured move would have given: the "target at p75" rule
was conditional and applied only if 2.5R was unreachable; after the fixes
it is reachable, so the condition was not met.

### The main point: widening the universe does NOT solve the problem

Removing the rank cutoff would add 599 symbol-months to 710,
i.e. roughly double the universe. Trades would come to about
48 in-sample and about 16 out-of-sample — **against the required 60**.

So universe width is not the main frequency knob. What binds are two
setup checks: candle size in ATR passes 2.1%, funding
at an extreme — 10.9%. Calibrating them is the next step, and it must be done
in-sample with a sweep, like all the other thresholds.

Out-of-sample is untouched for both A and B.

---

## DEC-014 — 2026-09-15 — Exit at the cascade VWAP; three defects in the Setup A signal

**Question.** Two independent measurements on different setups, different timeframes
and different targets gave an almost zero target hit rate (B — 1.1%,
A — 4.2%). This could no longer be a coincidence: the defect is in HOW
exits are defined, not in a particular number.

**What was done before choosing the target.** The distribution of the price move
after entry was measured — the maximum in our favour before the time stop, in R units,
for every in-sample trade. This is the question that should have been asked
before writing the spec: what target is reachable at all on this horizon.

### Move distribution, in-sample 2024-08-07 — 2026-02-06

| | p10 | p25 | median | p75 | p90 | max |
|---|---|---|---|---|---|---|
| **Setup A**, 24 trades, 15m, time stop 24h | 0.03 | 0.37 | **1.28** | **1.73** | 2.59 | 5.00 |
| same, ignoring the stop | 0.19 | 0.75 | 1.28 | 1.73 | 2.59 | 5.00 |
| **Setup B**, 1174 trades, 4h, time stop 48h | 0.06 | 0.16 | **0.36** | **0.70** | 1.32 | 7.66 |
| same, ignoring the stop | 0.06 | 0.16 | 0.36 | 0.70 | 1.32 | 7.66 |

**Setup B's 3R target sat at the 98.7th percentile of its own move.**
A 1.1% hit rate is not an anomaly but an arithmetically expected consequence.
The trend-continuation hypothesis is still not refuted: it should be tested
with a target of about 0.70R or a window much longer than 48 hours.

**The stop is not the constraint in either setup.** The distributions
with and without the stop coincide completely for B, and for A — from
the median upward. The target is unreachable because of the nature of the move, not because
we get stopped out earlier.

**Side note — on window length.** For Setup B the maximum move falls,
at the median, on bar 5 of 12, and at p90 on bar 12 of 12: the move
runs into the time stop before it is over. For Setup A the median is bar 61
of 96 — the 24-hour window is commensurate with the move.

### Exit at the cascade VWAP

Setup A's target is defined not as a number but as the cascade VWAP: the average price
at which the cascade closed positions, volume-weighted at the typical
price `(H+L+C)/3`. The setup's hypothesis is precisely that price returns
to it — so the target must be computed from the same data
as the cascade itself.

**The cascade window became one quantity for three uses.** Previously
there were effectively two: the OI contraction is measured over an hour, while the "stop beyond
the cascade extreme" took the extreme of ONE entry bar. A stop beyond the
extreme of the event's last candle and a stop beyond the extreme of the event are
different things, and the discrepancy did not show itself in any way: the numbers were computed,
the report was printed. Now it is the constant `CASCADE_WINDOW`, from which
all three quantities are computed.

### Three defects found along the way

None of them crashed or was logged.

1. **Lookahead in ATR.** The hourly ATR was keyed by the bar's OPEN time,
   while Bybit candles are dated by the start of the period. For an entry on the 15-minute
   bar 10:00–10:15, `at(10:00)` returned the ATR of the 10:00–11:00 hour —
   45 minutes of the future inside the stop distance and inside the candle
   size filter. The series was switched to CLOSE time, and the lookup is matched
   against the decision moment (the close of the entry bar), not
   against its open.

2. **The volume z-score window was four times shorter than declared.** It was computed as
   `days × 24` — i.e. in hourly bars — and applied to the 15-minute
   series: seven days turned into 42 hours. The window is now computed
   on the entry timeframe (672 bars).

3. **Silent truncation of the same window.** `min(lookback, len-1)` during
   warm-up substituted a window shorter than specified. A z-score over forty bars
   and one over seven hundred are different quantities under one name, and during warm-up
   the first is systematically inflated. The window is taken whole or not taken
   at all (P-001).

### Re-measuring P-005 after the fixes

| exit | trades | TARGET | P-005 | P-006 |
|---|---|---|---|---|
| before: fixed_r 2.5R, old construction | 24 | 4.2% | FAIL | — |
| after: fixed_r 2.5R | 26 | 11.5% | PASS | PASS |
| after: cascade VWAP | 24 | **70.8%** | PASS | **FAIL** |

Fixing the construction by itself raised the target hit rate
from 4.2% to 11.5% at the same 2.5R target: a large part of the "unreachability"
was a consequence of the ATR lookahead and the inflated z-score, not of the nature
of the move.

**The VWAP exit failed P-006** — see the principle. The median distance
to the target is 0.38R against a minimum of 2.0R; 37.5% of targets are below the cost threshold
`min_move_pct`. E is exactly zero at a 73.9% win rate.

**Conclusion on exits.** The "target at VWAP" construction is correct as a definition,
but with the current stop it gives a target that does not pay off: the stop sits
beyond the cascade extreme plus 1.5 ATR, and in R terms the VWAP ends up
right next to the entry. Choosing between "move the target" and "move the stop"
on the same data is not allowed — that is fitting. Both variants remain
in the code under `exit_mode`; neither of them is the working one under the rule set
BEFORE looking at the data (75th percentile of the move = 1.5R).

**What was deliberately NOT done.** A grid search over the target picking the best E
in-sample. That is exactly the fitting P-005 forbids, and the order
"distribution → rule → target" exists precisely to prevent it.

### The main limitation: 24 trades in 18 months

With 142 admitted symbols Setup A gives 24 trades in a year and a half.
The standard error of E on such a sample is on the order of 0.25R; telling
an edge apart from zero is impossible with any target. **The next question is
frequency, not the target**, and until it is resolved any numbers for A remain
diagnostics of the construction.

Out-of-sample is untouched for both A and B.

---
## DEC-013 — 2026-08-08 — Setup B rejected

**Final. B's parameters are not tuned, and variations of B are not tested
on this data: the out-of-sample has been spent.**

**Result: 4 of 5 thresholds failed.**

| threshold | required | actual | verdict |
|---|---|---|---|
| E on out-of-sample | > +0.15R | −0.093R | FAIL |
| trades on OOS | ≥ 60 | 320 | PASS |
| drawdown p95 | < 40% | 201.8% | FAIL |
| survives at 0.10% | E > 0 | −0.098R | FAIL |
| does not rely on 1-2 symbols | E > 0 without the two best | −0.116R | FAIL |

**The key number is not OOS but in-sample: +0.034R.** There is no edge
even on the data where tuning the parameters is allowed. This is not
overfitting — that would give a high in-sample and a low out-of-sample.

**But it is not a refutation of the hypothesis either.** Breaking down the
exit reasons shows that it was not the hypothesis that got tested.

**The structural cause, found by breaking down the exits.**

| close reason | share | mean R |
|---|---|---|
| time stop | **90.2%** | +0.069 |
| stop | 8.6% | −1.017 |
| 3R target | **1.1%** | +2.982 |

The target is reached in one trade out of a hundred. The setup works not as
"trend continuation to 3R" but as "hold for 48 hours and take whatever
comes", and the mean result of such holding is statistically indistinguishable
from zero.

The cause is that the target and the time stop are incompatible: a 3R move
on 4-hour bars within 12 bars is rare.

**How to state the result.** The trend continuation hypothesis is
NOT REFUTED — the setup's construction did not allow testing it.
What was measured was a random 48-hour hold, not a move to the target.

The difference matters for the future: the question "does the trend continue
with rising OI and neutral funding" remains open. It has to be tested
differently — with a time stop compatible with the target, or with a target
reachable within the holding window.

A defect in how the setup was formulated, not in the data, the implementation
or the market. Formalized as P-005.

**Why a regime explanation does not work.** The correlation of monthly E
with the universe's median turnover is **+0.098** — there is no link. Limitation 8
(OOS is structurally quieter) does not explain the drawdown. Walk-forward gives 6
positive windows out of 18 with no tie to regime: the dispersion is random.

**What was checked and turned out to be irrelevant:**

* costs — the slippage sweep is flat (−0.090 at 0.02%
  vs −0.109 at 0.20%): the strategy is not killed by costs, it
  was never alive in the first place;
* marginal participants — 1.8% of trades, E changes from +0.007 to +0.008;
* concentration — without the two best symbols the result gets WORSE
  (−0.093 → −0.116), i.e. it is uniformly bad, not because of a couple of pairs.

**A side finding that matters for the next setups.** The Risk Manager
filtered out 41% of signals, and the dominant reason is not MIN_NOTIONAL (146) but
**LIQ_BUFFER (885)**. The structural stop beyond the 20-bar extreme turns out to be
so far away that the requirement of a threefold buffer to liquidation rejects it.

Hence a rule for designing setups with a $40 deposit: the distance
to the stop is not a free strategy parameter but a hard constraint of the
Risk Manager. A stop further than about 8% from entry cannot pass at any
leverage.

---

## DEC-012 — 2026-08-08 — Stream coverage threshold: 80%

**Fixed BEFORE the run**, in the same way as the acceptance thresholds.
Changing it after the first results is equivalent to curve-fitting.

**Question.** A stream may formally exist for a symbol yet be so full
of holes that it is unusable. Where to draw the line?

**Why this is a separate quantity at all.** Completeness by presence
and completeness by percentage are different things. The `effective_start` check
answers the question "is there a stream", and a series with 40% of the bars answers
it in the affirmative. But such a series is unfit both for ATR and for detecting
a breakout: the indicator window silently stretches over twice the period.

**Denominator.** The expected number of records FOR THE PERIOD, not the range
of the data itself. A symbol with two months of history inside a two-year window
has 100% coverage of its own range and 8% coverage of the period. The first
figure is misleading precisely because it looks perfect.

**Chosen: 0.80.**

The rationale rests on the measured picture, not on taste. Actual
gaps after the repair are fractions of a percent, i.e. a healthy symbol has
coverage of about 99%. The 80% threshold cuts off not "slightly holey" series
but qualitatively different cases: a symbol that traded for only part of the period,
or a stream with multi-day outages.

Setting the threshold higher (say, 95%) would mean cutting symbols
for normal market noise. Lower (50%) would let through series on which
indicators cannot be computed.

**A third knob for universe composition.** There are now three, and all three
go into the Phase 2 sensitivity check:

| knob | sweep values |
|---|---|
| slippage (DEC-007) | 0.02 / 0.05 / 0.10 / 0.20% |
| liquidity threshold (DEC-009) | $10M / $25M / $50M |
| **stream coverage** | **70% / 80% / 90%** |

A strong dependence of the result on where this line is drawn
means it rests on holey symbols — and that is an independent
sign of fragility, regardless of whether the acceptance thresholds are passed.

**Implementation.** The value lives only in the config; the module reads it
from there. Duplicating it in code would create a second source of truth
that would one day diverge from the first. The config requires the base
value to be included in the sweep — otherwise the base run would fall outside it
and there would be nothing to compare against.

---

## DEC-011 — 2026-08-07 — Walk-forward as a third slice

**Question.** Limitation 8 (DEC-010) established that out-of-sample is
a structurally quieter market than in-sample. So a drawdown on OOS
will have two equally plausible explanations: overfitting
and regime sensitivity. After the fact, the convenient one will be picked.

**Chosen:** add a rolling window — 6 months of training, testing on the
following month, a one-month step. Over a 24-month period this gives **18 windows**
against the stated minimum of 12.

**What this distinguishes.**

| observation | conclusion |
|---|---|
| E positive in most windows, failures fall on quiet months | regime sensitivity — can be captured by a filter |
| E jumps around with no link to regime | overfitting |
| E negative in most windows | the setup does not work |

A single OOS run does not distinguish these cases: it gives one number,
and any explanation fits it. Eighteen estimates spread across
different regimes give a distribution — and a distribution shows
whether the dispersion is random or tied to the state of the market.

**The acceptance thresholds do not change.** Walk-forward is a diagnostic, not a
replacement. All five standard thresholds remain in force at their previous
values. There is no softening, and this matters: the third slice was
added to UNDERSTAND the result better, not to accept it more easily.

**Limitation of the method: the windows overlap.**

6 months of training shifted by a month means that adjacent windows
share 5 months of data. The eighteen estimates of E are **not independent**,
and ordinary statistics on them will overstate significance: the effective number
of observations is closer to three or four than to eighteen.

The scheme is kept anyway: without overlap there are not enough windows at all.
But conclusions are limited to the sign and the dispersion; confidence intervals,
t-statistics and p-values over these windows are forbidden.

The warning is printed in the report itself, next to the results:
a month from now the context will be forgotten, and a table of eighteen numbers
looks like a ready-made sample.

**How to read a divergence between slices.**

Limitations 8 and 9 act in the same direction — both discount
the out-of-sample:

| picture | conclusion |
|---|---|
| failure on all three slices | unambiguous rejection |
| failure only on OOS with a decent walk-forward | investigate what broke |
| failure only on walk-forward | overfitting, rejection |

The acceptance thresholds do not change and remain mandatory. This is
about which conclusion to draw when the slices diverge, not about lowering
the bar.

**Additional check — correlation of E with liquidity.**
Monthly E is compared with the universe's median turnover for the same
month. A coefficient above 0.5 means this is not a strategy but a bet
on how busy the market is.

This must be known before Phase 3, not after: we will start trading precisely
in a quiet regime, and a strategy that lives only on high liquidity
will show something different on real money from what it showed on history.

---

## DEC-010 — 2026-08-07 — Universe liquidity is falling; a trade-off, not a defect

**Question.** Replacing the absolute threshold with a rank (DEC-009) evened out
the universe width. Did it shift the problem onto the composition?

**Measured on universe snapshots, 24 full months.**

Composition churn is stable:

| period | retained from the previous month |
|---|---|
| whole period | median 73% (65–83%) |
| in-sample | 73% |
| out-of-sample | 72% |

On this axis the periods are comparable.

The universe's median turnover differs by a factor of two:

| period | median of medians |
|---|---|
| in-sample | $117M/day |
| out-of-sample | $57M/day |
| ratio | **0.49** |

**But this is not an artifact of the IS/OOS boundary.** The monthly breakdown shows
a continuous decline: a peak of $200–228M in December 2024 — January 2025, then
a monotonic decrease to $51–65M in 2026. The in-sample itself spans the
range from $69M up to $228M and back down to $66M. The period boundary
simply fell in the middle of a decline that was already under way.

**No selection rule will make 2026 as liquid
as 2025.** The choice is this:

| rule | what is constant | what drifts |
|---|---|---|
| absolute threshold | liquidity quality | universe width |
| rank | universe width | liquidity quality |

You cannot have both at once — this is a property of the data, not a flaw
of the implementation.

**Chosen:** keep the rank (DEC-009), because the second quantity is
already under our control and the first is not.

Slippage is covered by the sweep from DEC-007: a run at 0.02%,
0.05%, 0.10%, 0.20% answers the question "will the strategy survive worse
execution" directly, not through a proxy. Moreover, even $57M/day
is 800 000 times our $70 notional, i.e. the difference between
$117M and $57M has no practical meaning for a position of our size.

Width, on the other hand, cannot be compensated for: fewer symbols mean fewer
opportunities and worse diversification, and this goes straight into the result.

**A mandatory consequence for reading the results.** Out-of-sample is
a structurally quieter market than in-sample. If Setup B shows
a worse result in OOS, that may be the regime rather than overfitting,
and a single run cannot tell one from the other.

Therefore the backtest report prints the universe's median turnover
by month — next to the equity curve, so the relationship is visible by eye.

Recorded in "Known backtest limitations" as item 8.

---

## DEC-009 — 2026-08-07 — Liquidity threshold: $50M → $25M

**Adopted BEFORE the first strategy run and before any result figure
existed.** The reason is not the results — there are none — but the
comparability of the samples.

**Question.** The $50M/day threshold gave 13–30 symbols per month. Is such
a universe sufficient for a statistical test?

**Measured on the collected data (2 years, 4h, 780 symbols).**

Universe width by month:

| threshold | median | min | max | unique |
|---|---|---|---|---|
| $10M | 30 | 1 | 30 | 143 |
| $25M | 30 | 1 | 30 | 142 |
| $50M | 30 | 1 | 30 | 121 |
| $100M | 15 | 5 | 30 | 69 |

The median hides the main point. Breakdown by period at $50M:

| period | symbols/month | breakout episodes/month |
|---|---|---|
| in-sample (2024-09…2026-01) | ~30 | 284 |
| out-of-sample (2026-02…2026-08) | 13–23 | 157 |

**The number of trades was never at risk.** The throughput ceiling is
2 positions × (30 days / 48h time stop) = 30 trades per month, i.e. 180
over the six months of OOS. The acceptance threshold of 60 trades is 33% of the ceiling.
Even with 13 symbols there are about 124 breakout episodes a month, four times the
ceiling. The binding constraint is the position limit, not the universe width.

**The real problem is the threshold's procyclicality.** An absolute threshold
in dollars lets in more symbols in busy periods and fewer
in quiet ones. 2026 is quieter than 2025, so the OOS universe is systematically
narrower than the in-sample one: 17 vs 30 symbols, 157 vs 284 episodes.
Results on such samples cannot be compared — the difference in the reference
set would enter the result as a difference in setup quality.

**Chosen:** $25M/day.

**Why exactly this value.** At $25M the rank binds before liquidity does
in almost every month, so the universe is exactly 30 in both IS and OOS —
the periods become comparable. $10M gives the same, but without headroom:
any further drop in turnover will make the threshold binding again.

The point of an absolute threshold is still preserved. Our notional is $70;
at a turnover of $25M/day the position is 0.0003% of daily volume.
The $50M threshold was calibrated for an account several orders of magnitude larger
and is excessive for our size.

**What this does not cancel.** The threshold sensitivity check in Phase 2
remains mandatory: the run is repeated at $10M, $25M and $50M,
and the divergence of the results goes into the report. Changing the threshold now
removes a known distortion; it is not picking a convenient value.

**Limitation.** The even width of 30 is achieved because the rank is binding.
So the universe is now determined mainly by `top_n`, and the liquidity
threshold works as insurance against degenerate cases. This is
a deliberate trade-off: comparability of periods matters more than fine-tuning
liquidity at our position size.

---

## DEC-008 — 2026-08-07 — TimescaleDB compression and schema migrations

**Question.** `orderbook_snapshots` has compression enabled for chunks older
than a week. TimescaleDB restricts schema changes on compressed tables —
how much will this get in the way of migrations in Phase 3?

**Tested on the live DB** (TimescaleDB 2.17.2, PG16), not from the documentation:

| operation | result |
|---|---|
| `ADD COLUMN` | works |
| `DROP COLUMN` | works |
| `RENAME COLUMN` | works |
| `ALTER COLUMN TYPE` | **error** |

**The key clarification.** Decompressing the chunks is not enough. The check is
on the `timescaledb.compress` setting itself, not on the presence of compressed
chunks: with a fully decompressed table but compression still enabled, a type
change is still rejected. The first version of the workaround was written
with exactly this mistake and looked like it worked, because `psql -f` without
`ON_ERROR_STOP` keeps going after an error — the "success" at the end of the output
was printed regardless of the outcome.

**Decision.** `migrations/helpers.py::alter_compressed_table` runs the
verified sequence: drop the policy → decompress chunks →
disable compression → apply the DDL → enable compression → restore the policy.
The compression settings and the policy interval are read from the catalog, not
passed in the call: otherwise a migration could silently restore the wrong
configuration.

The procedure runs inside a single transaction — this matters, because
Alembic wraps a migration in exactly that way.

**Scope.** Column type changes only. For ADD/DROP/RENAME there is no
need to call the helper: they work directly, and decompressing a large
table costs minutes and disk space.

**Design consequence.** Compression is enabled only on
`orderbook_snapshots` — the research dataset that grows faster than
any other. It should not be enabled on the Phase 3 trading tables without need:
the cost of one awkward migration exceeds the gain in storage volume.

---

## DEC-007 — 2026-08-07 — Spread: a diagnostic, not a filter

**Question.** Passing the minimum notional says nothing about whether
a stop will fill at the expected price. Is a second universe filter needed,
based on a spread estimate from OHLC (Corwin–Schultz or Abdi–Ranaldo)?

**Options.**
1. Spread as a universe filter, threshold in the config
2. Spread as a diagnostic; the universe filter uses only turnover and age
3. Do not compute the spread at all

**Chosen:** option 2.

**Why the filter was rejected.**

First, it measures the wrong thing. What hurts us is not the resting spread
but stop slippage at the moment of a sharp move. The OHLC estimate gives
the average book width in calm times — an answer to a different question.

Second, spread and turnover are strongly correlated. A second threshold
built on the same data adds a knob for curve-fitting with almost
no new signal.

So `max_median_spread_pct` has been removed from the config entirely. A dead
threshold is worse than a missing one: it looks like it is working.

**Where the protection moved.** Into the cost model. Slippage is a
constant in the config, and Phase 2 requires a sensitivity run
over 0.02%, 0.05%, 0.10%, 0.20% as a separate section of the report.

Read it like this: **if the strategy is alive only at 0.02%, that is the
backtest result, more important than any win rate.** It means it rests
on an execution assumption that will not hold for our $70 position.

The config requires the base `slippage_pct` to be included in the sweep:
otherwise the base run would fall outside it and there would be nothing to compare against.

**Diagnostic.** Abdi–Ranaldo (2017) is computed and written to a separate
column: distribution across the universe, outlier search. DEC-001 is not
violated by this — the setup definition does not depend on the value, so
replacing the estimate with the real spread will not require a re-backtest.

**Limitation of the method, revealed by the tests.** The estimate degenerates when
the noise of the range midpoint is comparable to the useful signal `s²/4`. With
bar volatility around 1% and a spread of 0.1%, the signal is two orders of magnitude
below the noise, and the covariance comes out negative. In that case it returns
`None`, not zero: zero would read as "there is no spread" — the opposite of
"could not be estimated" (P-001). The figures are meaningful only together
with the `reliable` field.

**Measuring reality.** Alongside liquidations, the WS collector writes order book
snapshots once a minute for the universe symbols (a separate table, it does
not affect the logic). In six months this will give the real spread distribution
and a way to check how well the estimate matched reality.

---
## DEC-006 — 2026-08-07 — Source for enumerating delisted pairs

**Question.** Bybit keeps the history of dead pairs, but `instruments-info` does
not list them. Where do we get the list of names, and how do we check it is complete?

**Options.**
1. The `public.bybit.com/trading/` archive — per-symbol directories
2. Parsing the titles of `/v5/announcements/index?type=delistings` announcements
3. An external list of symbols

**Chosen:** option 1 as the primary source, option 2 as a reconciliation.

**Why — measured, not estimated.**

| source | USDT pairs |
|---|---|
| archive | 991 |
| live in the API | 800 |
| dead | 302 |
| recovered from announcements | 254 → coverage **84.1%** |

Announcements lose every sixth pair: titles are not uniform, some
delistings are bundled as a list in a single entry, some have no symbol
in the title at all. They are unfit as the primary source.

The archive comes from the same system as the API but is enumerated differently,
so it serves as an independent check. The hypothesis that it exists came from
the person who set the task, and it was fully confirmed.

**Limitation.** The completeness of the archive itself has not been verified
against an independent source. Until such a check, the backtest report says
"delisted coverage not confirmed", not "bias removed".

**Implementation.** The enumeration is one-off and cached on disk
(`var/cache/bybit`, TTL one week); a rerun does not touch the network,
a forced refresh is done with the `--refresh` flag.

---

## DEC-005 — 2026-08-07 — How to tell crypto from tokenized stocks

**Question.** The DEC-002 filter based on a base-coin blocklist did not work:
on the very first run SOXL, SNDK, SPCX, MU, SKHYNIX, SAMSUNG passed
as crypto.

**Options.**
1. The `symbolType` field from `instruments-info`
2. A manual base-coin blocklist
3. A heuristic based on `fundingInterval` and the trading schedule

**Chosen:** option 1.

**Why.** `symbolType` is the only API field that directly encodes the asset
class. As of 2026-08-07: `""` — 505 (crypto), `innovation` — 117 (innovation-zone
crypto), `stock` — 174, `commodity` — 4.

A manual list is doomed to lag: there are already 174 stocks, and every new listing
would require a code change. That is exactly how the filter leaked on the first run.

The base-coin blocklist is kept as a second barrier — in case the exchange
introduces a new asset class and does not set `symbolType`. An unknown type
is rejected by default so that a new class does not slip through silently.

**Consequence.** Of 688 USDT perps, 513 crypto pairs remain.

**A note on trusting the exchange.** `HEIUSDT` is marked `""`, although the ticker
resembles HEICO stock. The exchange's classification is accepted as is: guessing
from the ticker spelling would bring back the very manual fragility that this
decision was made to eliminate.

---

## DEC-004 — 2026-08-07 — Exchange client: ccxt or native REST

**Question.** The spec assumes "ccxt + native REST for specific
endpoints". Do we need both?

**Options.**
1. A native client everywhere, ccxt is never used
2. Native in Phase 1, defer the execution question until Phase 3
3. A thin interface behind which Phase 3 chooses the implementation

**Chosen:** option 1, final, with no deferred question.

**Why.** Option 2 was rejected on a direct objection: a deferred choice is
not a decision but a planned drift toward two abstractions, which is worse
than either option on its own.

Everything the system needs — `instruments-info`, `risk-limit`,
`funding/history`, `open-interest`, `kline`, `announcements` — is specific
to v5 and unevenly covered by ccxt. The extra layer gets in the way of debugging
cursor pagination and rate-limit accounting, which is exactly what makes up
the main complexity of data collection.

Option 3 was rejected because data collection and execution work
with disjoint sets of endpoints: a shared interface over both would
be fictitious with one exchange and one implementation.

**One abstraction will still appear, but for a different reason.** In Phase 3
an `ExecutionGateway` is introduced — a 5–6-method protocol covering only
execution. Its justification is the spec §14 requirement for a mock exchange for
integration tests, i.e. substitution with a test double. Not portability
and not "so that ccxt could be chosen".

**Status:** confirmed.

---

## DEC-003 — 2026-08-07 — Forming the universe for the backtest

**Question.** Today's top 30 by turnover is survivorship selection; the backtest would come out
optimistic. How should the universe be formed?

**Options.**
1. Point-in-time, monthly rebalancing
2. Fix the top 30 at the start of the history (August 2024)
3. Today's top 30

**Chosen:** option 1, point-in-time monthly, with four conditions.

**Why.** Turnover sits in the `turnover` field of the candles themselves, no additional
data is required — so the correct option costs roughly
one evening of work versus systematically inflated results
for the alternatives.

**Implementation conditions.**

1. **Lagged selection.** The universe for month M is formed by turnover
   for month M−1. Never by turnover of the period in which we trade. Cover
   with a separate look-ahead test.

2. **Absolute liquidity threshold.** Not just rank: a minimum
   average daily turnover in USD as a separate condition. In the config, starting
   at $50M/day, with a check of how sensitive the result is to the parameter.

3. **Persistence in the DB.** The `universe_snapshots` table: month, symbol, turnover
   for the previous period, rank. Needed for reproducibility — otherwise a month
   later one cannot tell why the backtest gave exactly these numbers, and a rerun
   may differ.

4. **Documenting biases.** All conditions and remaining distortions go
   into the "Known backtest limitations" section in PROGRESS.md.

**Clarification after checking the API.** Initially the delisting bias was
considered irremovable: delisted pairs disappear from `instruments-info` together
with their history. The 2026-08-07 check showed the opposite — Bybit keeps candles, OI
and funding for dead pairs (LUNAUSDT for March 2022 is served in full),
and `/v5/announcements/index?type=delistings` makes it possible to recover their names
from announcement titles.

The bias is removed. The residual risk shifts to the completeness of announcement parsing
and is measured as a coverage percentage. Recovering delisted pairs is part of
Phase 1b as a separate step.

---

## DEC-002 — 2026-08-07 — Universe composition: crypto or everything

**Question.** The top by turnover on Bybit includes tokenized stocks and commodities:
XAUUSDT, XAGUSDT, SNDKUSDT (SanDisk), SOXLUSDT (a semiconductor ETF),
SPCXUSDT, SKHYNIXUSDT, MUUSDT (Micron). Should they be included?

**Options.**
1. Crypto only, filter by `baseCoin`
2. Crypto + precious metals
3. Everything, by turnover

**Chosen:** option 1, crypto only.

**Why.** The logic of both Setups is built on funding and on cascades of
forced closures in the leveraged crypto crowd. Tokenized gold
and stocks have different pricing mechanics, and the underlying market has weekends
and trading sessions — gaps would break the ATR calculation and exchange stops.

---

## DEC-001 — 2026-08-07 — Cascade confirmation in Setup A

**Question.** The spec requires confirmation via "liquidation volume over 1h > 3σ
above the 7d mean". The check showed: Bybit has no liquidation history,
there is no REST endpoint, only the real-time WebSocket `allLiquidation`
stream. There is nothing to backtest two years on.

**Options.**
1. A proxy via a sharp contraction in Open Interest
2. Buy historical data from an aggregator (Coinglass and the like)
3. Postpone Setup A, start with Setup B

**Chosen:** option 1, with three conditions.

**Why option 2 was rejected.** Aggregators collect liquidations from the exchanges'
public streams, and Binance throttles its stream to one event per second.
During a real cascade — that is, exactly at the moment we
care about — the data are understated by an order of magnitude. There is no point paying for them.

**Implementation conditions.**

1. **The proxy is a definition, not an approximation.** The trigger is formulated
   as "a sharp OI contraction over 1h in the direction of the price move", with no
   mention of liquidations. Live trading uses exactly the same definition. Substituting
   real liquidations in Phase 5 is forbidden — it would make the backtest diverge
   from live trading. A switch to real data, if it happens, means
   a full re-backtest from scratch, not a "refinement".

2. **Thresholds are fixed in the config before the run.** The OI drop percentage over 1h,
   the minimum candle size in ATR, the direction, volume requirements. All of them are
   in-sample parameters, tuned only on the first 18 months.

3. **Check the real depth of OI history for each symbol separately**,
   not merely that the endpoint works.

**Result of checking condition 3.** There is no 6-month limit: OI is served
from the symbol's listing date (BTCUSDT since 2020-07-31). For some pairs OI starts
earlier than the 15m candles — the bottleneck is the candles, not OI. Of the checked
top symbols, only HYPEUSDT is shorter than the in-sample window (listed 2024-11-30).

**Order of work.** Setup B is run through completely first — its data are clean
and unambiguous, so it is an honest test of the backtest engine itself. Only
then A.

**The WebSocket liquidation collector** starts in parallel from Phase 1b, writing
from today into a separate table. It takes no part in the trigger and does not affect
the logic. A research dataset: in six months we will see whether
the OI trigger correlates with actual cascades.
