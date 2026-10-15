#!/usr/bin/env bash
#
# Project wrapper around psql. P-002: a check counts only by its
# exit code.
#
# ON_ERROR_STOP=1 is mandatory. Without it psql runs the rest of the script
# after an error and returns 0, so the output ends on a successful line
# and the failed DDL goes unnoticed. That is exactly how a column type
# change on a compressed hypertable once "passed".
#
# Usage:
#   scripts/psql.sh -c "SELECT 1"
#   scripts/psql.sh -f migration.sql
#   echo "SELECT 1" | scripts/psql.sh
set -euo pipefail

cd "$(dirname "$0")/.."

# shellcheck disable=SC1091
[[ -f .env ]] && set -a && source .env && set +a

exec docker compose exec -T \
    -e "PGOPTIONS=--client-min-messages=warning" \
    postgres psql \
    -v ON_ERROR_STOP=1 \
    -U "${POSTGRES_USER:-crypto}" \
    -d "${POSTGRES_DB:-crypto_bot}" \
    "$@"
