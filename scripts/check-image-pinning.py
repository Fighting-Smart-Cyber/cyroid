#!/usr/bin/env python3
"""Every image reference must name a digest. ADR-0007, enforced rather than ratcheted.

    python3 scripts/check-image-pinning.py [--root .]

This replaces a ratchet that could not have worked. The old check extracted matches with
`grep -oE '(FROM|image:)\\s+[\\w./-]+:[\\w.-]+'` and then filtered the extracted text for `@sha256`.
`grep -o` prints only the part that matched, and the pattern stops at the tag -- so a correctly
pinned `image: postgres:16@sha256:...` was extracted as `image: postgres:16`, found not to contain
`@sha256`, and counted as unpinned. The baseline of 18 was therefore counting pinned and unpinned
references alike, and pinning something could not lower it. Ratchets are only useful if the number
means what it says.

Two exemptions, both narrow and both stated in the output so a reader sees what was not checked:

* **Images this repository builds locally.** `pg-api`, `pg-worker`, `pg-frontend` and
  `pg-frontend-serve` are produced by the Compose project itself and exist in no registry, so there
  is no digest to pin them to. What ADR-0007 forbids -- pulling an unpinned image at runtime -- does
  not apply to an image that is never pulled.
* **References whose tag is a variable**, e.g. `${VERSION:-latest}` in docker-compose.prod.yml. Those
  resolve at deploy time from `scripts/registry.env`, which is where the digest lives for the one
  image that needs it. Pinning them here would mean hardcoding a release into a file that is meant to
  follow releases. This is the exemption most worth revisiting, and it is listed explicitly on every
  run rather than silently skipped.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

# `FROM x`, `FROM x AS y`, `image: x`. The reference is the token after the keyword.
REFERENCE = re.compile(r"^\s*(?:FROM|image:)\s+(\S+)", re.IGNORECASE)

LOCALLY_BUILT = {"pg-api", "pg-worker", "pg-frontend", "pg-frontend-serve"}

GLOBS = ("Dockerfile*", "docker-compose*.yml", "**/Dockerfile*", "**/docker-compose*.yml")


def is_locally_built(ref: str) -> bool:
    return ref.split(":")[0].split("/")[-1] in LOCALLY_BUILT and "/" not in ref.split(":")[0]


def scan(root: pathlib.Path):
    pinned, exempt_local, exempt_var, unpinned = [], [], [], []
    seen_files = set()
    for glob in GLOBS:
        for path in sorted(root.glob(glob)):
            if not path.is_file() or path in seen_files:
                continue
            # Skip anything vendored or inside a build context we do not own.
            if any(part in {".git", "node_modules", ".venv"} for part in path.parts):
                continue
            seen_files.add(path)
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                m = REFERENCE.match(line)
                if not m:
                    continue
                ref = m.group(1)
                where = f"{path.relative_to(root)}:{lineno}"
                if ref == "scratch":
                    continue
                if "$" in ref:
                    exempt_var.append((where, ref))
                elif "@sha256:" in ref:
                    pinned.append((where, ref))
                elif is_locally_built(ref):
                    exempt_local.append((where, ref))
                else:
                    unpinned.append((where, ref))
    return pinned, exempt_local, exempt_var, unpinned


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=pathlib.Path, default=pathlib.Path("."))
    args = ap.parse_args(argv)

    pinned, exempt_local, exempt_var, unpinned = scan(args.root)

    print(f"Pinned by digest: {len(pinned)}")
    print(f"Exempt, built by this repository and pulled from no registry: {len(exempt_local)}")
    for where, ref in exempt_local:
        print(f"  {where}  {ref}")
    print(f"Exempt, tag resolved at deploy time from scripts/registry.env: {len(exempt_var)}")
    for where, ref in exempt_var:
        print(f"  {where}  {ref}")

    if unpinned:
        print(f"\nFAIL: {len(unpinned)} image reference(s) name a tag and no digest:")
        for where, ref in unpinned:
            print(f"  {where}  {ref}")
        print(
            "\nADR-0007: every image by digest. Resolve the digest of the multi-arch INDEX, not of one\n"
            "platform's manifest -- pinning a single-arch digest breaks the arm64 Compose overrides:\n"
            "\n"
            "  docker buildx imagetools inspect <ref> | head -2\n"
            "\n"
            "then write it as `name:tag@sha256:...`, keeping the tag so a human can still read what it is."
        )
        return 1

    print("\nEvery image reference outside the two stated exemptions names a digest.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
