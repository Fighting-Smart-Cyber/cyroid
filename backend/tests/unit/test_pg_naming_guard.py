"""Guards the pg-* rename.

Fails if a proving-ground-* Docker object name reappears, and fails if a
catalog-facing identifier is renamed away. See CLAUDE.md.
"""

import pathlib
import re

import pytest

# parents[2] is the backend root: `backend/` on the host, `/app` in the
# container (only backend/ is bind-mounted). Resolving this way works in both.
BACKEND = pathlib.Path(__file__).resolve().parents[2] / "proving_ground"

# The frontend is NOT mounted into the api container, so this check runs only
# on the host / in CI. The skipif below makes that explicit rather than silent.
FRONTEND = pathlib.Path(__file__).resolve().parents[3] / "frontend" / "src"

# DOM element id, not a Docker object — deliberately not renamed.
FRONTEND_ALLOWED = {"proving-ground-clipboard-bridge"}

# Docker object names and label keys that must no longer exist.
FORBIDDEN = [
    re.compile(r"proving-ground-range"),
    re.compile(r"proving-ground-mgmt"),
    re.compile(r"proving-ground-ranges"),
    re.compile(r"proving-ground-management"),
    re.compile(r"proving-ground\.[a-z_]+"),  # label keys
]

# scripts/*.sh and docker-compose*.yml are at the repo root and, like the
# frontend, are NOT mounted into the api container (only backend/ is), so
# these checks run only on the host / in CI. Same repo-root resolution and
# skipif pattern as the frontend check above.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
SCRIPTS = REPO_ROOT / "scripts"
COMPOSE_FILES = sorted(REPO_ROOT.glob("docker-compose*.yml"))

# Docker object names and image tags that must no longer exist in shell
# scripts or compose files. This is exactly the class of miss that let
# Items 2 and 3 of the fix wave slip through every earlier grep and review:
# producers were renamed but a consumer elsewhere still looked up the old
# name.
#
# The bare `name=proving-ground-"` pattern is deliberately broader than a
# specific suffix: deploy.sh's stale `docker network ls --filter
# "name=proving-ground-"` (Item 2) matched every legacy network by prefix
# rather than naming one, so a suffix-only pattern like the ones below would
# not have caught it. Proven by reverting Item 2 and observing this pattern
# go red -- see fixwave-report.md.
SCRIPT_COMPOSE_FORBIDDEN = [
    re.compile(r"proving-ground-range"),
    re.compile(r"proving-ground-mgmt"),
    re.compile(r"proving-ground-ranges"),
    re.compile(r"proving-ground-management"),
    re.compile(r"proving-ground-(api|worker|frontend)"),  # image tags
    re.compile(r'name=proving-ground-"'),  # stale network-filter prefix
]

# Catalog-facing identifiers that must still exist.
REQUIRED = {
    "image_namespace": 'image_namespace: str = "proving-ground"',
    "minio_bucket": 'minio_bucket: str = "proving-ground-artifacts"',
}

# api/content.py must still declare this MinIO bucket name literal —
# renaming it orphans content already stored under the old bucket.
CONTENT_BUCKET_NEEDLE = '"proving-ground-content"'

# ---------------------------------------------------------------------------
# Bare `"proving-ground-` / `'proving-ground-` string-literal guard.
#
# The FORBIDDEN patterns above only catch specific known-bad suffixes. The
# rename's actual surface turned out to be larger, so this check is broader:
# it flags *any* proving-ground-prefixed string literal in backend/proving_ground
# and requires each legitimate site to be explicitly allowlisted below, so a
# new one introduced later fails loudly instead of slipping through.
# ---------------------------------------------------------------------------

LITERAL_PATTERN = re.compile(r"""["']proving-ground-""")

# Substrings matched against the offending line. Grouped by why each site is
# deliberately exempt from the rename (see Global Constraints / CLAUDE.md).
ALLOWED_LITERALS = {
    # MinIO bucket names — renaming orphans artifacts/content already stored
    # under the old bucket name.
    "proving-ground-artifacts",  # config.py:86  minio_bucket
    "proving-ground-content",  # api/content.py:509
    # Docker image names/tags — catalog- and registry-facing; the rename's
    # bright-line test excludes anything a catalog blueprint resolves against.
    "proving-ground-golden-",  # api/snapshots.py:74
    "proving-ground-snapshot-",  # api/snapshots.py:125
    "proving-ground-snapshot/",  # docker_service.py:2529
    # tempfile.mkdtemp() prefixes — ephemeral local directories, not Docker
    # objects, so they're outside this rename's scope.
    "proving-ground-blueprint-export-",
    "proving-ground-blueprint-import-",
    "proving-ground-export-",
    "proving-ground-export-offline-",
    "proving-ground-import-",
    "proving-ground-import-artifacts-",
    "proving-ground-import-images-",
    # Same ephemeral export temp-dir as the mkdtemp prefix above -- these are
    # membership checks ("is this path inside a proving-ground-export temp
    # dir?") before an rmtree/cleanup, not a new Docker object.
    "proving-ground-export",  # tasks/blueprint_export.py:106,125
}


def _python_files():
    return [p for p in BACKEND.rglob("*.py") if "__pycache__" not in str(p)]


@pytest.mark.parametrize("pattern", FORBIDDEN, ids=lambda p: p.pattern)
def test_no_legacy_docker_names(pattern):
    hits = []
    for path in _python_files():
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{path}:{i}: {line.strip()}")
    assert not hits, "legacy proving-ground-* Docker name reintroduced:\n" + "\n".join(hits)


def test_no_unallowlisted_proving_ground_string_literals():
    """Catches bare "proving-ground-*/'proving-ground-* string literals
    anywhere in backend/proving_ground that aren't one of the deliberately
    preserved sites in ALLOWED_LITERALS.
    """
    hits = []
    for path in _python_files():
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if not LITERAL_PATTERN.search(line):
                continue
            if any(allowed in line for allowed in ALLOWED_LITERALS):
                continue
            hits.append(f"{path}:{i}: {line.strip()}")
    assert not hits, (
        "unallowlisted proving-ground-* string literal found — either it's a "
        "regression (fix it) or a deliberate exemption missing from "
        "ALLOWED_LITERALS:\n" + "\n".join(hits)
    )


@pytest.mark.parametrize("name,needle", REQUIRED.items(), ids=list(REQUIRED))
def test_catalog_identifiers_preserved(name, needle):
    config = (BACKEND / "config.py").read_text(encoding="utf-8")
    assert (
        needle in config
    ), f"{name} was renamed. It is catalog-facing and must stay as-is — see CLAUDE.md."


def test_content_bucket_name_preserved():
    """The DEFAULT moved from a literal in api/content.py to a setting in config.py.

    What the guard protects is the name, not its address: renaming it orphans content already
    stored under the old bucket. Making it configurable is the opposite of renaming it -- an
    environment that pre-provisions buckets could not name them while it was a bare literal in
    two functions. The default is unchanged, and this asserts that.
    """
    config = (BACKEND / "config.py").read_text(encoding="utf-8")
    assert CONTENT_BUCKET_NEEDLE in config, (
        "proving-ground-content MinIO bucket name was renamed — this orphans "
        "content already stored under the old bucket. See CLAUDE.md."
    )
    content = (BACKEND / "api" / "content.py").read_text(encoding="utf-8")
    assert "content_bucket()" in content, (
        "api/content.py must resolve the bucket through object_store.content_bucket(), so the "
        "name has exactly one definition."
    )


@pytest.mark.skipif(not FRONTEND.exists(), reason="frontend not present")
def test_frontend_has_no_legacy_container_names():
    """The Image Cache page filters on platform container prefixes."""
    hits = []
    for path in list(FRONTEND.rglob("*.ts")) + list(FRONTEND.rglob("*.tsx")):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "proving-ground" not in line:
                continue
            if any(allowed in line for allowed in FRONTEND_ALLOWED):
                continue
            hits.append(f"{path}:{i}: {line.strip()}")
    assert not hits, "legacy proving-ground-* name in frontend:\n" + "\n".join(hits)


@pytest.mark.skipif(
    not SCRIPTS.exists(), reason="scripts/ not present (not mounted into the api container)"
)
def test_scripts_have_no_legacy_docker_names():
    """scripts/*.sh consumers of Docker object names must track the pg-* rename.

    deploy.sh's uninstall paths rely on `docker network ls --filter name=...`
    to find networks to remove; a stale filter here silently leaves
    infrastructure and range networks behind on uninstall.
    """
    hits = []
    for path in sorted(SCRIPTS.glob("*.sh")):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for pattern in SCRIPT_COMPOSE_FORBIDDEN:
                if pattern.search(line):
                    hits.append(f"{path}:{i}: {line.strip()}")
    assert not hits, "legacy proving-ground-* Docker name in scripts/:\n" + "\n".join(hits)


@pytest.mark.skipif(
    not COMPOSE_FILES, reason="docker-compose*.yml not present (not mounted into the api container)"
)
def test_compose_files_have_no_legacy_docker_names():
    """docker-compose*.yml must not pin explicit image: tags to the old names.

    Explicit `image:` tags are not derived from the Compose project name, so
    a project rename does not update them automatically.
    """
    hits = []
    for path in COMPOSE_FILES:
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            for pattern in SCRIPT_COMPOSE_FORBIDDEN:
                if pattern.search(line):
                    hits.append(f"{path}:{i}: {line.strip()}")
    assert not hits, "legacy proving-ground-* Docker name in docker-compose*.yml:\n" + "\n".join(
        hits
    )
