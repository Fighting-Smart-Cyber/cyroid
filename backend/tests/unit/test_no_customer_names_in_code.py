"""Shipped code must not name a customer.

The engine is public and capability-agnostic: it trains people on whatever
capability a distribution supplies. Naming one customer in a UI placeholder
implies otherwise to every reader of the public repository, and the strings
are no more useful for it -- someone configuring an Organization field does
not need a specific agency to know what belongs there.

The domain may be named in README and the roadmap, which describe what this
was built for. That is a different claim from shipped code assuming it.

Found by dry-running the public snapshot filter, which is the only place the
distinction becomes visible.
"""

import pathlib
import re

# Directories that ship in the public snapshot. scripts/roadmap and
# public-exclude.txt legitimately name the customer and are filtered out
# before publication, so they are not scanned here.
SHIPPED = [
    pathlib.Path(__file__).resolve().parents[2] / "proving_ground",
    pathlib.Path(__file__).resolve().parents[3] / "frontend" / "src",
]

# Customers, programmes and agencies. Matched case-insensitively on word
# boundaries so "logic2" or a UUID fragment does not trip it.
FORBIDDEN = re.compile(
    r"\b(LOGC2|DEFCON[\s-]?AI|defconai|USMC|CYBERCOM|Fleet Marine Force)\b",
    re.IGNORECASE,
)

SUFFIXES = {".py", ".ts", ".tsx", ".js", ".jsx", ".html", ".css"}


def test_shipped_code_names_no_customer():
    hits = []
    scanned = 0
    for root in SHIPPED:
        if not root.exists():
            # The frontend is not mounted into the api container, so a local
            # container run scans the backend only. CI checks out the whole
            # repository and scans both; the assertion below makes a
            # scanned-nothing run fail rather than pass quietly.
            continue
        for path in root.rglob("*"):
            if path.suffix not in SUFFIXES or "node_modules" in str(path):
                continue
            scanned += 1
            for i, line in enumerate(
                path.read_text(encoding="utf-8", errors="ignore").splitlines(), 1
            ):
                m = FORBIDDEN.search(line)
                if m:
                    hits.append(f"{path}:{i}: {m.group(0)!r} in {line.strip()[:80]}")
    assert scanned > 0, (
        "The scan found no files at all, so this guard proves nothing. Check "
        f"the roots resolve: {[str(r) for r in SHIPPED]}"
    )
    assert not hits, (
        "Shipped code names a specific customer or programme. The engine is "
        "public and capability-agnostic; use a generic example instead:\n  " + "\n  ".join(hits)
    )
