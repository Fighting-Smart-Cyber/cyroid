# Container hardening evidence

CYROID's images are hardened and evidenced by CYROID's own pipeline. This document says what that
produces, what it proves, and the two things it cannot do — one of which is waiting on a decision
nobody has made yet.

## Why not Iron Bank

Iron Bank is the obvious answer and it is not available to us yet: submission needs a repo1 account
and a sponsor, and we have neither. More usefully, Iron Bank would not close this gap even if we had
both, because **Iron Bank does not authorize or approve containers**. It produces evidence, and it
produces a reciprocity relationship — a registry that authorising officials already trust.

The evidence half is ours to produce, and this pipeline produces it. The relationship half is not
something a pipeline can manufacture, and nothing here claims otherwise. When an AO asks "is this
image from Iron Bank", the answer is no; when they ask "show me the SBOM, the findings against a
stated policy, the signature and the provenance", the answer is this bundle.

## What each release produces

The `evidence` stage runs where the image builds run, and every job either produces its artifact or
fails. None is `allow_failure`, and none ends in `|| true`.

| Job | Output | Gates? |
|---|---|---|
| `evidence:sbom` | SPDX 2.3 and CycloneDX 1.5 per image, plus syft's own JSON | fails if syft produces nothing |
| `evidence:vulnerability` | every grype finding per image, at every severity | fails if the database is unavailable |
| `evidence:vulnerability-policy` | the verdict: blocking, excused, recorded | **yes** — see the threshold below |
| `evidence:provenance` | SLSA v1.0 provenance predicate per image | fails if a digest is missing |
| `evidence:signature` | cosign signature, SLSA and CycloneDX attestations, pushed beside the image | **yes on a tag** — a release with unverifiable images fails |
| `evidence:scap` | which DISA STIG profile applies per image, and why | records the determination |
| `evidence:bundle` | one `.tar.gz` for an SSP or eMASS submission | fails if it cannot name what it describes |

Everything is named by **digest**, never by tag. A tag can be moved to different content; a digest
cannot, so a signature over a digest covers exactly the bytes that were scanned.

## The vulnerability threshold

Stated in `ironbank/vulnerability-allowlist.yaml`, which is also the only thing that suppresses a
finding:

- **Critical and High gate the build.** Medium and Low are recorded in the bundle and do not gate.
- **Only findings with a fix available gate.** A finding with no upstream fix cannot be actioned here;
  the action is a different base image, which is the decision below. Unfixable findings are still
  counted, still in the SBOM, still in the bundle — an assessor sees every one.
- **An exception needs a justification, a named person and an expiry**, and no entry may run longer
  than the policy's window. An expired entry fails the pipeline, which forces the finding back in
  front of someone instead of ageing into permanence.

`backend/tests/unit/test_vulnerability_allowlist.py` enforces those rules, and
`backend/tests/unit/test_vulnerability_policy.py` tests the threshold against findings written on
purpose — an expired entry, an unfixable Critical, a missing scan file — so the gate's behaviour is
known before a release depends on it.

## STIG/SCAP: blocked on a base decision

**No DISA STIG profile applies to the images as they are built today.** This is not a tooling gap and
not an air-gap problem. The content does not exist:

| Image | Base | ComplianceAsCode content | DISA `stig` profile |
|---|---|---|---|
| `pg-api`, `pg-worker` | `python:3.11-slim` → Debian 12 | yes — ANSSI BP-028, CIS | **no** |
| `pg-frontend` | `nginx:alpine` → Alpine | **none at all** | no |

DISA has published no Debian STIG, and Alpine has no SCAP product upstream. Running a CIS profile and
filing the result as STIG evidence would be a misrepresentation, so `evidence:scap` records the
determination — which profile was sought, per image, and why none applied — and runs no evaluation.

The options, both of which have consequences outside CI:

1. **Move the final stage to a base DISA does publish a STIG for** — UBI 8/9/10, or Ubuntu
   22.04/24.04. The `BASE_IMAGE`/`BASE_TAG` build arguments already exist for this. Note that
   `backend/Dockerfile` installs with `apt-get`, so a UBI base needs the package steps rewritten for
   `microdnf` first; a test guards that mismatch rather than letting the build discover it.
2. **State in the SSP that the container OS is covered by the host's STIG** and assess the image
   against CIS instead, with the CIS result offered as what it is.

This is deliberately left open. `ironbank/*/hardening_manifest.yaml` carries an empty `BASE_IMAGE`
for the same reason, and the manifest tests skip with that stated rather than asserting a base nobody
has chosen.

## FIPS

**Self-hardening cannot produce a FIPS 140-3 claim.** FIPS validation is a laboratory certificate
against a specific crypto module build. It is inherited from a base that holds one — UBI, Ubuntu Pro
FIPS, Chainguard FIPS — or it is absent. If a control asks for FIPS-validated cryptography, the answer
is a base decision, the same decision as above, and not something this pipeline can assert.

## The air gap

ADR-0007 forbids a public-registry dependency **at runtime**, and this stage does not create one: the
product still pulls nothing at runtime, and every image it references is pinned by digest.

What the stage does need at **build** time is grype's vulnerability database. A disconnected build has
to stage it:

```
GRYPE_DB_ARCHIVE=/path/or/url/inside/the/enclave
```

With that set, the scan imports from there and auto-update is off. Left unset, CI updates from the
public database and records which database it used in the bundle's manifest. A disconnected build with
neither set **fails** — it does not pass quietly against an empty or stale database, because a scan
with no data finds nothing and that is indistinguishable from a clean image.

The same applies to a SCAP datastream if a base decision ever makes one applicable: vendor it or stage
it, the way `ironbank/schema/hardening_manifest.schema.json` is vendored.

## Verifying a release

```
cosign verify             --key cosign.pub <registry>/<image>@sha256:...
cosign verify-attestation --key cosign.pub --type slsaprovenance1 <registry>/<image>@sha256:...
cosign verify-attestation --key cosign.pub --type cyclonedx       <registry>/<image>@sha256:...
```

The digests are in the bundle's `manifest.json`, and `signatures.md` repeats the exact commands for
the images in that release.

## Attaching the bundle to an eMASS package

**Uploading an artifact to eMASS does not make it available for inheriting — it must be attached to a
control.** An artifact sitting in the library and an artifact attached to a control are different
things to the assessor working that control, and only the second is inheritable. Attach the bundle to
the controls the evidence speaks to rather than to the package as a whole:

| Control | What in the bundle speaks to it |
|---|---|
| SA-11 (developer testing) | the scan findings and the policy verdict |
| SI-2 (flaw remediation) | the policy verdict, and the allowlist with its expiries |
| SR-4, SR-11 (provenance, component authenticity) | the SLSA provenance and the signatures |
| CM-6 (configuration settings) | the SCAP determination — read it before citing this for CM-6 |

## What this is not

- Not an authorisation. See the first section.
- Not a FIPS claim. See above.
- Not a STIG evaluation, unless the determination says a profile applied.
- Not an Iron Bank submission. `ironbank/` holds the metadata a submission would need, validated
  against Iron Bank's own schema, and that is all it is.
