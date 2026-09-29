#!/usr/bin/env python3
"""Decide which DISA STIG profile applies to each image we build -- and say so when none does.

    python3 scripts/scap-applicability.py evidence/sbom/pg-api.syft.json --out evidence/scap/

Read this before assuming the STIG job is missing. It is not missing; the profile is.

A SCAP evaluation needs content written for the operating system inside the image. DISA publishes
STIGs for a specific list of operating systems, and ComplianceAsCode -- the upstream that turns them
into machine-readable SCAP datastreams -- ships a `stig` profile only for that list. For the bases
this repository currently builds on:

* `python:3.11-slim` is Debian 12. ComplianceAsCode ships Debian 12 content, but its profiles are
  ANSSI BP-028 and CIS. There is NO `stig` profile, because DISA has not published a Debian STIG.
* `nginx:alpine` is Alpine. There is no ComplianceAsCode product for Alpine at all -- no datastream,
  no profiles, nothing to evaluate against.

So "run OpenSCAP against the applicable DISA profile" cannot be satisfied on today's bases. Not
because of tooling, and not because of the air gap: the content does not exist. The options are to
move the final stage to a base DISA does publish a STIG for (UBI 8/9/10, Ubuntu 22.04/24.04), or to
state in the SSP that the container OS is covered by the host's STIG and the image is assessed
against CIS instead. Both are decisions with consequences beyond CI, which is why this script
produces the determination and refuses to invent a verdict.

The distro comes from the SBOM syft already produced, not from exporting the image filesystem. That
keeps this job free of a container runtime and free of the `openscap-ocp` image, which ships neither
`tar` nor `unzip` and so cannot unpack a datastream without help.

The mapping below is VENDORED for the same reason the Iron Bank schema is: an air-gapped build cannot
query GitHub, and a check that silently passes when a network call fails has stopped being a check.
Refresh it from
https://github.com/ComplianceAsCode/content/tree/master/products/<product>/profiles
"""

from __future__ import annotations

import argparse
import datetime
import json
import pathlib
import sys

# distro id (as syft reports it) -> {version prefix: (ComplianceAsCode product, has a DISA stig profile)}
# Verified against ComplianceAsCode master on 2026-09-28.
SCAP_CONTENT = {
    "rhel": {"8": ("rhel8", True), "9": ("rhel9", True), "10": ("rhel10", True)},
    "rocky": {"8": ("rhel8", True), "9": ("rhel9", True)},
    "almalinux": {"9": ("rhel9", True)},
    "ol": {"8": ("ol8", True), "9": ("ol9", True)},
    "ubuntu": {
        "22.04": ("ubuntu2204", True),
        "24.04": ("ubuntu2404", True),
        "26.04": ("ubuntu2604", False),
    },
    # Content exists; the profiles are ANSSI and CIS. No DISA STIG.
    "debian": {"11": ("debian11", False), "12": ("debian12", False), "13": ("debian13", False)},
    # No product at all.
    "alpine": {},
}

STIG_PROFILE = "xccdf_org.ssgproject.content_profile_stig"


def determine(distro_id: str, version: str) -> dict:
    families = SCAP_CONTENT.get(distro_id)
    if families is None:
        return {
            "applicable": False,
            "reason": (
                f"{distro_id!r} is not a distribution ComplianceAsCode publishes content for, so there "
                f"is no datastream to evaluate against -- DISA or otherwise."
            ),
        }
    if not families:
        return {
            "applicable": False,
            "reason": (
                f"{distro_id} has no ComplianceAsCode product. Alpine and other musl distributions have "
                f"no SCAP content at all; this is an absence of content, not a clean result."
            ),
        }
    for prefix, (product, has_stig) in sorted(families.items(), key=lambda kv: -len(kv[0])):
        if version.startswith(prefix):
            if has_stig:
                return {
                    "applicable": True,
                    "product": product,
                    "datastream": f"ssg-{product}-ds.xml",
                    "profile": STIG_PROFILE,
                    "reason": f"DISA publishes a STIG for {distro_id} {version}; evaluate {product} against it.",
                }
            return {
                "applicable": False,
                "product": product,
                "reason": (
                    f"ComplianceAsCode ships {product} content, but it has no `stig` profile: DISA has "
                    f"not published a STIG for {distro_id} {version}. CIS and ANSSI profiles exist and "
                    f"would produce real findings, but they are not STIG evidence and must not be "
                    f"described as such."
                ),
            }
    return {
        "applicable": False,
        "product": None,
        "reason": (
            f"No content matches {distro_id} {version}. Known versions: "
            f"{', '.join(sorted(families))}."
        ),
    }


def distro_of(sbom: dict) -> tuple[str, str]:
    d = sbom.get("distro") or {}
    return (d.get("id") or "").lower(), str(d.get("versionID") or d.get("version") or "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sboms", nargs="+", help="syft-json SBOMs, one per image")
    ap.add_argument("--out", type=pathlib.Path, required=True)
    args = ap.parse_args()

    args.out.mkdir(parents=True, exist_ok=True)
    report = {
        "generated": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "profile_sought": STIG_PROFILE,
        "images": [],
    }
    for path in args.sboms:
        p = pathlib.Path(path)
        if not p.is_file():
            print(
                f"FATAL: no SBOM at {p}. Without it the distribution is unknown, and an unknown "
                f"distribution is not an inapplicable one.",
                file=sys.stderr,
            )
            return 2
        sbom = json.loads(p.read_text())
        distro_id, version = distro_of(sbom)
        if not distro_id:
            print(
                f"FATAL: {p} records no distribution. syft could not identify the base; treat that "
                f"as a finding, not as 'no STIG applies'.",
                file=sys.stderr,
            )
            return 2
        entry = {
            "image": p.name.split(".")[0],
            "distro": f"{distro_id} {version}".strip(),
            **determine(distro_id, version),
        }
        report["images"].append(entry)

    (args.out / "scap-applicability.json").write_text(json.dumps(report, indent=2) + "\n")

    applicable = [e for e in report["images"] if e["applicable"]]
    print(f"DISA STIG applicability, profile sought: {STIG_PROFILE}")
    for e in report["images"]:
        print(f"\n  {e['image']} — {e['distro']}")
        print(f"    applicable: {'YES' if e['applicable'] else 'NO'}")
        print(f"    {e['reason']}")
    if not applicable:
        print(
            "\nNo image has an applicable DISA STIG profile, so no STIG evaluation was run. This is "
            "recorded as a gap in the evidence bundle rather than reported as a pass.\n"
            "The decision this needs is a base decision, not a CI change — see "
            "docs/security/container-hardening-evidence.md."
        )
    else:
        print(f"\n{len(applicable)} image(s) have an applicable profile; evaluate them with oscap.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
