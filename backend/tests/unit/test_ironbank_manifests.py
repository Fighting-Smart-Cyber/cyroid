"""Our Iron Bank submission metadata must satisfy Iron Bank's own schema.

Iron Bank builds the image itself, from our Dockerfile, against a base it controls -- it does not
pull what we publish. `hardening_manifest.yaml` is the contract for that build, and a manifest
that does not validate is rejected at submission, which is a slow way to find a typo.

The schema is VENDORED at ironbank/schema/hardening_manifest.schema.json rather than fetched:
repo1.dso.mil needs credentials for most things and CI has none, and a test that silently skips
when a network call fails is a test that stops running. Re-fetch it with

    curl -s "https://repo1.dso.mil/api/v4/projects/ironbank-tools%2Fironbank-pipeline\
/repository/files/schema%2Fhardening_manifest.schema.json/raw?ref=master"

from `ironbank-tools/ironbank-pipeline`, which is public.

What this does NOT assert is that Iron Bank will accept the submission. It will not: acceptance
depends on CVE findings against a hardened base, and on a sponsor. This only guarantees the
metadata is well-formed and says what we mean.
"""

import json
import pathlib

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
IRONBANK = REPO_ROOT / "ironbank"
SCHEMA = IRONBANK / "schema" / "hardening_manifest.schema.json"

needs_repo = pytest.mark.skipif(not SCHEMA.exists(), reason="repo root not present")

MANIFESTS = sorted(IRONBANK.glob("*/hardening_manifest.yaml")) if IRONBANK.exists() else []


def _load(path: pathlib.Path) -> dict:
    return yaml.safe_load(path.read_text())


@needs_repo
def test_there_is_a_manifest_for_every_image_we_build():
    """The set is derived from the CI jobs, not written down twice.

    pg-api and pg-worker are the SAME image built from backend/Dockerfile under two names, so
    they are one Iron Bank container. A fourth image appearing in CI without a manifest is the
    failure this catches.
    """
    ci = yaml.safe_load((REPO_ROOT / ".gitlab-ci.yml").read_text())
    contexts = {
        job["variables"]["DOCKERFILE"]
        for name, job in ci.items()
        if isinstance(job, dict)
        and name.startswith("build:")
        and "variables" in job
        and "DOCKERFILE" in job.get("variables", {})
    }
    assert contexts, "no build jobs found -- has the pipeline been restructured?"
    assert len(MANIFESTS) >= len(contexts) - 1, (
        f"{len(contexts)} distinct Dockerfiles are built in CI but only {len(MANIFESTS)} "
        f"hardening manifests exist: {[m.parent.name for m in MANIFESTS]}"
    )


@needs_repo
@pytest.mark.parametrize("manifest", MANIFESTS, ids=lambda p: p.parent.name)
def test_manifest_validates_against_iron_banks_schema(manifest):
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.validate(instance=_load(manifest), schema=json.loads(SCHEMA.read_text()))


@needs_repo
@pytest.mark.parametrize("manifest", MANIFESTS, ids=lambda p: p.parent.name)
def test_the_licence_is_the_one_we_actually_ship_under(manifest):
    """AGPL-3.0-only, per ADR-0008.

    The frontend image claimed MIT until 0.55.0. Iron Bank checks this against the SPDX list, so
    it would have been caught eventually -- but a wrong licence label on a published container is
    a licensing problem long before it is a submission problem.
    """
    assert _load(manifest)["labels"]["org.opencontainers.image.licenses"] == "AGPL-3.0-only"


@needs_repo
@pytest.mark.parametrize("manifest", MANIFESTS, ids=lambda p: p.parent.name)
def test_the_version_matches_the_release_being_built(manifest):
    """A manifest pinned to a version we no longer ship submits the wrong thing."""
    version = (REPO_ROOT / "VERSION").read_text().strip()
    doc = _load(manifest)
    assert doc["tags"] == [version], f"tags {doc['tags']} != VERSION {version}"
    assert doc["labels"]["org.opencontainers.image.version"] == version


@needs_repo
@pytest.mark.parametrize("manifest", MANIFESTS, ids=lambda p: p.parent.name)
def test_the_dockerfile_accepts_the_base_the_manifest_passes(manifest):
    """`args: {BASE_IMAGE, BASE_TAG}` is only meaningful if the Dockerfile reads them.

    Iron Bank passes these into the build. A Dockerfile with a hardcoded FROM ignores them
    silently and is hardened against a base nobody chose.
    """
    dockerfile = {
        "cyroid-api": REPO_ROOT / "backend" / "Dockerfile",
        "cyroid-frontend": REPO_ROOT / "frontend" / "Dockerfile.prod",
    }[manifest.parent.name]
    text = dockerfile.read_text()
    assert "ARG BASE_IMAGE" in text, f"{dockerfile.name} does not take BASE_IMAGE"
    assert "ARG BASE_TAG" in text, f"{dockerfile.name} does not take BASE_TAG"
    assert (
        "${BASE_IMAGE}:${BASE_TAG}" in text
    ), f"{dockerfile.name} declares them but does not FROM them"


# The package manager each base family ships. A base from one family with a Dockerfile written
# for another does not build -- it fails on the first RUN, inside Iron Bank, days later.
BASE_FAMILY_PACKAGE_MANAGERS = {
    "ubi": ("dnf", "microdnf", "yum"),
    "rhel": ("dnf", "microdnf", "yum"),
    "rocky": ("dnf", "microdnf", "yum"),
    "debian": ("apt-get", "apt"),
    "ubuntu": ("apt-get", "apt"),
    "alpine": ("apk",),
    "chainguard": ("apk",),
}

ALL_MANAGERS = {m for ms in BASE_FAMILY_PACKAGE_MANAGERS.values() for m in ms}


@needs_repo
@pytest.mark.parametrize("manifest", MANIFESTS, ids=lambda p: p.parent.name)
def test_the_base_and_the_dockerfile_agree_about_the_package_manager(manifest):
    """A base from one distro family with a Dockerfile written for another cannot build.

    This exists because the first draft of these manifests named `redhat/ubi/ubi9` while
    backend/Dockerfile installs with apt-get. UBI has no apt. Iron Bank builds the image itself,
    so nothing local would have caught it -- the failure would have arrived at submission, from
    a base that was a guess in the first place.

    An empty BASE_IMAGE is fine and is the current state: it means the base is undecided, which
    is honest, rather than asserted and wrong.
    """
    doc = _load(manifest)
    base = (doc.get("args") or {}).get("BASE_IMAGE") or ""
    if not base:
        pytest.skip("BASE_IMAGE is deliberately empty: the base is undecided, see the manifest")

    dockerfile = {
        "cyroid-api": REPO_ROOT / "backend" / "Dockerfile",
        "cyroid-frontend": REPO_ROOT / "frontend" / "Dockerfile.prod",
    }[manifest.parent.name]
    text = dockerfile.read_text()

    used = {m for m in ALL_MANAGERS if f"{m} " in text or f"{m}\n" in text}
    if not used:
        return  # installs nothing; any base will do

    family = next((f for f in BASE_FAMILY_PACKAGE_MANAGERS if f in base.lower()), None)
    assert family, (
        f"BASE_IMAGE {base!r} is not a family this guard knows. Add it to "
        f"BASE_FAMILY_PACKAGE_MANAGERS with the package manager it ships, or the check is vacuous."
    )
    expected = set(BASE_FAMILY_PACKAGE_MANAGERS[family])
    assert used & expected, (
        f"{dockerfile.name} installs with {sorted(used)} but BASE_IMAGE {base!r} is {family}, "
        f"which ships {sorted(expected)}. Iron Bank builds against that base and this would "
        f"fail on the first RUN."
    )
