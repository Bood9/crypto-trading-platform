#!/usr/bin/env bash
# Re-fetch data lost before the DEC-004 and DEC-036 fixes.
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate
R=var/reports
mark() { echo "=== $(date -u +%H:%M:%S) $* ==="; }

# A usage error (2) and a full failure (1) stop the script; a partial one (3)
# is printed. Same wrapper as in deepen.sh (DEC-026).
step() {
  local log="$1"; shift
  set +e
  python -m src.cli "$@" > "$log" 2>&1
  local code=$?
  set -e
  case "$code" in
    0) ;;
    3) echo "!!! partial failure, see $log" ;;
    *) echo "!!! FAILED with code $code, see $log"; exit "$code" ;;
  esac
}

mark "4h candles: gaps across all candidates"
step "$R/repair_4h.log" repair-gaps --symbols "$R/candidates_5y.txt" --timeframes 4h
mark "1h candles: gaps across the universe"
step "$R/repair_1h.log" repair-gaps --symbols "$R/universe_5y.txt" --timeframes 1h
mark "funding: full re-fetch with the fixed collector"
step "$R/refetch_funding.log" backfill --symbols "$R/universe_5y.txt" --years 5 \
  --streams funding --full --concurrency 4
mark "rebuilding the universe"
step "$R/build_universe_5y_repaired.log" build-universe --years 5 --export "$R/universe_5y.txt"
mark "availability matrix"
step "$R/coverage_5y_repaired.log" coverage-report --symbols "$R/universe_5y.txt" \
  --years 5 --streams kline_4h,kline_1h,oi,funding
mark "DONE"
