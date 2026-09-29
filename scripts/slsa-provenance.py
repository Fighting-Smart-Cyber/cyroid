#!/usr/bin/env python3
"""Emit an SLSA v1.0 provenance predicate for one image, from what CI knows about the build.

    python3 scripts/slsa-provenance.py --image pg-api --digest sha256:... --out provenance.json

`cosign attest --type slsaprovenance1` wraps this in an in-toto statement and signs it, so what is
written here is the predicate body only.

What provenance is for, and what this one honestly claims: it says which source commit, which
builder and which build definition produced the image with this digest, signed by a key a verifier
can check. That is the answer to "is this image the one built from the code you showed me".

What it does NOT claim is SLSA Build Level 3. Level 3 requires the build platform to generate the
provenance itself, in a way the build steps cannot influence -- and this predicate is produced by a
build step, from environment variables that the same pipeline sets. An honest reading is Build Level
1 (provenance exists, and it is complete and signed), approaching Level 2 by virtue of a
managed builder and a signing key CI holds but jobs cannot print. Claiming 3 for a script the build
runs on itself is the sort of thing an assessor catches, and it costs more credibility than it buys.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import pathlib
import subprocess

# SLSA v1.0. The build type is a URI naming HOW the build was done. It is ours to define, and it
# points at the file that actually defines it -- derived from CI_PROJECT_URL rather than written down,
# because this script is published with CYROID and no internal hostname may appear in a published file
# (backend/tests/unit/test_public_snapshot_is_clean.py enforces that, and it is right to).
BUILD_TYPE_PATH = "/-/blob/master/.gitlab-ci.yml"
# Used when there is no CI_PROJECT_URL, e.g. a local run: a stable URI that says what the build was
# without naming where it happened.
BUILD_TYPE_FALLBACK = "https://slsa.dev/container-based-build/v0.1?gitlab-ci"


def build_type() -> str:
    project = os.environ.get("CI_PROJECT_URL")
    return f"{project}{BUILD_TYPE_PATH}" if project else BUILD_TYPE_FALLBACK


def _git(*args: str) -> str:
    try:
        return subprocess.check_output(["git", *args], text=True).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return ""


def build(image: str, digest: str, started: str | None) -> dict:
    env = os.environ.get
    commit = env("CI_COMMIT_SHA") or _git("rev-parse", "HEAD")
    repo = env("CI_PROJECT_URL") or ""
    ref = env("CI_COMMIT_REF_NAME") or _git("rev-parse", "--abbrev-ref", "HEAD")
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()

    return {
        "buildDefinition": {
            "buildType": build_type(),
            # What a rebuilder would have to be told to reproduce this.
            "externalParameters": {
                "source": {
                    "uri": f"git+{repo}@{ref}" if repo else ref,
                    "digest": {"gitCommit": commit},
                },
                "image": image,
                "dockerfile": env("DOCKERFILE", ""),
                "context": env("CONTEXT", ""),
                "buildArgs": {
                    "APP_VERSION": (
                        pathlib.Path("VERSION").read_text().strip()
                        if pathlib.Path("VERSION").is_file()
                        else ""
                    )
                },
                "platforms": env("BUILD_PLATFORMS", "linux/amd64"),
            },
            # What the platform chose, which a rebuilder does not control.
            "internalParameters": {
                "gitlabPipelineId": env("CI_PIPELINE_ID", ""),
                "gitlabJobId": env("CI_JOB_ID", ""),
                "runner": env("CI_RUNNER_DESCRIPTION", ""),
                "runnerTags": env("CI_RUNNER_TAGS", ""),
            },
            "resolvedDependencies": [
                {
                    "uri": f"git+{repo}@{ref}" if repo else ref,
                    "digest": {"gitCommit": commit},
                }
            ],
        },
        "runDetails": {
            "builder": {
                # The builder identity. A verifier that trusts this pipeline trusts this string, so it
                # names the project and the runner rather than "gitlab".
                "id": f"{repo}/-/pipelines" if repo else "local",
                "version": {"gitlab-runner": env("CI_RUNNER_VERSION", "")},
            },
            "metadata": {
                "invocationId": env("CI_PIPELINE_URL", ""),
                "startedOn": started or now,
                "finishedOn": now,
            },
            # The subject digest is repeated here for readers; cosign puts the authoritative one in
            # the in-toto statement's subject.
            "byproducts": [{"name": image, "digest": {"sha256": digest.removeprefix("sha256:")}}],
        },
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", required=True)
    ap.add_argument("--digest", required=True)
    ap.add_argument("--started")
    ap.add_argument("--out", type=pathlib.Path, required=True)
    args = ap.parse_args()

    if not args.digest.startswith("sha256:"):
        raise SystemExit(f"--digest must be a sha256 digest, got {args.digest!r}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(build(args.image, args.digest, args.started), indent=2) + "\n")
    print(f"wrote {args.out} for {args.image}@{args.digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
