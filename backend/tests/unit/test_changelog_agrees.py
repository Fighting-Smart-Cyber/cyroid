"""The changelog exists twice, so it must agree with itself and with VERSION.

`frontend/src/lib/changelog.ts` feeds the in-app "What's new" modal.
`CHANGELOG.md` is what someone reads in the repository. Two copies of the same
list is exactly the shape that drifts: the likely failure is not that both are
wrong, it is that a release is added to one and forgotten in the other, and the
modal then either announces nothing or announces a version nobody documented.

Only the SET OF VERSIONS is compared. The two are written for different readers
and their prose is deliberately different -- the modal describes what changed
for the person using PROVING GROUND, the markdown says which files moved -- so
comparing text would force them to be the same document, which would make the
in-app notes worse.
"""

import pathlib
import re

import pytest

# Both files live at the repo root, which is not mounted into the api container
# (only backend/ is), so these run on the host and in CI.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
CHANGELOG_TS = REPO_ROOT / "frontend" / "src" / "lib" / "changelog.ts"
CHANGELOG_MD = REPO_ROOT / "CHANGELOG.md"
VERSION_FILE = REPO_ROOT / "VERSION"

needs_repo = pytest.mark.skipif(not CHANGELOG_TS.exists(), reason="repo root not present")

# The markdown carries every historical release; the modal deliberately does
# not. Only releases at or above this floor are required to appear in both.
# Below it is the unrecorded 0.9-0.40 stretch, which is marked as a gap in the
# markdown rather than invented after the fact.
FLOOR = (0, 41, 0)


def _parse(v: str) -> tuple:
    parts = (v.split(".") + ["0", "0"])[:3]
    return tuple(int(p) if p.isdigit() else 0 for p in parts)


def _ts_versions() -> set:
    text = CHANGELOG_TS.read_text()
    # Only inside the CHANGELOG array, so the doc comment's examples cannot count.
    body = text[text.index("export const CHANGELOG") :]
    return set(re.findall(r"version:\s*'([0-9]+\.[0-9]+\.[0-9]+)'", body))


def _md_versions() -> set:
    return set(re.findall(r"^## \[([0-9]+\.[0-9]+\.[0-9]+)\]", CHANGELOG_MD.read_text(), re.M))


@needs_repo
class TestTheTwoListsAgree:
    def test_every_modal_release_is_in_the_markdown(self):
        missing = _ts_versions() - _md_versions()
        assert not missing, (
            f"{sorted(missing)} appear in the in-app changelog but not in "
            "CHANGELOG.md. Add them, or the repository record is behind what "
            "the product is telling people."
        )

    def test_every_recent_markdown_release_is_in_the_modal(self):
        missing = {v for v in _md_versions() - _ts_versions() if _parse(v) >= FLOOR}
        assert not missing, (
            f"{sorted(missing)} are documented in CHANGELOG.md but will never be "
            "shown in the app. A release nobody is told about is the failure this "
            "modal exists to prevent."
        )


@needs_repo
class TestItDescribesTheVersionThatShipped:
    def test_the_current_version_has_an_entry(self):
        current = VERSION_FILE.read_text().strip()
        assert current in _ts_versions(), (
            f"VERSION is {current} but the in-app changelog has no entry for it, "
            "so anyone updating to it is shown the previous release's notes or "
            "nothing at all. Add the entry with the version bump."
        )

    def test_the_newest_entry_is_first(self):
        """releasesSince() and the modal both assume CHANGELOG[0] is latest."""
        text = CHANGELOG_TS.read_text()
        body = text[text.index("export const CHANGELOG") :]
        order = re.findall(r"version:\s*'([0-9]+\.[0-9]+\.[0-9]+)'", body)
        assert order == sorted(order, key=_parse, reverse=True), (
            f"Entries are not newest-first: {order}. The modal slices from the "
            "front to show the latest release."
        )


@needs_repo
class TestTheGapIsDeclaredRatherThanHidden:
    def test_the_unrecorded_stretch_is_marked(self):
        """Two hundred tags between 0.8.1 and 0.41.0 have no entries. Saying so
        is honest; silently resuming at 0.41.0 reads as though nothing shipped."""
        md = CHANGELOG_MD.read_text()
        assert "Gap:" in md and "0.40" in md, (
            "The unrecorded 0.9-0.40 stretch is no longer declared, so the file "
            "implies it is a complete record when it is not."
        )
