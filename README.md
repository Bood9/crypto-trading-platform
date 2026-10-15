# crypto-trading-platform

A research-first trading system for **Bybit USDT perpetual futures**: market-data
collection, preregistered strategy research with honest validation, and a
**paper-trading bot platform** — five cooperating role bots that check each
other's work — with a local web dashboard.

> **Status:** paper trading only. No strategy in this repository has passed its
> own preregistered acceptance threshold. Nothing here is financial advice or a
> promise of profit. See [Research results](#research-results).

---

## Highlights

- **Bot platform.** Every night a conductor runs five role bots as separate
  processes that hand work to each other through a Postgres "board":
  **Collector → Analyst → Risk Manager → Executor → Controller**. An entry needs
  two keys: the Executor's own plan must match the Risk Manager's approval,
  which is produced by a full rehearsal of the decision on a copy of the book.
  Exits and stops always work, even without approvals.
- **Paper venue on real prices.** Paper trades are executed against Bybit
  mainnet market data (1h bars), with slippage, taker fees and funding. Stops
  are replayed hourly. Real-money trading is blocked by construction; the order
  client accepts only the Bybit testnet endpoint.
- **Preregistered research.** Each strategy ("Setup") is written down with its
  rules, null hypothesis and acceptance thresholds *before* it is run. The
  out-of-sample window can be spent only once for all setups, and the code
  refuses to validate a setup whose config or code drifted from the frozen
  commit.
- **Data quality first.** Incremental collectors for 4h/1h candles, funding and
  open interest, with coverage reports, gap repair, delisted-symbol recovery
  (survivorship bias) and a monthly point-in-time universe.
- **Dashboard.** A read-only FastAPI + vanilla JS panel on `127.0.0.1:8765`:
  equity, positions, trades, risk limits, decision journal, the window of each
  bot (what it received, did and produced), and the path of every signal
  through the bots (signal → risk → execution → verification), with rule
  rejections and platform rejections counted separately.

## Architecture

```
launchd (hourly, :05)
   └── scripts/scheduled.sh
         ├── night shift (first run after 00:00 UTC)
         │     python -m src.bots night
         │       Collector   – refresh candles, funding, monthly universe
         │       Analyst     – signals at the 00:00 UTC decision moment
         │       Risk Manager– rehearsal on a copy → approve / reject per candidate
         │       Executor    – the only writer: exits, stops, approved entries
         │       Controller  – independent checks (shadow mode)
         └── day shift (every other hour)
               python -m src.bots day
                 Collector (held symbols) → Executor (stop watch) → Controller

Postgres / TimescaleDB  ← candles, funding, OI, universe, live book, bot board
FastAPI panel (127.0.0.1:8765)  ← read-only views over the same database
```

| Path | What lives there |
|---|---|
| `src/collectors/` | Bybit REST collectors, checkpoints, coverage, gap repair |
| `src/exchange/` | Bybit client with rate limiting, instrument filter, archive catalog |
| `src/strategy/` | Setups A–H, universe builder |
| `src/backtest/` | Engine, portfolio replay, evaluation, validation ledger, reports |
| `src/risk/` | Position sizer, portfolio governor, correlation |
| `src/execution/` | Order plans, executor, reconciliation, paper and Bybit venues |
| `src/live/` | Daily cycle, paper venue on market prices, book store, locks |
| `src/bots/` | Bot platform: roles, board, conductor |
| `src/web/` | Dashboard API and static UI |
| `config/settings.yaml` | All parameters, each with the reason it has that value |
| `DECISIONS.md` | Decision log (DEC-001 …): every design and research decision |

## Research results

Each setup was preregistered and judged against the same acceptance thresholds
(mean result per trade E > +0.15R on ≥ 60 trades, permutation drawdown p95 <
40%, survives 0.10% slippage, not carried by two symbols, beats its null
hypothesis by ≥ 2 SE).

| Setup | Idea | Verdict | Decision |
|---|---|---|---|
| A | Reversion after open-interest compression / liquidation cascades (intraday) | Hypothesis refuted | DEC-016 |
| B | Trend continuation (intraday) | Rejected | DEC-013 |
| C | Trend continuation, hypothesis test on deep history | Rejected on deep history | DEC-032 |
| D | Time-series momentum on daily bars | Rejected in-sample | DEC-035 |
| E | 28-day time-series momentum, 2×ATR stop, 7-day time exit | Real but small edge: E = +0.024R, +2.63 SE over a random-direction null; below the +0.15R threshold | DEC-039 |
| F | Funding skew, against the crowd | Rejected — worse than random | DEC-043 |
| G | Cross-sectional rank momentum, market-neutral | Rejected — no relative strength | DEC-045 |
| H | Reversion after an extreme day | Rejected | DEC-047 |

Setup E is the only one with a real, if small, edge over its null hypothesis.
Because it does not reach the acceptance threshold, it runs as a **forward
paper test** on the bot platform to collect out-of-sample trades.

## Quick start

Requirements: Python 3.11+, Docker (for Postgres/TimescaleDB), macOS or Linux.

```bash
git clone https://github.com/Bood9/crypto-trading-platform.git
cd crypto-trading-platform
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env            # no exchange keys are needed for data and paper trading
docker compose up -d postgres
alembic upgrade head
```

Collect data and build a universe:

```bash
python -m src.cli backfill --symbols universe-now --streams kline_4h,kline_1h,funding --years 1.2
python -m src.cli build-universe --month due
```

Run the bots once by hand (paper venue), then open the dashboard:

```bash
python -m src.bots night --dry      # preview: nothing is written to the book
python -m src.bots night            # paper execution
python -m src.cli web               # http://127.0.0.1:8765
```

Other useful commands: `trade-status`, `trade-resume`, `paper-monitor`,
`coverage-report`, `insample-report`, `validate-setup`, `testnet-check`
(`python -m src.cli --help`).

### Scheduling (macOS)

`scripts/launchd/` contains launchd agents for the hourly job and the panel.
Replace `__PROJECT_DIR__` with the absolute path of your checkout, copy them to
`~/Library/LaunchAgents/` and load them with `launchctl load`. The nightly
decision needs the machine awake at 00:05 UTC; on battery macOS lets the job
run only in short dark-wake windows, so keep the machine on power.

## Development

```bash
ruff check src tests migrations
pytest -q
```

The pre-commit hook (`.githooks/`, enable with
`git config core.hooksPath .githooks`) runs ruff, the test suite and a
migration-drift check.

## Safety

- Paper trading on market prices needs no exchange keys.
- The trading client refuses any endpoint except Bybit **testnet**; keys, if
  used, live only in `.env`, which is git-ignored.
- One writer per book (file lock), sticky trading halts that need a human to
  resume, write-ahead records before every order, reconciliation against the
  venue at the start of every cycle.

## License

MIT — see [LICENSE](LICENSE).

*This software is for research and education. Trading crypto derivatives is
risky; past or simulated results do not predict future returns.*
