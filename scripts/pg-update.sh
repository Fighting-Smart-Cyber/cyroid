#!/usr/bin/env bash
# pg-update.sh — pull latest PROVING GROUND code and redeploy.
# Non-destructive: keeps the DB, catalogs, registry and volumes.
# alembic migrations run automatically on api start.
# Usage: ~/pg-update.sh [branch]   (defaults to the current branch)
set -euo pipefail
cd "$HOME/proving-ground"

BRANCH="${1:-$(git branch --show-current)}"
echo "==> fetching + updating to origin/$BRANCH"
git fetch --prune origin
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"
echo "    now at $(git rev-parse --short HEAD) — $(git log -1 --format='%s')"

echo "==> ensuring range networks exist"
./scripts/init-networks.sh >/dev/null 2>&1 || true

echo "==> rebuilding + redeploying (this may take several minutes)"
./scripts/compose.sh up -d --build

echo "==> waiting for health"
for _ in $(seq 1 60); do
  total=$(docker compose ps --format '{{.Name}}' | wc -l | tr -d ' ')
  healthy=$(docker compose ps --format '{{.Status}}' | grep -c healthy || true)
  if [ "$total" -gt 0 ] && [ "$healthy" -ge "$total" ]; then
    echo "    all $total services healthy"
    break
  fi
  sleep 5
done
docker compose ps --format '{{.Name}}  {{.Status}}' | sed 's/proving-ground-//'
echo "==> update complete"
