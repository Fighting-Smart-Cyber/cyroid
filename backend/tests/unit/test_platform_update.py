"""Updating the platform from the UI restarts the thing serving the request.

That is the whole difficulty. The API cannot run the update itself: it would be
killed partway through, and the caller could not distinguish a successful
update from a crash. So the work runs in a container outside the compose
project, which `docker compose up` does not touch and which therefore survives
the restart it causes.

These cover the parts that are easy to get wrong and expensive to discover in
production.
"""

import inspect

from proving_ground.api import admin as admin_api


def _update_path_source() -> str:
    """The update's whole implementation, not just the endpoint.

    The endpoint delegates the container spec and the git preamble to pieces it
    shares with the update check, so that one definition covers both. These
    assertions are about what an update does, never about which function
    currently holds a given line.
    """
    return "\n".join(
        [
            inspect.getsource(admin_api.start_platform_update),
            inspect.getsource(admin_api._repo_container_spec),
            admin_api._GIT_SETUP,
        ]
    )


class TestItSurvivesTheRestartItCauses:
    def test_the_update_runs_outside_the_compose_project(self):
        src = _update_path_source()
        assert "containers.run(" in src, (
            "The update is not launched as a separate container, so it will be "
            "killed by the restart it triggers."
        )
        assert "detach=True" in src, "A blocking run dies with the API process."

    def test_status_is_readable_after_the_restart(self):
        """The job is labelled so a freshly started API can find it again."""
        src = inspect.getsource(admin_api.get_platform_update_status)
        assert "_find_update_job" in src
        assert "UPDATE_LABEL" in inspect.getsource(admin_api._find_update_job), (
            "The job is found by name or id rather than a label, so a restarted "
            "API cannot locate the run that is still in progress."
        )


class TestGuards:
    def test_admin_only(self):
        for fn in (admin_api.start_platform_update, admin_api.get_platform_update_status):
            assert "AdminUser" in inspect.getsource(fn), (
                f"{fn.__name__} does not require an admin; updating the platform "
                f"restarts every service and pulls code onto the host."
            )

    def test_it_refuses_while_a_range_is_deploying(self):
        """The deploy worker restarts too. A deploy caught mid-flight leaves a
        half-built range whose row still claims it is deploying."""
        src = _update_path_source()
        assert (
            "RangeStatus.DEPLOYING" in src
        ), "Nothing stops an update from interrupting a deploy in progress."
        assert "HTTP_409_CONFLICT" in src

    def test_it_refuses_a_concurrent_update(self):
        src = _update_path_source()
        assert 'status == "running"' in src, (
            "Two updates can run at once, racing each other over the same " "working tree."
        )


class TestNoArbitraryRef:
    def test_the_endpoint_takes_no_branch_or_ref(self):
        """Updating runs whatever the pull brings.

        Accepting a ref from the request would let an admin execute arbitrary
        code on the host by pointing it at any branch. Changing branches stays
        a deliberate act at a shell.
        """
        sig = inspect.signature(admin_api.start_platform_update)
        params = set(sig.parameters) - {"admin_user", "db"}
        assert not params, (
            f"start_platform_update accepts {params}; a caller-supplied ref "
            f"turns this into arbitrary code execution on the host."
        )

    def test_the_pull_is_fast_forward_only(self):
        src = _update_path_source()
        assert "--ff-only" in src, (
            "A merge commit could be created on the host, or a divergent "
            "history silently merged."
        )


class TestHostPathDiscovery:
    def test_the_repo_path_comes_from_the_bind_mount(self):
        """Paths inside the API container mean nothing to a sibling container;
        it needs the host's view of the repository."""
        src = inspect.getsource(admin_api._host_repo_root)
        assert '"/app"' in src and "Mounts" in src

    def test_an_undiscoverable_path_fails_loudly(self):
        src = _update_path_source()
        assert "pg-update.sh" in src, (
            "When the path cannot be found the error should point at the shell "
            "script that still works, rather than just failing."
        )


class TestItRunsAsTheRepositoryOwner:
    """The first real run failed on this, before it read even the branch name:

        fatal: detected dubious ownership in repository at '/repo'

    The image runs as root, the repository is owned by uid 1000, and git
    refuses to operate across that gap. Root would also write files into the
    working tree that its owner cannot afterwards remove.
    """

    def test_the_container_runs_as_the_owning_uid(self):
        src = _update_path_source()
        assert '"user": run_as' in src, (
            "The update container runs as root, so git refuses the repository "
            "and any file it writes is left root-owned."
        )
        assert 'os.stat("/app")' in src, (
            "The uid is hardcoded or guessed rather than read from the mount; "
            "bind mounts preserve numeric ids, so /app is the source of truth."
        )

    def test_it_keeps_access_to_the_docker_socket(self):
        """Dropping from root loses socket access unless the group comes too."""
        src = _update_path_source()
        assert "group_add" in src and "docker.sock" in src, (
            "Running as a non-root user without the socket's group cannot talk "
            "to Docker, so the redeploy half of the update fails."
        )

    def test_safe_directory_is_set_as_a_fallback(self):
        """If the ids cannot be resolved the run falls back to root, and this
        is what keeps git working in that case."""
        src = _update_path_source()
        assert "safe.directory" in src

    def test_home_is_writable(self):
        """`git config --global` needs a writable HOME, and the owning uid has
        no home directory inside this image."""
        src = _update_path_source()
        assert '"HOME"' in src, (
            "git config --global will fail with an unwritable HOME, taking the "
            "safe.directory fallback down with it."
        )

    def test_the_branch_is_resolved_once(self):
        """The failing run printed '==> updating ' with an empty branch: the
        name was resolved by a subshell that had already failed."""
        src = _update_path_source()
        assert 'branch="$(git rev-parse --abbrev-ref HEAD)"' in src, (
            "The branch is still resolved inline per use, so a failure shows up "
            "as an empty string rather than an error."
        )
