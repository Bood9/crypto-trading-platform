#!/usr/bin/env bash
# Daily cycle (DEC-041): refresh data → decision → journal.
#
# Run AFTER 00:00 UTC and no later than 02:00 UTC: the entry window is two
# hours after the daily close (the backtest enters at the close). Later
# entries are rejected with code ENTRY_WINDOW_MISSED; exits always run.
#
#   scripts/daily.sh              # paper on main-market prices
#   VENUE=testnet scripts/daily.sh  # Bybit testnet, testnet keys in .env
#   EXECUTE=0 scripts/daily.sh    # only show the decisions
#
# Exit codes: 0 - all fine; 3 - some data is stale, the decision was made
# on the rest; 4 - trading is switched off, a human is needed
# (trade-status, then trade-resume); anything else - crashed.
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate

VENUE="${VENUE:-paper}"
SETUP="${SETUP:-e}"
EXECUTE="${EXECUTE:-1}"
LOGS=var/live
mkdir -p "$LOGS"
STAMP="$(date -u +%Y%m%d)"
mark() { echo "=== $(date -u +%H:%M:%S) $* ==="; }
# Logs are APPENDED: a retry on the same day must not erase the traces
# of the previous attempt (re-check of the night of 2026-09-28).
for f in "$LOGS/backfill_$STAMP.log" "$LOGS/cycle_${VENUE}_$STAMP.log" "$LOGS/universe_$STAMP.log"; do
  echo "##### attempt $(date -u +%H:%M:%S) UTC" >> "$f"
done

mark "data: current month universe and open positions"
# 1.2 years: the signal ATR is computed over 400 days of history
# (LIVE_HISTORY_DAYS). Collection is incremental: already collected data
# is not downloaded again.
set +e
python -m src.cli backfill --symbols universe-now --streams kline_4h,kline_1h,funding \
  --years 1.2 --concurrency 4 >> "$LOGS/backfill_$STAMP.log" 2>&1
code=$?
set -e
case "$code" in
  0) ;;
  3) echo "!!! some streams were not refreshed, see $LOGS/backfill_$STAMP.log" ;;
  *) echo "!!! collection failed with code $code, see $LOGS/backfill_$STAMP.log"
     echo "    The cycle runs anyway: reconciliation and exits do not depend on signal freshness,"
     echo "    and symbols without fresh data will be skipped with code STALE_DATA." ;;
esac

mark "universe: is it time to build a new month"
set +e
python -m src.cli build-universe --month due --check >> "$LOGS/universe_$STAMP.log" 2>&1
code=$?
set -e
if [ "$code" = "5" ]; then
  mark "universe: collecting 4h for all candidates"
  set +e
  python -m src.cli backfill --symbols candidates --streams kline_4h \
    --years 1.2 --concurrency 4 >> "$LOGS/universe_$STAMP.log" 2>&1
  code=$?
  # A full collection failure (1) leaves nothing to build from. A partial one
  # (3) is acceptable: build-universe checks selection-month completeness
  # itself, per symbol.
  if [ "$code" = "1" ]; then
    echo "!!! candidate collection failed - universe not built, retry on the next attempt"
  else
    python -m src.cli build-universe --month due >> "$LOGS/universe_$STAMP.log" 2>&1
    code=$?
  fi
  set -e
  if [ "$code" = "0" ]; then
    mark "universe built"
    # New symbols of the month: fetch their 1h and funding now so the cycle sees them.
    set +e
    python -m src.cli backfill --symbols universe-now --streams kline_4h,kline_1h,funding \
      --years 1.2 --concurrency 4 >> "$LOGS/backfill_$STAMP.log" 2>&1
    set -e
  else
    echo "!!! universe build failed (code $code), see $LOGS/universe_$STAMP.log"
  fi
elif [ "$code" != "0" ]; then
  echo "!!! universe check failed (code $code), see $LOGS/universe_$STAMP.log"
fi

mark "cycle: setup $SETUP, venue $VENUE"
args=(trade-cycle --setup "$SETUP" --venue "$VENUE")
[ "$EXECUTE" = "1" ] && args+=(--execute)
set +e
python -m src.cli "${args[@]}" 2>&1 | tee -a "$LOGS/cycle_${VENUE}_$STAMP.log"
code=${PIPESTATUS[0]}
set -e
case "$code" in
  0) mark "done" ;;
  3) mark "done, but some symbols were skipped due to stale data (see journal)" ;;
  4) echo "!!! TRADING SWITCHED OFF - a human is needed:"
     echo "    python -m src.cli trade-status --setup $SETUP --venue $VENUE"
     echo "    python -m src.cli trade-resume --setup $SETUP --venue $VENUE --reason '...'" ;;
  *) echo "!!! cycle failed with code $code, see $LOGS/cycle_${VENUE}_$STAMP.log" ;;
esac
exit "$code"
