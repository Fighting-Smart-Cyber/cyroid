"""The update check must distinguish "nothing to pull" from "I could not look".

Reporting a failed check as update_available=false would render as "Up to
date" and disable the update button -- going quiet at exactly the moment
something is wrong with the credential or the remote. `checked` carries that
distinction, and the button only disables on checked=true.

The check runs in a short-lived container for the same reason the update does:
the API container has only the backend bind-mounted at /app, so it cannot see
the repository's .git at all.
"""

import inspect

from proving_ground.api import admin as admin_api


class TestNotKnowingIsNotUpToDate:
    def test_the_response_carries_a_checked_flag(self):
        fields = admin_api.UpdateCheckResponse.model_fields
        assert "checked" in fields
        assert "update_available" in fields

    def test_update_available_defaults_to_false_but_checked_does_not_default(self):
        """checked has no default, so no code path can forget to state it."""
        fields = admin_api.UpdateCheckResponse.model_fields
        assert fields["checked"].is_required(), (
            "checked has a default, so a response built without it silently "
            "claims the remote was consulted."
        )

    def test_every_failure_path_reports_checked_false(self):
        """Not one of them may report update_available without a real answer."""
        src = inspect.getsource(admin_api.check_for_platform_update)
        # Each early return is a failure path; all must set checked=False.
        returns = [ln.strip() for ln in src.splitlines() if "UpdateCheckResponse(" in ln]
        assert len(returns) >= 4, f"expected several return paths, found {len(returns)}"
        assert src.count("checked=False") >= 4, (
            "A failure path omits checked=False and would be rendered as " "'up to date'."
        )

    def test_the_success_path_derives_availability_from_a_measurement(self):
        """Availability is computed, never assumed.

        Which measurement depends on the channel: commits behind the branch tip
        for "branch", a strictly newer release tag for "release". Both must be
        present -- a channel that fell through to a constant would report
        "up to date" without having measured anything.
        """
        src = inspect.getsource(admin_api.check_for_platform_update)
        assert "available = behind > 0" in src, "branch channel lost its measurement"
        assert (
            "is_newer(latest_tag, current_version)" in src
        ), "release channel lost its measurement"


class TestItRunsAgainstTheRepoLikeTheUpdateDoes:
    def test_it_uses_the_shared_container_spec(self):
        """The mount path is what took the platform down; one definition only."""
        src = inspect.getsource(admin_api.check_for_platform_update)
        assert "_repo_container_spec(docker, db)" in src
        assert "**spec" in src

    def test_it_does_not_wear_the_update_label(self):
        """Sharing UPDATE_LABEL would make the status endpoint report a check
        as though it were an update."""
        src = inspect.getsource(admin_api.check_for_platform_update)
        assert "CHECK_LABEL" in src and "UPDATE_LABEL" not in src

    def test_it_cannot_hang_the_request(self):
        src = inspect.getsource(admin_api.check_for_platform_update)
        assert "timeout=CHECK_TIMEOUT_SECONDS" in src

    def test_it_always_removes_its_container(self):
        src = inspect.getsource(admin_api.check_for_platform_update)
        assert "finally:" in src and "container.remove(force=True)" in src

    def test_it_requires_an_admin(self):
        assert "AdminUser" in inspect.getsource(admin_api.check_for_platform_update)


class TestATagDisagreementCannotBreakTheCheck:
    """`git fetch --prune --tags` force-updates every tag and fails the whole
    fetch with "would clobber existing tag" if any local tag disagrees with the
    remote's. This repository is permanently in that state -- a run of tags
    could never be pushed because of the commit-author email restriction -- so
    the very first real use of this endpoint aborted under `set -eu`, and with
    stderr discarded it could only report "The check produced no output."

    Whether the host is behind is a question about commits. A tag disagreement
    must not be able to answer it "I do not know".
    """

    def test_the_fetch_that_must_succeed_does_not_force_tags(self):
        src = inspect.getsource(admin_api.check_for_platform_update)
        assert "--prune --tags" not in src, (
            "The fetch force-updates tags again, so any tag that disagrees with "
            "the remote fails the whole check."
        )
        assert '"git fetch --prune origin\\n"' in src

    def test_a_tag_disagreement_cannot_break_the_check(self):
        """The guarantee is now structural rather than a swallowed error.

        This used to assert `|| true` on a `git fetch --tags`, making a
        clobbering fetch non-fatal. Tags are now read with `git ls-remote`,
        which asks the remote what it has and writes no local refs at all, so
        there is no local tag state left to disagree with anything. Asserting
        the mechanism is absent is stronger than asserting its failure was
        tolerated.
        """
        src = inspect.getsource(admin_api.check_for_platform_update)
        assert "ls-remote" in src, "tags are being fetched again rather than listed"
        code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
        assert "fetch --tags" not in code, (
            "a clobbering tag fetch is back in the check; that is what broke "
            "this endpoint the first time"
        )

    def test_the_commit_count_fetch_keeps_its_errors(self):
        """Discarding stderr is what made the first failure undiagnosable."""
        src = inspect.getsource(admin_api.check_for_platform_update)
        line = next(ln for ln in src.splitlines() if "git fetch --prune origin" in ln)
        assert "2>&1" not in line and "/dev/null" not in line, (
            f"{line.strip()} discards its errors, so a failure arrives at the "
            "UI with nothing to say."
        )


class TestAFailedCheckLeavesATrace:
    def test_the_non_exception_failure_paths_log(self):
        """These returned silently, so the API log was empty while the UI said
        the check had failed -- diagnosing it meant reproducing it by hand."""
        src = inspect.getsource(admin_api.check_for_platform_update)
        assert (
            src.count("logger.warning") >= 3
        ), "A failure path returns checked=False without logging anything."
