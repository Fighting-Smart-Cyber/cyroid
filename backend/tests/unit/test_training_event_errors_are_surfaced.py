"""Every lifecycle action on the training-event pages must say what went wrong.

Start, complete, cancel, reactivate, delete, join and the participant edits all
ended at `console.error`, so an instructor who pressed "Start & Deploy Labs"
against a blueprint the substrate refuses observed nothing at all -- no toast,
no banner, no state change -- with a diagnosable 400 sitting in the response.
That is how the substrate breakage in the start path presented for weeks: as a
button that does nothing.

Nothing in the frontend test suite can catch this coming back. vitest runs with
environment 'node' and jsdom is not a dependency, so there is no way to render
these pages and press a button; the pure helpers in src/lib/trainingEvents.ts
are unit tested, but a page that stopped calling them would pass every one of
those tests. This reads the source instead, which is the same thing
test_branding_is_centralised.py and test_no_customer_names_in_code.py do for
their own invariants.

Deliberately scoped to these two files. The same defect exists elsewhere in the
frontend and is being fixed file by file; widening this guard would fail on
pages nobody has reached yet and say nothing useful about the ones that are
done.
"""

import pathlib
import re

import pytest

PAGES = pathlib.Path(__file__).resolve().parents[3] / "frontend" / "src" / "pages"

WATCHED = [PAGES / "TrainingEvents.tsx", PAGES / "TrainingEventDetail.tsx"]

# Anything that puts the failure in front of the user. `setError` and
# `setStartError` render into the page itself; `toast.` is the shared stack.
REPORTS = re.compile(r"\btoast\.(error|warning)\b|\bsetError\(|\bsetStartError\(")

CATCH = re.compile(r"\}\s*catch\s*\(")


def _catch_bodies(source: str):
    """Yield the text of each catch block, found by matching its braces.

    A regex cannot do this: the bodies contain their own braces, template
    literals and JSX.
    """
    for match in CATCH.finditer(source):
        open_brace = source.find("{", match.end())
        assert open_brace != -1, "a catch clause with no block"
        depth = 0
        for i in range(open_brace, len(source)):
            if source[i] == "{":
                depth += 1
            elif source[i] == "}":
                depth -= 1
                if depth == 0:
                    yield source[open_brace : i + 1]
                    break


@pytest.mark.parametrize("path", WATCHED, ids=lambda p: p.name)
def test_no_lifecycle_error_is_swallowed(path: pathlib.Path):
    if not path.exists():
        pytest.skip(
            "The frontend is not mounted into the api container, so a local "
            "container run has nothing to scan. CI checks out the whole "
            "repository."
        )
    source = path.read_text(encoding="utf-8")

    bodies = list(_catch_bodies(source))
    assert bodies, f"{path.name}: no catch blocks found, so this guard proves nothing"

    silent = [body for body in bodies if not REPORTS.search(body)]
    assert not silent, (
        f"{path.name}: {len(silent)} catch block(s) report nothing to the user. "
        "An instructor pressing a lifecycle button has to be told why it was "
        "refused; surface the server's detail with apiErrorDetail and "
        "toast.error.\n  " + "\n  ".join(b.strip()[:160] for b in silent)
    )


@pytest.mark.parametrize("path", WATCHED, ids=lambda p: p.name)
def test_no_failure_is_logged_to_the_console_or_a_native_dialog(path: pathlib.Path):
    """`console.error` is invisible to the user and `alert` cannot carry a
    server's multi-line schema refusal, which is how a 422 reached the screen
    as "[object Object]"."""
    if not path.exists():
        pytest.skip("frontend not present in this checkout")
    source = path.read_text(encoding="utf-8")

    hits = [
        f"{i}: {line.strip()[:120]}"
        for i, line in enumerate(source.splitlines(), 1)
        if "console.error(" in line or re.search(r"(?<![\w.])alert\(", line)
    ]
    assert not hits, (
        f"{path.name}: a failure goes to the console or a native dialog instead "
        "of the page:\n  " + "\n  ".join(hits)
    )
