# PROGRESS

Multi-agent trading system, Bybit USDT Perpetual. Deposit $40.

Last updated: 2026-09-24

---

## Current status

**828 tests, ruff clean, no migration drift.** Decisions up to DEC-041.

### Where we are on the chain "collect → analyze → place an order"

| link | state |
|---|---|
| collection | ready; history is being deepened to 5 years (DEC-018) |
| analysis | ready; battery of checks, funnel, sweeps, null hypothesis |
| risk | single-trade sizer + portfolio governor (DEC-017) |
| orders | plan with provable rounding, executor, reconciliation with the exchange |
| sending to the exchange | **Phase 5 (DEC-041)**: daily cycle; paper at market prices and Bybit testnet. Mainnet is closed by construction |

### Setups

| setup | status |
|---|---|
| A — OI squeeze | **rejected**, hypothesis refuted (DEC-016) |
| B — trend, structural stop | **rejected**, the design did not test the hypothesis (DEC-013) |
| C — trend, ATR stop | **rejected** on deep history, 4 of 5 thresholds (DEC-032) |
| D — time-series momentum, daily bars | **rejected** in-sample: E +0.044R, 1.27 SE above the coin flip (DEC-035) |
| E — rule D at $100 | **rejected** on the E threshold: +0.024R; the signal is real — **2.63 SE above the coin flip** (DEC-039) |
| F — funding against the crowd | **rejected**: −0.047R, 1.85 SE worse than the coin flip (DEC-043) |
| G — rank momentum | **rejected**: +0.011R, indistinguishable from the coin flip (+0.74 SE) (DEC-045) |
| H — reversion after an extreme day | **rejected**: −0.097R, 65 trades, −0.28 SE (DEC-047) |

**Setup C, in-sample 2024-08 — 2026-02, best case:**

| threshold | required | actual | |
|---|---|---|---|
| E on OOS | > +0.15R | +0.062R | FAIL |
| trades | ≥ 60 | ~366 | PASS |
| drawdown p95 (population) | < 40% | 45.5% | FAIL |
| survives at 0.10% | E > 0 | +0.050R | PASS |
| without the two best | E > 0 | +0.032R | PASS |

Over random entry with the same exit design: **+2.27 σ**
(DEC-021) — an edge apparently exists, but it is less than half
of what was named sufficient in advance.

### What is running now — forward paper (DEC-041)

Setup E trades on paper at main-market prices: deposit $100,
risk 1.5% ($1.50) per trade, position $5–30, up to four positions (DEC-050), exit
after 7 days or at the stop. The goal is to collect the out-of-sample set
that was missing (DEC-039): the same thresholds on ≥ 60 forward trades.

```bash
python -m src.cli web                  # panel: http://127.0.0.1:8765
python -m src.cli trade-status         # the same in the terminal
VENUE=testnet scripts/daily.sh         # mechanics on testnet (keys in .env)
```

It starts on its own: launchd every hour at :10 (`scripts/scheduled.sh`),
the cycle runs once per UTC day, entry window 03:10–05:00 MSK. Docker with the database
is brought up by the script. The readable journal of runs and failures is
`var/live/journal.md`; detailed logs are `var/live/*.log`.

### What ran earlier

The `scripts/deepen.sh` pipeline: light 4h for 815 candidates →
point-in-time universe rebuild over 5 years → full streams for it →
availability matrix.

Completed. After the top-up download (DEC-036, DEC-037) the data is complete relative to
the exchange itself: funding is almost three times fuller than before, no gaps in candles.

### What's next — per the plan written down before the data (DEC-028)

1. `validate-setup --period deep_history` — 2021-11 — 2024-08, a different
   market regime, configuration frozen at `a6ff492`;
2. failing any threshold — Setup C is rejected, the recent OOS is preserved;
3. passing — `validate-setup --period recent_oos`, once.

All three rules are enforced by code, not by agreement (DEC-030).

### The main constraint — account size (DEC-037)

At $40, the exchange minimum order ($5) at 1.5% risk caps the stop at
12% of price — and cuts off 84% of days for a daily strategy with a normal
stop (the median 2×ATR stop is 17.8%). Around $100–200 the constraint
stops shaping the strategy. This does not create an edge —
it only makes strategies testable as designed.

---

## How to run

```bash
cp .env.example .env          # change the DB password
docker compose up -d
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/alembic upgrade head
.venv/bin/python -m src.cli universe-report --limit 30
.venv/bin/python -m pytest -q
```

Bybit keys are not needed in Phase 1: all endpoints used are public.

---

## Done

### 2026-08-07 — Bybit public API reconnaissance

A run against the live API before writing code, to check the spec's premises. The
reconnaissance scripts are temporary and are not part of the repository.

**Minimum trade size is not a problem.**

The spec claimed that BTCUSDT has a minimum notional of 100 USDT and BTC is unavailable.
In fact `minNotionalValue` = 5 USDT for all checked pairs, including BTC.
The constraint comes from `minOrderQty`: 0.001 BTC × $65 036 = $65 effective
minimum.

Hence the rule for the Instrument Filter: compute
`effective_minimum = max(minNotionalValue, minOrderQty × price)`,
and separately the granularity `qtyStep × price`.

At a $70 notional practically everything is tradable. The exception is BTCUSDT: the lot step is
$65, the position can only be whole, partial exits are impossible. This is the real
reason to exclude BTC, different from the one stated in the spec.

| symbol | min. entry | position step | splitting $70 |
|---|---|---|---|
| BTCUSDT | $65.08 | $65.08 | whole — exclude |
| ETHUSDT | $19.28 | $19.28 | 3 parts |
| SOLUSDT | $7.38 | $7.38 | 9 parts |
| XRPUSDT | $5.00 | $0.10 | 676 parts |
| BNBUSDT | $5.92 | $5.92 | 11 parts |

**MMR is lower than the spec assumed.** For SOLUSDT the first risk tier gives
`maintenanceMargin` = 0.005 (0.5%), tier limit $50 000. The spec assumed 1% for
alts. We are always in the first tier. Pull it from the API, do not hardcode.

**Bybit has no liquidation history.** No REST endpoint exists,
`/v5/market/liquidation` is empty. Only the WebSocket `allLiquidation` in real
time. See decision DEC-001.

**OI depth is not limited to six months.** The hypothesis was not confirmed: OI
is served from the symbol's listing date. BTCUSDT from 2020-07-31, LINKUSDT
from 2020-10-16.

For some symbols OI starts **earlier** than 15m candles — we are limited by
candles, not by OI:

| symbol | kline 15m from | OI 1h from |
|---|---|---|
| BTCUSDT | 2020-03-20 | 2020-07-31 |
| ETHUSDT | 2021-03-10 | **2020-10-16** |
| UNIUSDT | 2021-11-25 | **2021-03-13** |
| SOLUSDT | 2021-10-10 | **2021-06-25** |
| HYPEUSDT | 2024-11-30 | 2024-11-30 |

HYPEUSDT (11th by turnover today) was listed on 2024-11-30 — it does not have 18 months
of in-sample and drops out of the in-sample set.

**History of delisted pairs is retained.** Checked on each pair's live
period: Bybit serves candles, OI and funding after delisting, although the symbol is absent
from `instruments-info`.

| symbol | period | kline | OI | funding |
|---|---|---|---|---|
| LUNAUSDT | 2022-03 | yes, close=$88.60 | yes | yes |
| FTTUSDT | 2022-06 | yes, close=$28.92 | yes | yes |
| AGIXUSDT | 2024-01 | yes | yes | yes |
| OCEANUSDT | 2024-01 | yes | yes | yes |
| SRMUSDT | 2022-03 | yes | yes | yes |
| MATICUSDT | 2023-06 | yes | yes | yes |
| MULTIUSDT | 2023-03 | yes | **no** | **no** |

MULTIUSDT shows that retention is uneven — the collector must survive
partial absence of streams for a symbol rather than treat it as an error.

**Enumeration of dead pairs — measured.** See DEC-006.

| source | USDT pairs |
|---|---|
| archive `public.bybit.com/trading/` | 991 (1870 directories in total) |
| live in `instruments-info` | 800 |
| **dead** | **302** |
| recovered by parsing announcements | 254 → **coverage 84.1%** |

Announcements miss every sixth pair, so the archive serves as the primary
enumeration source, and announcements are used only for reconciliation.

**Universe composition.** The top by turnover includes tokenized stocks and commodities.
The asset class marker was found in the `symbolType` field (DEC-005):

| symbolType | count | what it is |
|---|---|---|
| `""` | 505 | crypto |
| `innovation` | 117 | crypto, innovation zone |
| `stock` | 174 | AAPL, ADBE, AMC, SOXL, MU, SNDK, SPCX… |
| `commodity` | 4 | BZ, CL, XAG, XAU |

### 2026-08-07 — Phase 1a implemented

Done: config with validation, structlog with a secrets filter, token bucket
and backoff, async Bybit v5 client, Instrument Filter, the
`universe-report` report, the `instruments` table with a migration, 83 tests.

**Acceptance result.** Of 688 USDT perps, after the DEC-002 filter
**513 crypto pairs remain, all tradable** at a $70 notional. A partial exit is possible
for 512 of them; the only exception is BTCUSDT.

| symbol | min. entry | step | parts | MMR |
|---|---|---|---|---|
| BTCUSDT | $65.23 | $65.23 | 1 — whole | 0.50% |
| ETHUSDT | $19.31 | $19.31 | 3 | 0.50% |
| SOLUSDT | $7.38 | $7.38 | 9 | 0.50% |
| XRPUSDT | $5.00 | $0.10 | 675 | 0.50% |
| HYPEUSDT | $5.00 | $0.57 | 123 | 0.67% |

Full dump: `var/reports/universe_2026-08-07.json`.

**Found and fixed a livelock in the token bucket.** Accumulated binary fraction
error drove `tokens` to `0.9999999999999998`; the deficit came out at 2e-16,
the delay at 1e-17, and `now + wait == now`. The bucket stopped letting
requests through, spinning in a busy loop: the process looked alive and burned CPU.
It reproduced on the ninth grant at rate=20, burst=1.

Fixed with an `_EPSILON = 1e-9` tolerance in the comparison and a delay floor
`_MIN_WAIT = 1e-6`. Covered by a regression test of 500 grants
and parametrization over four configurations.

In production this would have been a nasty failure: the collector silently stops, data
stops updating, and monitoring sees a live process.

---

## Open questions

### Spread — question closed (DEC-007)

The spread filter was removed entirely, `max_median_spread_pct` was deleted
from the config. The OHLC estimate measures the average book width at rest,
while what hurts us is stop slippage at the moment of a sharp move — a different
question. On top of that, the spread is strongly correlated with turnover.

Protection moved to the cost model: a mandatory Phase 2 sweep
over slippage 0.02/0.05/0.10/0.20%. Abdi–Ranaldo remains
as a diagnostic and affects nothing.

The actual value is measured with WS order book snapshots starting today.

### Archive completeness not verified

The `public.bybit.com/trading/` archive lists 991 USDT pairs versus 800
live in the API, but its own completeness has not been confirmed by an independent
source. Until it is checked, the backtest report says **"delisted coverage
not confirmed"**, not "bias removed".

**In the backlog:** check the archive list against historical snapshots
of the Bybit instruments page via the Wayback Machine at several points in
2022–2024. If we find a pair that traded then and is missing
from the archive, we will learn the size of the gap. Not a Phase 1b priority.

---

## Phase 1 plan

### Phase 1a — infrastructure and Instrument Filter

Delivers the first practical result: the list of coins tradable at $40.

1. Repository skeleton, `docker-compose` (Postgres + TimescaleDB, Redis),
   `.env.example`
2. `core/`: config (pydantic-settings + `settings.yaml`), structlog with a secrets
   filter, SQLAlchemy 2.0 async + Alembic
3. `exchange/rate_limiter.py` — token bucket, exponential backoff
4. `exchange/bybit_client.py` — async httpx over v5: cursor pagination,
   unified `retCode` handling, retries
5. `instruments` table migration
6. `exchange/instrument_filter.py` — `instruments-info` + `risk-limit`,
   effective minimum, granularity, first-tier MMR
7. CLI `universe-report` — tradability report
8. Tests: filter math, rate limiter

### Phase 1b — collectors and backfill

9. Hypertable migrations: `ohlcv`, `funding_history`, `open_interest`,
   `liquidations`, `universe_snapshots`
10. 15m/1h/4h, funding and OI collectors — incremental, idempotent,
    with checkpoints, surviving partial absence of streams
11. Recovery of delisted pairs via announcements + coverage reconciliation
12. WS liquidation collector — a separate table, a research dataset,
    does not affect trading logic (DEC-001)
13. Point-in-time universe construction with a one-month lag (DEC-003)
14. Bulk backfill, a data coverage report with gaps
15. Tests: incrementality on a mock exchange, no lookahead in universe
    selection
### Phase 2 order

First we run **Setup B** all the way through — the data is clean, there is no ambiguity,
so it is an honest test of the backtest engine itself. Only after that, Setup A.

---

## Backfill speed measurement (2026-08-07)

A rehearsal on three symbols at full depth: BTCUSDT (long
history), SOLUSDT (typical), MULTIUSDT (candles present, no OI or funding).

| quantity | value |
|---|---|
| actual speed | ~2.5 req/s |
| time per symbol | ~50 s |
| rows per second | ~750–1200 |
| rows per symbol | ~111 500 |

The speed is a third of the token-bucket ceiling (20 req/s) because
the bottleneck is not the network but the writes: batches of 3–4 thousand rows
dominate the request time.

**Extrapolation:**

| symbols | time | rows | on disk before compression |
|---|---|---|---|
| 30 | ~25 min | 3.4 M | 0.34 GB |
| 100 | ~1.5 h | 11.2 M | 1.14 GB |
| 513 | ~7.4 h | 57.3 M | 5.87 GB |

The network-based `--estimate` gave 83 minutes for 513 symbols — it
counts only requests and turned out five times too optimistic. The real
limiter is the DB.

**Conclusion for the decision on the bulk run.** There is no point in running
all 513 symbols: the working universe is the union of the monthly top-30
over two years, roughly 60–120 pairs. With 100 symbols the run
fits into an hour and a half, which needs neither an overnight launch
nor splitting into stages.

---

## Setup acceptance thresholds

**Fixed on 2026-08-07, before the first run and before any result
figure appeared.**

```
E on out-of-sample          > +0.15R
trades on OOS               >= 60
drawdown, 95th percentile   < 40%
survives slippage           >= 0.10%
result does not rest on 1-2 symbols
```

**All five are mandatory. Failing any one rejects the setup.**

### Mandatory backtest report blocks

Always computed, regardless of whether the thresholds were passed.

1. **Sweeps over the three universe-composition knobs.** Slippage
   0.02 / 0.05 / 0.10 / 0.20% (DEC-007), liquidity threshold
   $10M / $25M / $50M (DEC-009), stream coverage 70% / 80% / 90%
   (DEC-012). A strong dependence of the result on any of them is
   a sign of fragility.
2. **Median universe turnover by month** next to the equity curve
   (DEC-010).
3. **Contribution of marginal entrants** — the 22 symbols from
   `config/marginal_entrants.txt`: their share of trades, separately for IS
   and OOS, plus E with the full universe and E without them.

   They are not excluded up front — they pass selection legitimately. But if
   the result changes noticeably without them, it rests on marginal
   entrants, and that is a sign of fragility in its own right.
4. **Walk-forward** with the window-overlap warning (see below).
5. **Correlation of monthly E with the median turnover** of the universe.

---

**Third view — walk-forward (DEC-011): a diagnostic, not a replacement.**
Rolling window: train on 6 months, test on the next one, step of one month —
18 windows over the period. The thresholds above are not changed by it.

It is needed because OOS is structurally quieter than in-sample (limitation 8),
and a single run cannot tell overfitting from regime
sensitivity. It is read from the distribution of E:

| observation | conclusion |
|---|---|
| E > 0 in most windows, dips in quiet months | regime sensitivity |
| E jumps around with no link to regime | overfitting |
| E < 0 in most windows | the setup does not work |

Plus the correlation of monthly E with the median universe turnover:
above 0.5 means a bet on market liquidity, not a strategy.
We need to know this before Phase 3 — we will start trading in a quiet regime.

> **The windows overlap — do not build statistics on them.**
>
> Training on 6 months with a one-month shift means adjacent windows
> share 5 months of data. The eighteen estimates of E are **not independent**,
> and ordinary statistics on them will overstate significance.
>
> The overlap is justified: without it there are not enough windows. But conclusions are limited
> to **sign and spread**. Confidence intervals, t-statistics
> and p-values must not be computed over these windows.
>
> The warning is printed in the report itself next to the results,
> not only here.

### How to read a divergence between views

Limitations 8 and 9 act in the same direction: both make
out-of-sample cheaper. The consequence for interpretation:

| picture | conclusion |
|---|---|
| fail on all three views | unambiguous rejection |
| fail only on OOS with a decent walk-forward | investigate what broke — not an automatic pass |
| fail only on walk-forward | overfitting, rejection |

**This is not a softening of the thresholds.** The thresholds keep their values
and are mandatory. This is about which conclusion to draw when the views disagree
with each other.

After the run these numbers are not revised. If, after the results, there is an urge
to soften them — that itself is the sign that the setup did not pass, not a reason
to change a threshold.

The meaning of each:

* **E > +0.15R.** Positive expectancy with a margin for real
  costs turning out higher than modelled. Exactly zero is a loss
  after the very first unaccounted-for detail.
* **>= 60 trades.** With a smaller sample the spread swamps the signal:
  on 20 trades a positive E comes out by chance far too often.

  Reachability has been checked. The throughput ceiling is set by the
  position limit, not by the width of the universe:

  | average holding | trades/month | over 6 months OOS | margin to threshold |
  |---|---|---|---|
  | 6h | 240 | 1440 | 24× |
  | 12h | 120 | 720 | 12× |
  | 24h | 60 | 360 | 6× |
  | 48h (time stop) | 30 | 180 | 3× |

  Even in the worst case, where every position is held until the time stop,
  the margin is threefold. Signals meanwhile exceed the ceiling by an order of magnitude:
  9.5 breakout episodes per symbol per month with 30 symbols.
* **Drawdown p95 < 40%.** Computed by Monte Carlo permutation of the
  trade order (spec §7). With a $40 deposit a 40% drawdown is $16,
  after which continuing is pointless.
* **Survives slippage >= 0.10%.** From the DEC-007 sweep. If the
  strategy is positive only at 0.02%, it rests on an execution
  assumption that a $70 position will not have.
* **Does not rest on 1-2 symbols.** Remove the two most profitable symbols;
  if E drops to zero or below, it is not a strategy but a coincidence
  on a specific pair. Checked explicitly, in a separate run.

Order of testing — Setup B first: its data is clean and there is no
ambiguity, which makes it an honest test of the backtest engine itself.

---

## Known backtest limitations

This section must be read before interpreting any results.

### 1. The cascade trigger is a definition, not an approximation

Setup A does not use liquidations. The trigger is defined as **a sharp contraction
of Open Interest over 1 hour in the direction of the price move**. Exactly this definition
is used both in the backtest and live — identically.

Substituting real liquidations in Phase 5 is forbidden: it would make the backtest diverge
from live trading. A switch to real data, if it ever happens,
means a full re-backtest from scratch, not a "refinement".

### 2. Setup A thresholds are fixed before the run

The percentage OI drop over 1h, the minimum candle size in ATR, the direction,
the volume requirements — all in the config, fixed before the first run.
These are in-sample parameters, tuned only on the first 18 months.

### 3. Lagged universe selection

The universe for month M is built from turnover in month M−1. Never from turnover
of the period being traded. Covered by a dedicated lookahead test.

### 4. The liquidity threshold is absolute, not only rank-based

The top-30 by rank in a sluggish period may include pairs on which our stop
would slip. A minimum average daily turnover is a separate condition,
in the config, starting at $50M/day, with a check of the result's sensitivity
to the parameter.

### 5. Survivorship bias — removable

Originally considered irremovable. A check on 2026-08-07 showed that Bybit
keeps the history of delisted pairs (LUNAUSDT for March 2022 is served in full),
and the `public.bybit.com/trading/` archive lists them by name.

**Measured:** the archive contains 991 USDT pairs versus 800 live in the API, i.e.
302 dead. Parsing announcements recovers 254 of them — 84.1% coverage.
Therefore the archive, not the announcements, is the source for enumeration (DEC-006).

Residual risk: the completeness of the archive itself. It has not been verified against an independent
source, so until such a check the backtest report states
**"delisted coverage not confirmed"**, not "bias removed".

Additionally: for some recovered pairs the streams are incomplete (MULTIUSDT — has
candles, no OI or funding). Such symbols drop out of setups that depend
on OI.

### 6. Instrument parameters are applied retroactively

`min_notional`, `qty_step` and MMR are available only as of today — Bybit does not
serve historical ones. In the backtest we apply current parameters to past
periods. There is no practical workaround.

### 9. The 22-symbol top-up landed mostly in OOS

Lowering the threshold to $25M (DEC-009) added 22 symbols to the universe
that were not in the original backfill. Their distribution is skewed:

| period | appearances in the composition |
|---|---|
| in-sample | 5 (3–7% of the month's composition) |
| out-of-sample | 18 (5–20% of the month's composition) |

The peak is in April 2026 — 6 of the month's 30 symbols. The cause is structural:
in-sample the universe was capped by rank even at $50M, while in OOS the threshold
was binding, so the lowering picked up symbols precisely there.

All 22 are live (not delisted), rank 19–38, almost all appear only once.
So they are marginal entrants, not the core of the universe — but
they must be kept in mind when analysing OOS months.

### 8. Out-of-sample is a structurally quieter market

Median universe turnover: $117M/day in-sample versus $57M/day
out-of-sample, a ratio of 0.49. This is not an artifact of the period boundary —
the decline is continuous, from the December 2024 peak (~$220M) to $51–65M in 2026.

Composition turnover, meanwhile, is stable: 73% in IS, 72% in OOS.

Consequence: if Setup B shows a worse result in OOS, it may be
the regime rather than overfitting. A single run cannot tell one from
the other. Median universe turnover by month is printed in the report
next to the equity curve (DEC-010).

### 7. Some symbols are shorter than the in-sample window

HYPEUSDT and similar were listed after the start of the period. They drop out
of the in-sample set. The list of such symbols goes into the backtest report explicitly.

---

## Phase 2 results

### Setup B — rejected (DEC-013)

4 of 5 thresholds failed. In-sample +0.034R, out-of-sample −0.093R.

Structural cause: the 3R target is reached in 1.1% of trades, 90.2% exit
on the time stop with an average of +0.069R. The target and the time stop are incompatible —
a 3R move on 4h bars within 12 bars is rare.

Setup B's out-of-sample is spent. Parameters are not tuned,
variations are not tested.

**Clarification per DEC-014.** The cause is now stated more precisely than at rejection:
the 3R target sat at the 98.7th percentile of the setup's own move.
The median move after entry is 0.36R, the 75th percentile 0.70R. The
1.1% hit rate follows arithmetically from the distribution, not from the market
regime. The trend-continuation hypothesis remains untested.

### Setup A — exit design rebuilt (DEC-014)

Measured the distribution of the price move after entry: median 1.28R, 75th
percentile 1.73R, 90th 2.59R over a 24-hour horizon.

Three signal defects were found and fixed, none of which crashed
or was logged: lookahead in the hourly ATR (the series was keyed by the bar's open
time), a volume z-score window four times shorter than declared,
and silent truncation of that same window during warm-up.

The fix alone raised the 2.5R target hit rate from 4.2%
to 11.5% — P-005 passes. Exiting at the cascade VWAP gives a 70.8%
hit rate but fails the new guard P-006: median distance
to target 0.38R against a minimum of 2.0R, E exactly zero at a 73.9% win rate.

**The limitation that overrides everything else: 26 trades in 18 months.**
The standard error of E on such a sample is on the order of 0.25R. The P-005 verdict
"passed" on this sample is withdrawn per DEC-016: the minimum for a verdict is
30 trades; below that no verdict is issued.

Exits are closed: `exit_mode: fixed_r`, `target_r: 2.5` — the spec value,
passes both guards (11.5% target hit rate). The stop stays
beyond the cascade extreme.

Setup A's out-of-sample is untouched.
### Setup A — rejected, hypothesis refuted (DEC-016)

A trigger sweep on a sample of 756 trades: the sharper the OI contraction,
the **worse** the result. E is negative at every cascade-strength level
(−0.035, −0.045, −0.147, −0.056); there is no dose-response.

The wording differs from Setup B. B was not refuted — its
design did not allow the hypothesis to be tested. Here the design allowed the test,
and the hypothesis failed it. The question "does price revert after a
cascade of forced liquidations" is closed negatively on the top-30
crypto perpetuals over two years.

E = +0.492R at the spec parameters is 25 trades, the noisiest point of the grid.
Positive values appear only where the sample is under
a hundred; neighbouring, better-measured points contradict them.

**The Setup A out-of-sample has NOT BEEN SPENT.** There is nothing to take there:
in-sample, where tuning parameters is allowed, there is no edge.
The six months remain for the third setup.

### Frequency — where signals are lost (DEC-015)

The funnel is built. The Risk Manager turned out to be irrelevant: it passes
92.7% of signals. The losses are elsewhere:

| stage | passed |
|---|---|
| candle size in ATR | 2.1% |
| funding at an extreme | 10.9% |
| Risk Manager | 92.7% |
| symbol was in the universe that month | 18.7% |

Three defects in universe composition were found and fixed: the rank cutoff
reported itself with the code LOW_LIQUIDITY; universe width had no
mandatory sweep; tradability was judged by TODAY's price,
so rebuilding a snapshot produced a different composition.

**Widening the universe does not solve the problem.** Removing the rank
cutoff would give on the order of 16 trades on OOS versus the required 60.
The binding constraints are two setup checks — candle size and funding. Calibrating
them with an in-sample sweep is the next step.

---

## Deferred

### A third setup with independent logic — after Phase 2

Planned to raise trade frequency honestly, **without loosening the acceptance
thresholds of A and B**.

Why this is written down in advance: when setup A or B falls short on
statistics, there is a temptation to lower the 60-trade threshold or loosen
the filters. The right answer is to add an independent signal source,
not to dilute the criterion.

**Acceptance criterion — P-003:** the return correlation of the new setup
with each accepted one must be **below 0.3**. The threshold is initial and
will be refined on the first data.

Three setups firing under similar conditions give not triple the frequency
but one bet of triple size — exactly the error that position correlation control
guards against (spec §5), only at the logic level, where
it is harder to notice.

To be discussed once the results of A and B are known: before that
it is unclear which type of signal is falling short.

---

## Remaining

### Done beyond Phase 2

- portfolio risk governor: position, margin and correlation limits,
  daily and weekly stop with OFF modes (DEC-017);
- correlation source based on collected candles.

### Not done — and this blocks everything else

**There is not a single confirmed edge.** Setup B is rejected
(DEC-013), the Setup A hypothesis is refuted (DEC-016). Phases 3–7
(LLM veto, execution, live loop) are built ON TOP OF a setup
that does not exist.

The infrastructure can keep being built and tested — it is needed for
any setup. But a system put live without an edge, at a
round-trip cost of 0.16%, is guaranteed to lose money: this is not risk
but arithmetic.

The next substantive step is a third setup (P-003) with independent
logic. The outcome is unknown in advance: neither of the two setups tested
was confirmed.

- Phases 3–7 per the spec — after a setup appears that has passed the five thresholds.
