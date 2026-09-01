"""The product names itself in one place.

The engine is one product and a distribution is another (ADR-0011): the engine
ships the mechanism, a distribution ships the theme. Before `MIG-8` step 1 the
product name was 28 literals across ten frontend files, so "what does this
distribution call itself" had no single answer, and re-theming meant a
find-and-replace over the UI with no way to tell whether it had been complete.

This guards the property that makes either distribution strategy possible --
a per-client build or the runtime branding endpoint the ADR recommends. It
does not decide between them.

Only RENDERED names are checked. Two things are deliberately exempt:

  - `changelog.ts` -- release notes are a historical record. "PROVING GROUND"
    in a note about what shipped in 0.42.0 is a statement about that release,
    not a label to re-theme.
  - comments -- they explain the code to whoever is reading it, and a comment
    saying "PROVING GROUND platform infrastructure" is not something a user
    ever sees.
"""

import pathlib
import re

import pytest

# frontend/ is not mounted into the api container (only backend/ is), so this
# runs on the host and in CI.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
FRONTEND = REPO_ROOT / "frontend" / "src"
BRANDING = FRONTEND / "lib" / "branding.ts"

EXEMPT = {"branding.ts", "changelog.ts"}

needs_frontend = pytest.mark.skipif(not FRONTEND.exists(), reason="frontend not present")


def _strip_comments(source: str) -> str:
    """Drop /* ... */ (which also covers JSX {/* ... */}) and // to end of line.

    A `//` inside a string literal -- a URL -- would cut the rest of that line
    early. That can only make this check MISS something, never invent one, so
    the guard stays conservative rather than clever.
    """
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?<!:)//[^\n]*", "", source)


def _offenders() -> list:
    found = []
    for path in sorted(FRONTEND.rglob("*.ts")) + sorted(FRONTEND.rglob("*.tsx")):
        if path.name in EXEMPT:
            continue
        code = _strip_comments(path.read_text())
        for i, line in enumerate(code.splitlines(), start=1):
            if "PROVING GROUND" in line:
                found.append(f"{path.relative_to(REPO_ROOT)}:{i}: {line.strip()[:90]}")
    return found


@needs_frontend
class TestTheProductNamesItselfOnce:
    def test_no_rendered_product_name_outside_the_branding_module(self):
        offenders = _offenders()
        assert not offenders, (
            "The product name is written directly into the UI here, so a "
            "distribution cannot re-theme it from one place:\n  " + "\n  ".join(offenders)
        )

    def test_the_branding_module_exists_and_carries_the_name(self):
        assert BRANDING.exists(), "frontend/src/lib/branding.ts is gone."
        text = BRANDING.read_text()
        assert "productName" in text
        assert "PROVING GROUND" in text, (
            "branding.ts no longer carries a product name, so every screen that "
            "renders BRANDING.productName renders nothing."
        )

    def test_a_page_title_helper_exists(self):
        """document.title was built by hand in four places with two different
        shapes; one helper is what keeps tab titles consistent."""
        assert "def pageTitle" in BRANDING.read_text() or "function pageTitle" in (
            BRANDING.read_text()
        )


@needs_frontend
class TestTheGuardWouldActuallyCatchSomething:
    """A guard that cannot fail is worse than none: it reads as a passing check
    while enforcing nothing."""

    def test_a_rendered_name_is_detected(self, tmp_path):
        sample = '<h1 className="x">PROVING GROUND</h1>'
        assert "PROVING GROUND" in _strip_comments(sample)

    def test_a_comment_is_not_detected(self):
        assert "PROVING GROUND" not in _strip_comments("// PROVING GROUND platform bits")
        assert "PROVING GROUND" not in _strip_comments("{/* PROVING GROUND Services */}")
        assert "PROVING GROUND" not in _strip_comments("/** Opening PROVING GROUND. */")
