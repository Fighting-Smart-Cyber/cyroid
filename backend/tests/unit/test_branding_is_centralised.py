"""The product names itself in one place.

The engine is one product and a distribution is another (ADR-0011): the engine
ships the mechanism, a distribution ships the theme. Before `MIG-8` step 1 the
product name was 28 literals across ten frontend files, so "what does this
distribution call itself" had no single answer, and re-theming meant a
find-and-replace over the UI with no way to tell whether it had been complete.

This guards the property that makes either distribution strategy possible --
a per-client build or the runtime branding endpoint the ADR recommends. It
does not decide between them.

An install still managed to answer to three names at once: the literal in
`index.html`'s <title>, the name the shell renders, and whatever
GET /api/v1/branding says. The first version of this file did not catch it,
because it asked whether the string "PROVING GROUND" appeared anywhere in
branding.ts -- which a comment satisfied. It now reads the rendered default out
of the object, and holds the HTML shell and the tab title to it.

Only RENDERED names are checked. Three things are deliberately exempt:

  - `changelog.ts` -- release notes are a historical record. "PROVING GROUND"
    in a note about what shipped in 0.42.0 is a statement about that release,
    not a label to re-theme.
  - `branding.test.ts`, for the engine's own default only -- asserting that the
    default is the engine's name is the whole point of that test.
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
INDEX_HTML = REPO_ROOT / "frontend" / "index.html"
BRANDING = FRONTEND / "lib" / "branding.ts"
LAYOUT = FRONTEND / "components" / "layout" / "Layout.tsx"

EXEMPT = {"branding.ts", "changelog.ts"}
EXEMPT_FOR_DEFAULT_NAME = EXEMPT | {"branding.test.ts"}

needs_frontend = pytest.mark.skipif(not FRONTEND.exists(), reason="frontend not present")


def _strip_comments(source: str) -> str:
    """Drop /* ... */ (which also covers JSX {/* ... */}) and // to end of line.

    A `//` inside a string literal -- a URL -- would cut the rest of that line
    early. That can only make this check MISS something, never invent one, so
    the guard stays conservative rather than clever.
    """
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?<!:)//[^\n]*", "", source)


def _strip_html_comments(source: str) -> str:
    return re.sub(r"<!--.*?-->", "", source, flags=re.S)


def _default_product_name(branding_source: str) -> str:
    """The name the engine renders when the branding endpoint says nothing.

    Read out of the object rather than looked for anywhere in the file: the
    check this replaced was satisfied by the word appearing in a docstring,
    which is how branding.ts could claim one name while rendering another.
    """
    stripped = _strip_comments(branding_source)
    match = re.search(r"productName:\s*['\"]([^'\"]+)['\"]", stripped)
    return match.group(1) if match else ""


def _html_title(html_source: str) -> str:
    match = re.search(r"<title>(.*?)</title>", _strip_html_comments(html_source), flags=re.S)
    return match.group(1).strip() if match else ""


def _offenders(needle: str, exempt: set) -> list:
    found = []
    for path in sorted(FRONTEND.rglob("*.ts")) + sorted(FRONTEND.rglob("*.tsx")):
        if path.name in exempt:
            continue
        code = _strip_comments(path.read_text())
        for i, line in enumerate(code.splitlines(), start=1):
            if needle in line:
                found.append(f"{path.relative_to(REPO_ROOT)}:{i}: {line.strip()[:90]}")
    return found


@needs_frontend
class TestTheProductNamesItselfOnce:
    def test_no_rendered_product_name_outside_the_branding_module(self):
        offenders = _offenders("PROVING GROUND", EXEMPT)
        assert not offenders, (
            "The product name is written directly into the UI here, so a "
            "distribution cannot re-theme it from one place:\n  " + "\n  ".join(offenders)
        )

    def test_the_branding_module_exists_and_carries_the_name(self):
        assert BRANDING.exists(), "frontend/src/lib/branding.ts is gone."
        assert _default_product_name(BRANDING.read_text()), (
            "branding.ts no longer carries a rendered product name, so every "
            "screen that renders BRANDING.productName renders nothing."
        )

    def test_the_default_name_is_not_written_into_the_ui_either(self):
        """The engine's own name is as much a theme as a distribution's.

        Catching only the distribution's name let the same mistake back in
        under the other name: a literal in a heading is not re-themeable
        whichever product it happens to spell.
        """
        default = _default_product_name(BRANDING.read_text())
        assert default, "no default name to look for; see the test above."
        offenders = _offenders(default, EXEMPT_FOR_DEFAULT_NAME)
        assert not offenders, (
            "The engine's default name is written directly into the UI here "
            "instead of coming from BRANDING.productName:\n  " + "\n  ".join(offenders)
        )

    def test_a_page_title_helper_exists(self):
        """document.title was built by hand in four places with two different
        shapes; one helper is what keeps tab titles consistent."""
        assert "def pageTitle" in BRANDING.read_text() or "function pageTitle" in (
            BRANDING.read_text()
        )


@needs_frontend
class TestTheTabAgreesWithTheShell:
    """The browser tab is a second surface that can name the product, and it is
    the one nobody looks at. It named a third product for eleven releases."""

    def test_the_html_shell_title_matches_the_branding_default(self):
        assert INDEX_HTML.exists(), "frontend/index.html is gone."
        title = _html_title(INDEX_HTML.read_text())
        default = _default_product_name(BRANDING.read_text())
        assert title == default, (
            f"index.html's <title> is {title!r} but the shell renders {default!r}. "
            "The title shows until the app mounts, so the two have to agree; "
            "re-theming happens through GET /api/v1/branding, not by editing "
            "this file."
        )

    def test_the_shell_sets_the_tab_title_from_the_branding_helper(self):
        """Without this the HTML literal is the answer for the whole session,
        and no amount of branding configuration changes the tab."""
        assert LAYOUT.exists(), "the application shell moved; update this path."
        code = _strip_comments(LAYOUT.read_text())
        assert re.search(r"document\.title\s*=\s*pageTitle\(", code), (
            "The shell never sets document.title, so every page in the "
            "application shows whatever index.html happens to say."
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

    def test_the_default_name_is_read_from_the_object_and_not_the_prose(self):
        """This is the exact shape that used to pass while being wrong."""
        source = """
        /** Mentions ACME RANGE in the docs and nowhere else. */
        export const BRANDING: Branding = {
          productName: 'ENGINE',
          tagline: 'Cyber Range Orchestrator',
        }
        """
        assert _default_product_name(source) == "ENGINE"
        assert _default_product_name("/** productName: 'ACME RANGE' */") == ""

    def test_a_tab_title_that_disagrees_is_detected(self):
        assert _html_title("<title>ACME RANGE - Something</title>") == "ACME RANGE - Something"
        assert _html_title("<!-- <title>OLD</title> --><title>NEW</title>") == "NEW"
        assert _html_title("<head></head>") == ""
