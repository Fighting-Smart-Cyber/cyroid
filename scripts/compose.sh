#!/bin/sh
# The one place the overlay chain is written down.
#
# It was written down twice before this -- scripts/pg-update.sh and the update
# container's script in backend/proving_ground/api/admin.py -- so a host could
# be redeployed by either path and get a different stack. Adding the serve
# overlay to one of them would have left the other quietly putting the Vite dev
# server back on the public interface at the next update.
#
# POSIX sh, not bash: the update container runs docker:27-cli, which has no
# bash. That is also why the file list is a word-split string rather than an
# array.
#
# Usage: ./scripts/compose.sh up -d --build
#        ./scripts/compose.sh ps
#
# PG_SERVE_FRONTEND=0 keeps the Vite dev server and its hot reload. Do that on
# a developer's machine, not on a host anything else can reach.
set -eu
cd "$(dirname "$0")/.."

FILES="-f docker-compose.yml -f docker-compose.dev.yml -f docker-compose.override.yml"

if [ "${PG_SERVE_FRONTEND:-1}" != "0" ]; then
  FILES="$FILES -f docker-compose.serve.yml"
fi

# Deliberate word splitting; no compose file path contains a space.
# shellcheck disable=SC2086
exec docker compose $FILES "$@"
