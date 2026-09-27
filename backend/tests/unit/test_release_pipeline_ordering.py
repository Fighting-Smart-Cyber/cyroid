"""The chart must be published after the images its appVersion names.

`kubernetes_update.check()` decides whether an update is available by listing **chart** versions
in the registry, and the chart carries no image tag of its own -- an empty `image.tag` means the
chart's appVersion, which `build:chart` sets to the release version. So the chart appearing in the
registry is, in effect, the announcement that a release is installable.

It was not ordered that way. `build:chart`, `build:api`, `build:worker` and `build:frontend` all
sit in the `build` stage with no `needs` between them, so they started together -- and a helm
package plus push takes seconds against multi-minute image builds. Observed on the v0.53.1 tag
pipeline: `build:chart` had succeeded and published chart 0.53.1 while all three image jobs were
still running and no `0.53.1` tag existed for any of them.

An in-cluster host that polled `/admin/infrastructure/update/check` in that window was offered
0.53.1 and, on pressing Update, would have advanced its HelmRelease to a chart whose appVersion
named images that did not exist -- ImagePullBackOff across every Deployment, including the API
that would have had to report it.

This is a property of the *pipeline*, not of any job, which is why no job's own success could
catch it.
"""

import pathlib
import re

import pytest

yaml = pytest.importorskip("yaml")

ROOT = pathlib.Path(__file__).resolve().parents[3]
CI = ROOT / ".gitlab-ci.yml"

# The images the chart's appVersion resolves to. `pg-storage` and the other infrastructure
# mirrors are deliberately NOT here: the chart pins them by digest, not by the release version,
# so the chart does not depend on `build:infra` -- which is as well, since making every release
# wait on a job that mirrors upstream bases would let an upstream withdrawal block a release.
IMAGE_JOBS = {"build:api", "build:worker", "build:frontend"}


@pytest.fixture(scope="module")
def pipeline() -> dict:
    return yaml.safe_load(CI.read_text())


def test_the_chart_waits_for_the_images_its_app_version_names(pipeline):
    needs = set(pipeline["build:chart"].get("needs") or [])
    missing = IMAGE_JOBS - needs
    assert not missing, (
        "build:chart must `needs` the image jobs, or it publishes a chart whose appVersion "
        f"names images that do not exist yet. Missing: {sorted(missing)}"
    )


def test_those_jobs_are_all_in_one_stage_so_needs_is_what_orders_them(pipeline):
    """If they were split across stages the ordering would come for free and `needs` would be
    noise -- this asserts the reason the `needs` above is load-bearing."""
    stages = {job: pipeline[job].get("stage") for job in IMAGE_JOBS | {"build:chart"}}
    assert set(stages.values()) == {"build"}, stages


def test_the_chart_does_not_wait_for_the_infrastructure_mirrors(pipeline):
    """`build:infra` mirrors upstream bases and builds pg-dind; the chart references none of them
    by release version. It also fails whenever an upstream image is withdrawn -- which happened to
    quay.io/minio/minio before v0.53.0 -- and a release must not be blocked by that."""
    assert "build:infra" not in set(pipeline["build:chart"].get("needs") or [])


# Where the deployed MinIO reference lives. It was the chart's values.yaml until the chart
# stopped naming our registry (a private host in a public engine); scripts/install-k8s.sh
# reads this and passes it in, so this is the file that actually runs the image.
DEPLOYED_PIN = ROOT / "scripts" / "registry.env"


class TestTheInfrastructureMirrorDoesNotNeedUpstreamTwice:
    """`build:infra` failed on every tag from 0.52.1 because it re-pulled an image we had.

    MinIO withdrew `minio/minio` from Docker Hub; CI moved to quay.io; quay.io then refused
    anonymous pulls too -- the pinned digest, `:latest` and the tag list all answer 401. Each
    time, the artifact was already sitting in our own registry, pinned by digest, and being
    deployed from there: pg-devtest ran `pg-storage@sha256:52dfd5c0...` throughout.

    A digest names one exact image, so a copy we hold is the thing the pin asks for. The job now
    prefers our registry and keeps upstream only to seed a registry that lacks the image.
    """

    def test_the_storage_digest_is_read_from_the_deploying_file_not_repeated_in_ci(self, pipeline):
        """One copy of the digest. A second would drift, and the fast path would then serve an
        image nothing deploys -- silently, because both calls would succeed.

        Asserted on the mechanism, not on a filename appearing somewhere: a comment mentioning
        the file satisfied an earlier version of this test while the script still hardcoded the
        digest.
        """
        script = pipeline["build:infra"]["script"][0]
        deployed = re.search(r"pg-storage@(sha256:[a-f0-9]{64})", DEPLOYED_PIN.read_text())
        assert deployed, f"{DEPLOYED_PIN.name} pins no pg-storage digest"
        chart_digest = deployed.group(1)

        derives = [
            ln
            for ln in script.splitlines()
            if "registry.env" in ln and "grep" in ln and not ln.strip().startswith("#")
        ]
        assert derives, "build:infra must grep the digest out of scripts/registry.env"

        call = [ln for ln in script.splitlines() if "keep_or_mirror pg-storage" in ln]
        assert call, "pg-storage must go through keep_or_mirror"
        assert chart_digest not in call[0], (
            f"the digest is hardcoded at the call site as well as pinned in values.yaml: "
            f"{call[0].strip()}"
        )
        assert (
            chart_digest not in script
        ), "build:infra repeats the mirror digest values.yaml already pins; they will drift"

    def test_it_refuses_rather_than_skipping_when_the_chart_pins_nothing(self, pipeline):
        script = pipeline["build:infra"]["script"][0]
        assert "no pg-storage digest pinned in scripts/registry.env" in script
        assert "exit 1" in script

    def test_upstream_is_kept_as_the_way_to_seed_a_registry_that_lacks_it(self, pipeline):
        """Preferring our copy must not become "never mirror" -- a fresh or air-gapped registry
        has nothing to prefer."""
        assert "quay.io/minio/minio@sha256:" in pipeline["build:infra"]["script"][0]

    def test_only_a_digest_pinned_source_prefers_our_copy(self, pipeline):
        """A floating tag means "whatever upstream calls that now". Serving our copy instead
        would freeze it silently and the mirror would stop being a mirror."""
        script = pipeline["build:infra"]["script"][0]
        for floating in ("traefik:v2.11", "postgres:16-alpine", "redis:7-alpine"):
            line = next(ln for ln in script.splitlines() if floating in ln and "mirror" in ln)
            assert "keep_or_mirror" not in line, f"{floating} is a floating tag: {line.strip()}"
