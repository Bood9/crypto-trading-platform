#!/usr/bin/env bash
# launchd wrapper (DEC-041): the decision cycle runs once per UTC day,
# the day watch runs every hour after it.
#
# launchd calls it every hour at :05 local time, the minute pmset wakes
# the Mac. launchd cannot schedule in UTC, and local time shifts by an
# hour twice a year, so "what to do" is decided here, by UTC:
#
# * the first call after 00:00 UTC is the decision cycle (daily.sh). If the
#   Mac was not asleep, that is 00:05 UTC in summer and 01:05 UTC in
#   winter, inside the entry window;
# * the Mac slept and woke later: the cycle still runs. Paper executes the
#   decision at decision-moment prices, a real exchange would reject the
#   entry by the window; exits and stops are always accounted for;
# * the cycle already ran today: day watch. Stops on open positions count
#   immediately, no new entries.
#
# Both night and day are run by the bot platform (DEC-049, `python -m src.bots`):
# Collector → Analyst → Risk Manager → Executor → Controller. Exit codes
# are the same as trade-cycle. The fallback is the old daily.sh as a single
# process: `touch var/live/use_daily_sh` (delete the file to undo).
set -uo pipefail
cd "$(dirname "$0")/.."

LOGS=var/live
mkdir -p "$LOGS"
DAY="$(date -u +%Y%m%d)"
DONE="$LOGS/done_$DAY"

# A failure also goes into the human-readable journal, next to regular runs.
journal() {
  printf '## %s UTC · night run · NOT EXECUTED\n\n**Reason:** %s\n\n---\n\n' \
    "$(date -u '+%Y-%m-%d %H:%M')" "$1" >> "$LOGS/journal.md"
}

notify() {
  # A quote or backslash in the text would break the AppleScript string,
  # and the notification would silently vanish (embedding review 2026-09-30).
  local m="${1//\\/\\\\}"
  m="${m//\"/\\\"}"
  osascript -e "display notification \"$m\" with title \"CRYPTO: trading cycle\"" >/dev/null 2>&1 || true
}

# The Controller only reports for now (DEC-049): it does not change the shift's
# code, but the human learns about a failed check, once a day. Same for
# a night failure of the Collector (e.g. the month's universe was not built).
platform_alert() {
  local key title
  for pair in "controller:Controller" "collector:Collector"; do
    key="${pair%%:*}"; title="${pair#*:}"
    if [ -f "$LOGS/${key}_failed_$DAY" ] && [ ! -f "$LOGS/${key}_notified_$DAY" ]; then
      touch "$LOGS/${key}_notified_$DAY"
      # head -c cuts by bytes; iconv -c drops a letter cut in half.
      notify "$title: $(head -c 160 "$LOGS/${key}_failed_$DAY" | iconv -c -f UTF-8 -t UTF-8) — see the panel"
    fi
  done
}

export PATH="/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# Do not sleep while this whole script runs, including bringing up the
# database, not just the cycle itself (platform design 2026-09-29).
caffeinate -i -s -w $$ &

# The database lives in Docker. At night Docker Desktop might be closed, and
# then the cycle failed on the connection (2026-09-26, five attempts in a row).
ensure_db() {
  if ! docker info >/dev/null 2>&1; then
    echo "$(date -u +%FT%TZ) Docker is not running, starting it" >> "$LOGS/scheduled_$DAY.log"
    open -ga Docker
    for _ in $(seq 1 90); do
      docker info >/dev/null 2>&1 && break
      sleep 2
    done
  fi
  docker compose up -d --wait postgres >> "$LOGS/scheduled_$DAY.log" 2>&1
}
ensure_db_or_exit() {
  if ! ensure_db; then
    echo "$(date -u +%FT%TZ) database did not come up" >> "$LOGS/scheduled_$DAY.log"
    notify "Database did not come up (Docker). Retrying in an hour."
    journal "database did not come up (Docker did not start). Retrying in an hour."
    exit 0
  fi
}

# The day's decision is already made: day watch. Hourly bars for open
# positions, triggered stops count immediately, no new entries.
# Docker is NOT started during the day: if you closed it, the watch silently
# waits, without false "night run not executed" (day watch review 2026-09-29).
if [ -f "$DONE" ]; then
  MON="$LOGS/monitor_$DAY.log"
  echo "##### $(date -u +%H:%M:%S) UTC" >> "$MON"
  if ! docker info >/dev/null 2>&1; then
    echo "Docker is not running, watch skipped" >> "$MON"
    exit 0
  fi
  before=$(wc -c < "$LOGS/journal.md" 2>/dev/null || echo 0)
  if [ -f "$LOGS/use_daily_sh" ]; then
    caffeinate -i -s sh -c '.venv/bin/python -m src.cli backfill --symbols held \
        --streams kline_1h,funding --years 0.05; .venv/bin/python -m src.cli paper-monitor' \
      >> "$MON" 2>&1
  else
    caffeinate -i -s .venv/bin/python -m src.bots day >> "$MON" 2>&1
  fi
  code=$?
  after=$(wc -c < "$LOGS/journal.md" 2>/dev/null || echo 0)
  echo "code $code" >> "$MON"
  case "$code" in
    0) [ "$before" != "$after" ] && \
         notify "Day watch: a stop triggered or a position changed — see the journal" ;;
    4) notify "Trading is switched off, a human is needed: trade-status" ;;
    # A failure of the watch itself: one notification per day, not silence.
    *) if [ ! -f "$LOGS/monitor_failed_$DAY" ]; then
         touch "$LOGS/monitor_failed_$DAY"
         notify "Day watch failed (code $code), the night cycle will count the stops. Log: $MON"
       fi ;;
  esac
  platform_alert
  exit 0
fi

ensure_db_or_exit

# caffeinate: do not sleep while the cycle runs. On 2026-09-27 the Mac on battery
# woke for seconds at a time, and the cycle ran in fits for almost an hour.
# Journal size before the attempt: if the cycle wrote its own failure block, a
# second "NOT EXECUTED" block from this script is not needed (there used to be two).
before=$(wc -c < "$LOGS/journal.md" 2>/dev/null || echo 0)
if [ -f "$LOGS/use_daily_sh" ]; then
  caffeinate -i -s scripts/daily.sh >> "$LOGS/scheduled_$DAY.log" 2>&1
else
  caffeinate -i -s .venv/bin/python -m src.bots night >> "$LOGS/scheduled_$DAY.log" 2>&1
fi
code=$?
after=$(wc -c < "$LOGS/journal.md" 2>/dev/null || echo 0)
echo "$(date -u +%FT%TZ) code $code" >> "$LOGS/scheduled_$DAY.log"

case "$code" in
  0) touch "$DONE" ;;
  3) touch "$DONE"
     notify "The cycle did not run fully: part of the data is stale — see the journal" ;;
  4) touch "$DONE"; notify "Trading is switched off, a human is needed: trade-status" ;;
  # Failed (database not up, network): no mark, retry in an hour.
  # One notification per day: during a prolonged failure retries run every hour.
  *) if [ ! -f "$LOGS/notified_$DAY" ]; then
       touch "$LOGS/notified_$DAY"
       notify "The cycle failed with code $code, retrying every hour. Journal: var/live/journal.md"
     fi
     if [ "$before" = "$after" ]; then
       journal "the cycle failed with code $code, retrying in an hour. Details: var/live/scheduled_$DAY.log"
     fi ;;
esac
platform_alert
exit 0
