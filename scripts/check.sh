#!/usr/bin/env bash
#
# Single pre-commit check: linter, tests, migration drift.
#
# `pipefail` here is not a matter of taste. Without it the exit code of a
# pipeline is the code of the LAST command, so `pytest | tail` reports success
# even when tests fail. This trap has already let a red run slip through twice,
# and both times the environment was to blame, not attentiveness, so the fix
# belongs in the environment.
#
# `-u` catches typos in variable names, `-e` stops at the first failure.
set -euo pipefail

cd "$(dirname "$0")/.."

PY=".venv/bin/python"
RUFF=".venv/bin/ruff"
ALEMBIC=".venv/bin/alembic"

if [[ ! -x "$PY" ]]; then
    echo "No virtual environment. Create one: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'" >&2
    exit 1
fi

fail=0

echo "── ruff ────────────────────────────────────────────────"
"$RUFF" check src tests migrations || fail=1

echo
echo "── pytest ──────────────────────────────────────────────"
pytest_out=$("$PY" -m pytest -q 2>&1) || fail=1
echo "$pytest_out"
passed=$(printf '%s' "$pytest_out" | grep -oE '[0-9]+ passed' | head -1 | cut -d' ' -f1)
passed=${passed:-0}

echo
echo "── migration drift ─────────────────────────────────────"
if "$PY" -c 'import socket,sys; s=socket.socket(); s.settimeout(1); sys.exit(s.connect_ex(("localhost",5432)))' 2>/dev/null; then
    "$ALEMBIC" check || fail=1
else
    echo "Postgres unavailable, check skipped (docker compose up -d)"
fi

echo
if [[ $fail -eq 0 ]]; then
    # Marker for the status report. Written ONLY on a green run:
    # the report must show the number of tests passed, not run.
    mkdir -p var
    printf '%s|%s\n' "$passed" "$(date -u '+%d.%m.%Y %H:%M UTC')" > var/last-check.txt
    echo "All clean."
else
    echo "ERRORS FOUND - see above." >&2
fi
exit "$fail"
