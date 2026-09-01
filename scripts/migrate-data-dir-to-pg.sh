#!/usr/bin/env bash
# Copies the bind-mounted data directory from /data/cyroid to /data/proving-ground,
# and (separately) rewrites the absolute paths the database stores for cached
# ISOs and golden images.
#
# The compose files and config.py default moved to /data/proving-ground. An
# existing install keeps its ISOs, VM storage, golden images, catalogs and
# registry blobs under the old path, so without this the stack comes up on an
# EMPTY data directory: every cached ISO re-downloads and every golden image is
# gone.
#
# Non-destructive: the old directory is left intact for rollback. Nothing is
# deleted here.
#
# This is TWO separate phases with OPPOSITE required stack states, because the
# file copy touches the host filesystem the containers have bind-mounted (so
# the stack must be DOWN) while the path rewrite talks to the running
# database (so the stack, or at least `db`, must be UP). Running both from a
# single invocation is therefore impossible - do not try to "fix" that by
# relaxing either guard.
#
# Usage:
#   ./scripts/migrate-data-dir-to-pg.sh [--apply]
#       Phase 1: copy files. Requires the stack DOWN. Without --apply this is
#       a dry run that only reports what it would copy.
#
#   ./scripts/migrate-data-dir-to-pg.sh --paths-only [--apply]
#       Phase 2: rewrite stored DB paths. Requires the stack (at least `db`)
#       UP. Without --apply this is a dry run that reports how many rows
#       WOULD be updated, per table, without changing anything.
#
# Env vars:
#   OLD_DATA_DIR / NEW_DATA_DIR   - HOST-side directories for the file copy.
#                                   Default: /data/cyroid -> /data/proving-ground
#   OLD_DB_PREFIX / NEW_DB_PREFIX - CONTAINER-side path prefixes as stored in
#                                   the database. These are deliberately
#                                   separate from OLD_DATA_DIR/NEW_DATA_DIR:
#                                   on a DinD host the two are not the same
#                                   path (e.g. host
#                                   ~/proving-ground/data/proving-ground is
#                                   bind-mounted to container
#                                   /data/proving-ground). Reusing the host
#                                   vars for the SQL match would silently
#                                   match zero rows.
#                                   Default: /data/cyroid -> /data/proving-ground
set -euo pipefail

OLD="${OLD_DATA_DIR:-/data/cyroid}"
NEW="${NEW_DATA_DIR:-/data/proving-ground}"
OLD_DB_PREFIX="${OLD_DB_PREFIX:-/data/cyroid}"
NEW_DB_PREFIX="${NEW_DB_PREFIX:-/data/proving-ground}"

MODE="files"
APPLY=0
for arg in "$@"; do
  case "$arg" in
    --paths-only) MODE="paths" ;;
    --apply) APPLY=1 ;;
    *)
      echo "Unknown argument: $arg" >&2
      echo "Usage: $0 [--paths-only] [--apply]" >&2
      exit 2
      ;;
  esac
done

# ---------------------------------------------------------------------------
# Phase 2: rewrite stored DB paths. Requires the stack (at least `db`) UP.
# ---------------------------------------------------------------------------
if [ "$MODE" = "paths" ]; then
  if ! docker compose ps --quiet db 2>/dev/null | grep -q .; then
    echo "REFUSING: the database is not running. --paths-only needs it up:" >&2
    echo "  docker compose up -d db" >&2
    exit 1
  fi

  # NOTE: psql only expands :'variable' when the SQL is read as a script
  # (stdin/heredoc/-f) -- NOT when passed via -c, where ':' is not special.
  # Both branches below therefore feed psql a heredoc rather than -c.
  if [ "$APPLY" -eq 0 ]; then
    echo "DRY RUN — reporting rows that would be updated (old prefix: $OLD_DB_PREFIX, new prefix: $NEW_DB_PREFIX)."
    echo
    docker compose exec -T db psql -U proving_ground -d proving_ground -X \
      -v old_prefix="$OLD_DB_PREFIX" <<'SQL'
\echo base_images.iso_path
SELECT count(*) FROM base_images         WHERE iso_path          LIKE :'old_prefix' || '/%';
\echo golden_images.disk_image_path
SELECT count(*) FROM golden_images       WHERE disk_image_path   LIKE :'old_prefix' || '/%';
\echo vm_templates.cached_iso_path
SELECT count(*) FROM vm_templates        WHERE cached_iso_path   LIKE :'old_prefix' || '/%';
\echo vm_templates.golden_image_path
SELECT count(*) FROM vm_templates        WHERE golden_image_path LIKE :'old_prefix' || '/%';
\echo vms.iso_path
SELECT count(*) FROM vms                 WHERE iso_path          LIKE :'old_prefix' || '/%';
\echo artifacts.file_path
SELECT count(*) FROM artifacts           WHERE file_path         LIKE :'old_prefix' || '/%';
\echo content_assets.file_path
SELECT count(*) FROM content_assets      WHERE file_path         LIKE :'old_prefix' || '/%';
\echo artifact_placements.target_path
SELECT count(*) FROM artifact_placements WHERE target_path       LIKE :'old_prefix' || '/%';
SQL
    echo
    echo "Re-run with --apply to perform the rewrite."
    exit 0
  fi

  echo "Rewriting stored absolute paths in the database (old prefix: $OLD_DB_PREFIX, new prefix: $NEW_DB_PREFIX)..."
  echo
  docker compose exec -T db psql -U proving_ground -d proving_ground -v ON_ERROR_STOP=1 \
    -v old_prefix="$OLD_DB_PREFIX" \
    -v new_prefix="$NEW_DB_PREFIX" <<'SQL'
BEGIN;
\echo base_images.iso_path
UPDATE base_images         SET iso_path          = replace(iso_path,          :'old_prefix' || '/', :'new_prefix' || '/') WHERE iso_path          LIKE :'old_prefix' || '/%';
\echo golden_images.disk_image_path
UPDATE golden_images       SET disk_image_path   = replace(disk_image_path,   :'old_prefix' || '/', :'new_prefix' || '/') WHERE disk_image_path   LIKE :'old_prefix' || '/%';
\echo vm_templates.cached_iso_path
UPDATE vm_templates        SET cached_iso_path   = replace(cached_iso_path,   :'old_prefix' || '/', :'new_prefix' || '/') WHERE cached_iso_path   LIKE :'old_prefix' || '/%';
\echo vm_templates.golden_image_path
UPDATE vm_templates        SET golden_image_path = replace(golden_image_path, :'old_prefix' || '/', :'new_prefix' || '/') WHERE golden_image_path LIKE :'old_prefix' || '/%';
\echo vms.iso_path
UPDATE vms                 SET iso_path          = replace(iso_path,          :'old_prefix' || '/', :'new_prefix' || '/') WHERE iso_path          LIKE :'old_prefix' || '/%';
\echo artifacts.file_path
UPDATE artifacts           SET file_path         = replace(file_path,         :'old_prefix' || '/', :'new_prefix' || '/') WHERE file_path         LIKE :'old_prefix' || '/%';
\echo content_assets.file_path
UPDATE content_assets      SET file_path         = replace(file_path,         :'old_prefix' || '/', :'new_prefix' || '/') WHERE file_path         LIKE :'old_prefix' || '/%';
\echo artifact_placements.target_path
UPDATE artifact_placements SET target_path       = replace(target_path,       :'old_prefix' || '/', :'new_prefix' || '/') WHERE target_path       LIKE :'old_prefix' || '/%';
COMMIT;
SQL

  echo
  echo "Stored paths: done (row counts above, per table, as reported by psql's UPDATE tag)."
  exit 0
fi

# ---------------------------------------------------------------------------
# Phase 1: copy files. Requires the stack DOWN.
# ---------------------------------------------------------------------------
if [ ! -d "$OLD" ]; then
  echo "Nothing to do: $OLD does not exist."
  exit 0
fi

if [ -d "$NEW" ] && [ -n "$(ls -A "$NEW" 2>/dev/null)" ]; then
  echo "REFUSING: $NEW already exists and is not empty." >&2
  echo "Inspect it and remove it, or set NEW_DATA_DIR, then re-run." >&2
  exit 1
fi

if docker compose ps --quiet 2>/dev/null | grep -q .; then
  echo "REFUSING: the stack appears to be running. Bring it down first:" >&2
  echo "  docker compose down" >&2
  exit 1
fi

src_files="$(find "$OLD" -type f | wc -l | tr -d ' ')"
src_size="$(du -sh "$OLD" 2>/dev/null | cut -f1)"
echo "Source: $OLD  ($src_files files, $src_size)"
echo "Target: $NEW"

if [ "$APPLY" -eq 0 ]; then
  echo
  echo "DRY RUN — nothing copied. Re-run with --apply to perform the migration."
  exit 0
fi

mkdir -p "$NEW"
cp -a "$OLD/." "$NEW/"

dst_files="$(find "$NEW" -type f | wc -l | tr -d ' ')"
echo "Copied: $dst_files files"

if [ "$src_files" -ne "$dst_files" ]; then
  echo "FAILED: file count mismatch ($src_files -> $dst_files). $OLD is untouched." >&2
  exit 1
fi

echo
echo "=================================================================="
echo "Files: copied and verified."
echo "IMPORTANT: stored DB paths have NOT been rewritten yet. That step"
echo "needs the stack (or at least the db service) UP. Bring it up, then"
echo "run:"
echo "  $0 --paths-only --apply"
echo "=================================================================="
echo "$OLD is untouched — remove it once you have confirmed the stack is healthy:"
echo "  sudo rm -rf $OLD"
