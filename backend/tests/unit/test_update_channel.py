"""The in-UI update follows either a release tag or a branch tip.

Before this, `update_available` was `behind > 0` against the current branch and
tags were decorative: fetched best-effort, shown in the response, and consulted
by nothing. A host therefore offered an update for every merged commit and kept
reporting whatever VERSION happened to say, so the version number stopped
describing the running code -- the exact thing semver exists to prevent.

Two channels now:

    release   highest vX.Y.Z tag on the remote (default)
    branch    tip of the branch the host is on (dev hosts)
"""

import inspect
import pathlib

import pytest

from proving_ground.api import admin
from proving_ground.utils.versions import is_newer, latest_release, parse_version


class TestParseVersion:
    @pytest.mark.parametrize(
        "text,expected",
        [
            ("0.43.0", (0, 43, 0)),
            ("v0.43.0", (0, 43, 0)),
            ("  v1.2.3  ", (1, 2, 3)),
            ("10.20.30", (10, 20, 30)),
        ],
    )
    def test_accepts_x_y_z_with_or_without_v(self, text, expected):
        assert parse_version(text) == expected

    @pytest.mark.parametrize(
        "text",
        ["dev", "", None, "v1.0", "1.0.0-rc1", "v1.0.0-rc1", "latest", "v1.2.3.4", "va.b.c"],
    )
    def test_rejects_anything_else(self, text):
        """tag-release.sh refuses non-X.Y.Z and the CI release rule matches only
        ^v[0-9]+\\.[0-9]+\\.[0-9]+$, so a v1.0.0-rc1 runs no release jobs. It must
        not be offered as an update either."""
        assert parse_version(text) is None


class TestLatestRelease:
    def test_orders_numerically_not_lexically(self):
        # The classic failure: "v0.9.0" > "v0.10.0" as strings, which would pin
        # a host to an older release permanently.
        assert latest_release(["v0.9.0", "v0.10.0"]) == "v0.10.0"
        assert latest_release(["v0.43.0", "v0.43.10", "v0.43.2"]) == "v0.43.10"

    def test_ignores_tags_that_are_not_releases(self):
        assert latest_release(["v0.1.0", "v2.0.0-rc1", "nightly", "v0.2.0"]) == "v0.2.0"

    def test_returns_the_tag_as_spelled(self):
        assert latest_release(["v0.43.0"]) == "v0.43.0"

    def test_none_when_there_is_nothing_to_pick(self):
        assert latest_release([]) is None
        assert latest_release(["nightly", "latest", "v1.0.0-rc1"]) is None


class TestIsNewer:
    def test_strictly_greater_only(self):
        assert is_newer("v0.44.0", "0.43.0") is True
        assert is_newer("v0.43.0", "0.43.0") is False
        assert is_newer("v0.42.0", "0.43.0") is False

    def test_a_dev_version_is_never_overtaken(self):
        """A working checkout reports "dev" and already holds code no tag
        describes. Offering it every release would move it backwards."""
        assert is_newer("v9.9.9", "dev") is False

    def test_unparseable_candidate_is_not_newer(self):
        assert is_newer("nightly", "0.43.0") is False
        assert is_newer(None, "0.43.0") is False


class TestChannelSetting:
    def test_release_is_the_default(self, monkeypatch):
        """The safe answer for a host nobody is watching: it will not pull an
        unreleased master on its own.

        Isolated from the ambient environment and from .env deliberately. A
        plain Settings() here reads whatever the host running the suite has
        configured, so on a dev box with UPDATE_CHANNEL=branch this asserted
        the host's setting rather than the code's default.
        """
        from proving_ground.config import Settings

        monkeypatch.delenv("UPDATE_CHANNEL", raising=False)
        assert Settings(_env_file=None).update_channel == "release"

    @pytest.mark.parametrize("value", ["release", "branch", "RELEASE", " branch "])
    def test_accepts_both_channels_case_and_space_insensitively(self, value):
        from proving_ground.config import Settings

        assert Settings(update_channel=value).update_channel == value.strip().lower()

    def test_a_typo_is_refused_not_coerced(self):
        """Silently falling back to a default would change what a host deploys
        without telling anyone."""
        from pydantic import ValidationError

        from proving_ground.config import Settings

        # Specifically a validation failure. A bare Exception would also pass
        # if Settings() blew up for some unrelated reason, which would leave
        # the actual guarantee untested.
        with pytest.raises(ValidationError):
            Settings(update_channel="realease")


def _code_of(fn) -> str:
    """Source with the docstring AND comments removed.

    These assert on what the function does. Both the docstring and the inline
    comments quote the very commands being asserted about -- the comment above
    the ls-remote call explains at length why `git fetch --tags` is not used,
    and a naive substring search finds it there and fails a passing function.
    Only whole comment lines are dropped, so the git commands, which live in
    string literals, survive.
    """
    src = inspect.getsource(fn)
    parts = src.split('"""')
    src = parts[0] + "".join(parts[2:]) if len(parts) >= 3 else src
    return "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))


class TestTheCheckDoesNotFetchTags:
    """`git fetch --tags` force-updates every tag and fails the WHOLE fetch with
    "would clobber existing tag" when any local tag disagrees with the remote's
    -- the standing state of this repository. That is what silently broke this
    endpoint once already. ls-remote writes nothing locally and cannot."""

    def test_the_check_uses_ls_remote(self):
        src = _code_of(admin.check_for_platform_update)
        assert "ls-remote" in src

    def test_the_check_never_fetches_tags(self):
        src = _code_of(admin.check_for_platform_update)
        assert "fetch --tags" not in src

    def test_the_update_never_fetches_all_tags(self):
        src = _code_of(admin.start_platform_update)
        assert "fetch --tags" not in src
        # A single named tag is fine, and must say so explicitly.
        if "fetch origin tag" in src:
            assert "--no-tags" in src


class TestTheReleaseTargetIsNotCallerSupplied:
    def test_the_update_takes_no_ref_argument(self):
        """Accepting a ref from the request would be arbitrary code execution
        on a host of the caller's choosing."""
        params = inspect.signature(admin.start_platform_update).parameters
        assert not {"ref", "tag", "branch", "version"} & set(params)

    def test_the_target_is_revalidated_before_reaching_the_shell(self):
        src = _code_of(admin.start_platform_update)
        assert "_RELEASE_TAG_RE" in src

    @pytest.mark.parametrize(
        "hostile",
        ["v1.0.0; rm -rf /", "v1.0.0 && curl evil", "$(whoami)", "v1.0.0`id`", "../../etc"],
    )
    def test_the_tag_pattern_rejects_shell_metacharacters(self, hostile):
        assert not admin._RELEASE_TAG_RE.match(hostile)

    def test_the_tag_pattern_accepts_a_real_release(self):
        assert admin._RELEASE_TAG_RE.match("v0.43.0")


# docker-compose.yml lives at the repository root, which is NOT mounted into the
# API container (only backend/ is, at /app). So this runs in CI, where the whole
# tree is checked out, and skips when the suite is run inside the container.
_COMPOSE = pathlib.Path(__file__).resolve().parents[3] / "docker-compose.yml"


@pytest.mark.skipif(not _COMPOSE.exists(), reason="repo root not mounted in the API container")
class TestTheChannelReachesTheContainer:
    """A Settings field is not configuration until compose passes it through.

    `.env` is interpolation-only: Docker Compose reads it to expand ${VARS} in
    the compose files and does NOT inject it into containers. A setting that is
    declared in Settings and set in .env but never listed under a service's
    `environment:` silently keeps its default, and the host quietly follows the
    wrong channel while .env claims otherwise. This was hit twice -- once for
    RANGE_DEFAULT_*, once for this very field.
    """

    def test_it_is_passed_to_the_services_that_read_it(self):
        """Count service-level assignments, not occurrences of the string.

        Each wired line reads `UPDATE_CHANNEL: ${UPDATE_CHANNEL:-release}` and
        so contains the name twice; a naive count says 4 for two services and
        would be satisfied by a single service wired twice.
        """
        assignments = [
            ln
            for ln in _COMPOSE.read_text().splitlines()
            if ln.strip().startswith("UPDATE_CHANNEL:")
        ]
        assert len(assignments) == 2, (
            f"expected the api and the worker to receive UPDATE_CHANNEL, found "
            f"{len(assignments)} assignment(s); .env alone does not reach a container"
        )

    def test_the_compose_default_matches_the_settings_default(self):
        """Two defaults that disagree mean the answer depends on which one you
        read, and nobody reads both."""
        from proving_ground.config import Settings

        text = _COMPOSE.read_text()
        assert f"${{UPDATE_CHANNEL:-{Settings().update_channel}}}" in text
