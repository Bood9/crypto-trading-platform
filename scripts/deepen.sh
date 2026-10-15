#!/usr/bin/env bash
# Deepen history to 5 years and rebuild the universe membership (DEC-018).
#
# The order comes from the build-universe docstring and matters: first the
# LIGHT 4h for all candidates, then selection, then full depth for the
# selected. The reverse order introduces survivorship bias: the 2022 universe
# would be built from symbols that were liquid in 2025 (DEC-003).
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate
R=var/reports
mark() { echo "=== $(date -u +%H:%M:%S) $* ==="; }

# The load returns 0 on full success, 3 on partial failure (some delisted
# pairs lack one stream or another, which is expected), 1 on full failure or
# a tripped fuse, 2 on a usage error. Only 0 and 3 are tolerated, and a
# partial failure is printed, not swallowed. Previously there was nothing
# here: the load always returned 0, and the pipeline rebuilt the universe on
# data that had not loaded (DEC-026).
load() {
  local log="$1"; shift
  set +e
  python -m src.cli backfill "$@" > "$log" 2>&1
  local code=$?
  set -e
  case "$code" in
    0) ;;
    3) echo "!!! partial failure, see $log: $(grep -c ERROR "$log") streams" ;;
    *) echo "!!! LOAD FAILED with code $code, see $log"; exit "$code" ;;
  esac
}

# There is no separate top-up of the original 147 symbols: the fourth stage
# downloads full streams for the new universe, which includes almost all of
# them. Downloading them twice would waste several hours.

# 1. Light pass: 4h only, but for ALL candidates. The turnover used for
# selection lives in these candles, and without them the universe of past
# years would be built from today's survivors.
mark "light 4h for all candidates"
load "$R/backfill_light_4h.log" --symbols "$R/candidates_5y.txt" \
  --years 5 --streams kline_4h --concurrency 4

# 2. Point-in-time selection over the full depth.
mark "rebuilding the universe over 5 years"
python -m src.cli build-universe --years 5 --export "$R/universe_5y.txt" \
  > "$R/build_universe_5y.log" 2>&1

# 3. Full depth for the selected, including those that were not
# in the previous list of 147.
mark "full streams for the new universe"
load "$R/backfill_universe_5y.log" --symbols "$R/universe_5y.txt" \
  --years 5 --streams kline_4h,kline_1h,oi,funding --concurrency 4

# 4. Availability matrix over the same depth.
mark "availability matrix"
python -m src.cli coverage-report --symbols "$R/universe_5y.txt" --years 5 \
  --streams kline_4h,kline_1h,oi,funding --export "$R/availability_5y.json" \
  > "$R/coverage_5y.log" 2>&1

mark "DONE"
