#!/bin/sh
# Point every Iron Bank hardening manifest at the version in VERSION.
#
# The manifest has to carry the literal -- Iron Bank reads the file, so it cannot be computed at
# build time. That makes it a second copy of VERSION, and second copies drift. Rather than trust
# a release checklist, this syncs them and backend/tests/unit/test_ironbank_manifests.py fails
# the pipeline when they disagree, so drift cannot merge.
#
# Run it as part of the version bump, before committing:
#   echo 0.55.1 > VERSION && ./scripts/sync-ironbank-version.sh
set -eu
root="$(cd "$(dirname "$0")/.." && pwd)"
version="$(tr -d ' \n\r' < "$root/VERSION")"
[ -n "$version" ] || { echo "VERSION is empty" >&2; exit 1; }

found=0
for m in "$root"/ironbank/*/hardening_manifest.yaml; do
  [ -e "$m" ] || continue
  found=$((found + 1))
  # Both places the version appears: the tag list and the OCI label.
  sed -i.bak \
    -e "s/^  - \"[0-9][0-9.]*\"$/  - \"${version}\"/" \
    -e "s/^  org.opencontainers.image.version: \".*\"$/  org.opencontainers.image.version: \"${version}\"/" \
    "$m"
  rm -f "$m.bak"
  printf '  %-40s -> %s\n' "${m#"$root"/}" "$version"
done
[ "$found" -gt 0 ] || { echo "no hardening manifests found under ironbank/" >&2; exit 1; }
