#!/usr/bin/env python3
"""Assemble one evidence bundle per release, from what the evidence stage produced.

    python3 scripts/build-evidence-bundle.py --evidence evidence/ --out dist/

One artifact, so an SSP or eMASS submission has a single thing to attach and a reviewer has a single
thing to read. It contains nothing this pipeline did not produce: no summaries of work done
elsewhere, no claims about controls, no assertion that anything is authorised.

The caveat that decides whether this is useful at all, stated in the bundle's own README because it
is where people get it wrong: **uploading an artifact to eMASS does not make it available for
inheriting. It has to be attached to a control.** A bundle sitting in an eMASS artifact library is
invisible to the assessor working a control, and inheritance is what makes evidence reusable.

What the bundle deliberately does NOT claim:

* Not an authorisation. Iron Bank's own words are that it "does not authorize or approve containers";
  neither does this. It produces the same kinds of evidence -- SBOM, vulnerability findings against a
  stated policy, a signature, provenance -- and what it cannot produce is an AO who already trusts
  the registry it came from.
* Not FIPS. FIPS 140-3 is a lab certificate against a specific crypto module build. It is inherited
  from a base that holds one, and self-hardening cannot manufacture it. If the bundle is silent on
  FIPS that is why.
* Not a STIG evaluation, on today's bases. See scap/scap-applicability.json for which profile was
  sought and why none applied.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import os
import pathlib
import shutil
import tarfile

BUNDLE_README = """# CYROID container hardening evidence — {version}

Produced by the `evidence` stage of pipeline {pipeline} from commit `{commit}` on {generated}.

## What is in here

| Path | What it is |
|---|---|
| `manifest.json` | every image in this release by digest, the tool versions that produced the evidence, and a sha256 of every file in this bundle |
| `sbom/*.spdx.json` | SPDX 2.3 software bill of materials, one per image |
| `sbom/*.cdx.json` | the same inventory as CycloneDX 1.5, because submissions ask for one or the other and rarely the same one |
| `vulnerabilities/*.grype.json` | every finding, at every severity, for each image |
| `vulnerabilities/policy-verdict.json` | which findings gated the build, which were excused and by which allowlist entry, and which were recorded and did not gate |
| `vulnerabilities/allowlist.yaml` | the allowlist exactly as it stood for this release, with each justification and expiry |
| `provenance/*.slsa.json` | SLSA v1.0 provenance predicate per image: source commit, builder, build definition |
| `signatures.md` | what was signed, and the command a verifier runs to check it |
| `scap/scap-applicability.json` | which DISA STIG profile was sought per image, and the determination |

## How to verify the images this describes

Every image is named by digest. A digest cannot be moved to different content, so a signature over
the digest covers exactly the bytes this evidence describes:

```
cosign verify --key <public key> <image>@<digest>
cosign verify-attestation --key <public key> --type slsaprovenance1 <image>@<digest>
cosign verify-attestation --key <public key> --type cyclonedx        <image>@<digest>
```

## What this evidence does not assert

**It is not an authorisation.** Iron Bank states that it "does not authorize or approve containers";
this pipeline makes no larger claim. It produces equivalent evidence. What it cannot produce is an
authorising official who already trusts the registry it came from — that is a reciprocity
relationship, not an artifact.

**It is not a FIPS claim.** FIPS 140-3 is a laboratory certificate against a specific crypto module
build. It is inherited from a base image that holds one; self-hardening cannot manufacture it.

**It is not a STIG evaluation** unless `scap/scap-applicability.json` says a profile applied. Read
that file before citing this bundle for a configuration-compliance control.

## Attaching this to an eMASS package

**Uploading this to eMASS does not make it available for inheriting — it must be attached to a
control.** An artifact in the library and an artifact attached to a control are different things to
an assessor working that control, and only the second one is inheritable. Attach it to the controls
the evidence actually speaks to (SA-11 for the scanning, SR-4/SR-11 for provenance and the SBOM,
SI-2 for flaw remediation, CM-6 where the SCAP determination applies) rather than to the package as
a whole.
"""


def sha256_of(path: pathlib.Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--evidence", type=pathlib.Path, default=pathlib.Path("evidence"))
    ap.add_argument("--out", type=pathlib.Path, default=pathlib.Path("dist"))
    ap.add_argument("--version")
    args = ap.parse_args()

    version = args.version or (
        pathlib.Path("VERSION").read_text().strip()
        if pathlib.Path("VERSION").is_file()
        else "0.0.0"
    )
    env = os.environ.get
    generated = datetime.datetime.now(datetime.timezone.utc).isoformat()

    if not args.evidence.is_dir():
        raise SystemExit(
            f"FATAL: {args.evidence} does not exist. The evidence stage produced nothing; "
            f"an empty bundle is worse than no bundle because it looks like evidence."
        )

    stage = args.out / f"cyroid-evidence-{version}"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    # Copy what exists; record what does not, rather than quietly shipping a thinner bundle.
    wanted = {
        "sbom": "sbom",
        "vulnerabilities": "vulnerabilities",
        "provenance": "provenance",
        "scap": "scap",
    }
    missing = []
    for src_name, dst_name in wanted.items():
        src = args.evidence / src_name
        if src.is_dir() and any(src.iterdir()):
            shutil.copytree(src, stage / dst_name)
        else:
            missing.append(src_name)

    allowlist = pathlib.Path("ironbank/vulnerability-allowlist.yaml")
    if allowlist.is_file():
        (stage / "vulnerabilities").mkdir(exist_ok=True)
        shutil.copy2(allowlist, stage / "vulnerabilities" / "allowlist.yaml")

    digests = {}
    digest_dir = args.evidence / "digests"
    if digest_dir.is_dir():
        for f in sorted(digest_dir.glob("*.digest")):
            digests[f.stem] = f.read_text().strip()

    signatures = args.evidence / "signatures.md"
    if signatures.is_file():
        shutil.copy2(signatures, stage / "signatures.md")
    else:
        missing.append("signatures.md")

    (stage / "README.md").write_text(
        BUNDLE_README.format(
            version=version,
            pipeline=env("CI_PIPELINE_URL", "(local run)"),
            commit=env("CI_COMMIT_SHA", "(unknown)"),
            generated=generated[:19].replace("T", " ") + " UTC",
        )
    )

    files = {}
    for path in sorted(stage.rglob("*")):
        if path.is_file():
            files[str(path.relative_to(stage))] = sha256_of(path)

    manifest = {
        "product": "CYROID",
        "version": version,
        "generated": generated,
        "commit": env("CI_COMMIT_SHA", ""),
        "pipeline": env("CI_PIPELINE_URL", ""),
        "images": [{"image": name, "digest": digest} for name, digest in sorted(digests.items())],
        "tools": {k: env(k, "") for k in ("SYFT_IMAGE", "GRYPE_IMAGE", "COSIGN_IMAGE") if env(k)},
        "grype_db": env("GRYPE_DB_STATUS", ""),
        "missing": missing,
        "files": files,
        "caveats": [
            "Not an authorisation. Iron Bank does not authorize or approve containers, and neither does this.",
            "Not a FIPS 140-3 claim. FIPS is inherited from a validated base, never produced by self-hardening.",
            "Uploading to eMASS does not make this inheritable -- it must be attached to a control.",
        ],
    }
    (stage / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")

    tarball = args.out / f"cyroid-evidence-{version}.tar.gz"
    with tarfile.open(tarball, "w:gz") as tar:
        tar.add(stage, arcname=stage.name)

    print(f"bundle: {tarball} ({tarball.stat().st_size} bytes)")
    for name, digest in sorted(digests.items()):
        print(f"  {name} @ {digest}")
    print(f"  {len(files)} file(s)")
    if missing:
        print(
            f"  MISSING, recorded in the manifest rather than omitted silently: {', '.join(missing)}"
        )
    if not digests:
        print("FATAL: no image digests. The bundle cannot name what it describes.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
