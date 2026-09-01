#!/usr/bin/env bash
# Samples how many range deploys are IN FLIGHT concurrently.
#
# Counts ranges in status DEPLOYING, not pg.type=dind containers: a range's DinD
# container persists for the whole life of the range, so counting containers
# measures "ranges that exist", not "deploys running". An earlier version of this
# script made exactly that mistake and reported peak=6 against a cap of 3 while
# the cap was in fact holding perfectly.
#
# Usage: verify-deploy-concurrency.sh [seconds] [cap] [name-prefix]
set -uo pipefail

SAMPLE_SECONDS="${1:-600}"
CAP="${2:-6}"
PREFIX="${3:-}"
INTERVAL=3

if [ -n "$PREFIX" ]; then
  WHERE="where status='DEPLOYING' and name like '${PREFIX}%'"
else
  WHERE="where status='DEPLOYING'"
fi

echo "Sampling ranges in DEPLOYING every ${INTERVAL}s for ${SAMPLE_SECONDS}s (cap=${CAP})"
echo

max=0
saw_any=0
end=$(( $(date +%s) + SAMPLE_SECONDS ))
while [ "$(date +%s)" -lt "$end" ]; do
  n=$(docker exec pg-db-1 psql -U proving_ground -d proving_ground -tAc \
        "select count(*) from ranges ${WHERE};" 2>/dev/null | tr -d ' ')
  n="${n:-0}"
  [ "$n" -gt 0 ] && saw_any=1
  [ "$n" -gt "$max" ] && max=$n
  printf '%s  deploying=%s  peak=%s\n' "$(date +%H:%M:%S)" "$n" "$max"
  sleep "$INTERVAL"
done

echo
echo "Peak concurrent deploys: $max (cap $CAP)"
if [ "$max" -gt "$CAP" ]; then
  echo "FAIL: cap exceeded"
  exit 1
fi
if [ "$saw_any" -eq 0 ]; then
  echo "INCONCLUSIVE: no deploy was ever observed - was anything enqueued?"
  exit 2
fi
echo "PASS: cap held"
