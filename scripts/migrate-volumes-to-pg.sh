#!/usr/bin/env bash
# Copies platform volume data from the proving-ground_* names to pg_*.
#
# Docker cannot rename a volume, so the Compose project rename (proving-ground -> pg)
# would otherwise bring the stack up on EMPTY volumes, orphaning the platform database.
#
# Non-destructive: old volumes are left intact for rollback. Nothing is deleted here.
set -euo pipefail

PAIRS=(
  "proving-ground_postgres_data:pg_postgres_data"
  "proving-ground_minio_data:pg_minio_data"
  "proving-ground_frontend_node_modules:pg_frontend_node_modules"
)

fail=0

for pair in "${PAIRS[@]}"; do
  old="${pair%%:*}"
  new="${pair##*:}"

  if ! docker volume inspect "$old" >/dev/null 2>&1; then
    echo "SKIP  $old does not exist"
    continue
  fi
  if docker volume inspect "$new" >/dev/null 2>&1; then
    echo "SKIP  $new already exists - refusing to overwrite"
    continue
  fi

  echo "COPY  $old -> $new"
  docker volume create "$new" >/dev/null
  docker run --rm \
    -v "$old":/from:ro \
    -v "$new":/to \
    alpine:3.20 \
    sh -c 'cd /from && cp -a . /to/'

  # Verify by FILE COUNT and FILE CONTENT checksum.
  #
  # Do NOT compare `du -sb` totals: du sums directory inode sizes as well as file
  # contents, and a freshly written tree allocates directory entries differently, so
  # identical data reports byte deltas that are exact multiples of the 4K block size.
  # Content checksum is the only claim worth making here.
  ck() { docker run --rm -v "$1":/v:ro alpine:3.20 sh -c 'cd /v && find . -type f | sort | xargs md5sum 2>/dev/null | md5sum' | cut -d" " -f1; }
  fc() { docker run --rm -v "$1":/v:ro alpine:3.20 sh -c 'find /v -type f | wc -l' | tr -d " "; }

  src_c=$(ck "$old"); dst_c=$(ck "$new")
  src_f=$(fc "$old"); dst_f=$(fc "$new")

  if [ "$src_c" != "$dst_c" ] || [ "$src_f" != "$dst_f" ]; then
    echo "FAIL  mismatch for $new"
    echo "        checksum: $old=$src_c  $new=$dst_c"
    echo "        files:    $old=$src_f  $new=$dst_f"
    fail=1
  else
    echo "OK    $new verified ($dst_f files, content $dst_c)"
  fi
done

if [ "$fail" -ne 0 ]; then
  echo
  echo "MIGRATION FAILED - do NOT bring the stack up on these volumes."
  exit 1
fi

echo
echo "Migration complete. Old proving-ground_* volumes retained for rollback."
