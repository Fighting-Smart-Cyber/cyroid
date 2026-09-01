#!/usr/bin/env bash
# Tag a release.
#
# This does NOT bump VERSION. master is a protected branch, so the bump goes
# through a merge request first — see "CRITICAL: Cutting a release" in
# CLAUDE.md. This script only tags a master that is already correct, because
# scripts/publish-to-github.sh reads VERSION out of the tagged ref: a tag whose
# VERSION disagrees publishes a public commit under the wrong version.
#
# Usage:
#   ./scripts/tag-release.sh 0.39.0
set -euo pipefail

[ $# -eq 1 ] || { echo "Usage: $0 <version>   e.g. $0 0.39.0" >&2; exit 1; }
VERSION="$1"

# Must be exactly X.Y.Z. A glob is too loose here — "0.39.0-rc1" passes one and
# then tags something CI silently ignores.
if [[ ! "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "Refusing: '$VERSION' is not X.Y.Z. The CI rule only matches ^v[0-9]+\.[0-9]+\.[0-9]+$ —" >&2
  echo "          a tag like v${VERSION} would silently run no release jobs." >&2
  exit 1
fi

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

branch="$(git rev-parse --abbrev-ref HEAD)"
[ "$branch" = "master" ] || { echo "Refusing: on '$branch', not master." >&2; exit 1; }

[ -z "$(git status --porcelain)" ] || { echo "Refusing: working tree is dirty." >&2; exit 1; }

git fetch origin --quiet
if [ "$(git rev-parse HEAD)" != "$(git rev-parse origin/master)" ]; then
  echo "Refusing: master is not in sync with origin/master. Pull first." >&2
  exit 1
fi

file_version="$(tr -d '[:space:]' < VERSION)"
if [ "$file_version" != "$VERSION" ]; then
  echo "Refusing: VERSION on master is '$file_version', not '$VERSION'." >&2
  echo "          Merge the bump before tagging — publish-to-github.sh reads VERSION" >&2
  echo "          from the tagged ref and would publish under the wrong version." >&2
  exit 1
fi

if git rev-parse -q --verify "refs/tags/v$VERSION" >/dev/null; then
  echo "Refusing: tag v$VERSION already exists." >&2
  exit 1
fi

git tag -a "v$VERSION" -m "PROVING GROUND v$VERSION"
git push origin "v$VERSION"

cat <<MSG

Tagged v$VERSION and pushed it.

The tag pipeline runs build:infra automatically. publish-public is MANUAL —
press play to publish the public snapshot:
  Build > Pipelines > the v$VERSION pipeline > publish stage > play on publish-public
MSG
