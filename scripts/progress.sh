#!/usr/bin/env bash
#
# Progress of a long-running collection operation. Ctrl+C exits the view
# (the operation itself keeps going; it is a separate process).
#
# Usage:
#   scripts/progress.sh repair     # gap repair
#   scripts/progress.sh backfill   # symbol top-up
#
# P-002 deliberately does not apply here: this is an indicator for a human,
# not a check. No decisions are made from its output.
#
# The process search pattern starts with "python", not "src.cli":
# pgrep -f matches the WHOLE command line, so the pattern "src.cli ..."
# found the watching script itself and any shell where that string
# appears as an argument. Three watchdog loops once hung, each
# considering the others alive.
set -uo pipefail
cd "$(dirname "$0")/.."

case "${1:-repair}" in
    repair)
        # The denominator is NOT known in advance: repair touches only symbols
        # that have gaps. A hardcoded "142 × 3" lied, showing 87% on a fully
        # completed operation.
        LOG="var/reports/repair.log"; TOTAL=0
        MARK="repair.done"; PROC="[p]ython.*src\.cli repair-gaps"; NAME="Gap repair"
        ;;
    backfill)
        LOG="var/reports/backfill_tail.log"; TOTAL=108
        MARK="collector.done"; PROC="[p]ython.*src\.cli backfill"; NAME="Top-up"
        ;;
    *)
        echo "Usage: $0 [repair|backfill]" >&2
        exit 2
        ;;
esac

# grep -c prints 0 AND returns 1 when there are no matches, so
# `|| echo 0` would add a second line and break the arithmetic.
count() { grep -c "$1" "$2" 2>/dev/null | head -1 || echo 0; }

while true; do
    done_n=$(count "$MARK" "$LOG"); done_n=${done_n:-0}
    rows=$(grep -oE "(written|rows)=[0-9]+" "$LOG" 2>/dev/null \
           | cut -d= -f2 | awk '{s+=$1} END {print s+0}')
    tf=$(grep -oE "(timeframe|stream)=[0-9a-z_]+" "$LOG" 2>/dev/null | tail -1 | cut -d= -f2)
    last=$(grep -oE "symbol=[A-Z0-9]+" "$LOG" 2>/dev/null | tail -1 | cut -d= -f2)

    if (( TOTAL > 0 )); then
        pct=$(( done_n * 100 / TOTAL )); (( pct > 100 )) && pct=100
        filled=$(( pct / 2 ))
        bar=""; empty=""
        for ((i=0; i<filled; i++)); do bar+="█"; done
        for ((i=filled; i<50; i++)); do empty+="·"; done
        printf "\r\033[K%s [%s%s] %3d%%  %d/%d  ·  %s %s  ·  rows: %s" \
            "$NAME" "$bar" "$empty" "$pct" "$done_n" "$TOTAL" "${tf:-—}" "${last:-—}" "${rows:-0}"
    else
        # The total number of operations is unknown in advance: show a counter,
        # not a made-up percentage.
        spin="|/-\\"; frame=${spin:$(( SECONDS / 5 % 4 )):1}
        printf "\r\033[K%s %s  done: %d  ·  %s %s  ·  rows: %s" \
            "$NAME" "$frame" "$done_n" "${tf:-—}" "${last:-—}" "${rows:-0}"
    fi

    if ! pgrep -f "$PROC" >/dev/null 2>&1; then
        printf "\n\n%s finished. Operations: %d, rows: %s\n" "$NAME" "$done_n" "${rows:-0}"
        exit 0
    fi
    sleep 5
done
