"""Repo containers must see the repo at the host's own path.

`docker compose` inside such a container talks to the HOST's daemon through
the mounted socket, but resolves the compose files' relative bind mounts
(./config/registry-config.yml, ./backend, ./data/...) against the compose
file's own directory -- i.e. against that container's filesystem. The two only
agree when the repo sits at the same absolute path on both sides.

Mounted at /repo they did not agree, and the first real in-UI update took the
platform down:

    error mounting "/repo/config/registry-config.yml" ... not a directory

The host had no /repo, so Docker created every missing path as a DIRECTORY --
/repo/VERSION and /repo/traefik.yml became directories too -- and api, worker,
traefik and registry all failed to come back. The frontend stayed up in front
of nothing.

The spec now has ONE definition, shared by the update and the update check,
because a second copy is how this detail gets it wrong again.
"""

import ast
import inspect

from proving_ground.api import admin as admin_api


def _code_of(fn) -> str:
    """Source with comment lines dropped.

    The fix is explained in prose that quotes the very paths that broke, and
    that prose is worth keeping -- but it must not satisfy or defeat a check
    about the code.
    """
    src = inspect.getsource(fn)
    return "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))


class TestTheSharedSpecMountsTheRepoAtItsHostPath:
    def test_the_bind_target_is_the_host_path_not_a_fixed_mount_point(self):
        code = _code_of(admin_api._repo_container_spec)
        assert 'repo_root: {"bind": repo_root' in code, (
            "The repo is mounted somewhere other than its host path, so every "
            "relative bind mount in the compose files resolves to a path the "
            "host daemon does not have and silently creates as a directory."
        )

    def test_the_working_directory_is_the_host_path(self):
        assert '"working_dir": repo_root' in _code_of(admin_api._repo_container_spec)

    def test_no_fixed_repo_mount_point_survives_in_the_code(self):
        assert "/repo" not in _code_of(admin_api._repo_container_spec), (
            'A hardcoded "/repo" remains; the container and the host must agree '
            "on the repository path."
        )

    def test_an_unresolvable_repo_path_refuses_rather_than_guessing(self):
        code = _code_of(admin_api._repo_container_spec)
        assert "if not repo_root:" in code and "HTTPException" in code


class TestBothCallersUseIt:
    """One definition, or the detail drifts and breaks again."""

    def test_the_update_and_the_check_both_use_the_shared_spec(self):
        for fn in (admin_api.start_platform_update, admin_api.check_for_platform_update):
            assert "_repo_container_spec" in _code_of(fn), f"{fn.__name__} builds its own spec."

    def test_neither_caller_builds_its_own_mount(self):
        for fn in (admin_api.start_platform_update, admin_api.check_for_platform_update):
            code = _code_of(fn)
            assert '"bind"' not in code, (
                f"{fn.__name__} constructs a bind mount of its own rather than "
                "using the shared spec."
            )

    def test_neither_caller_hardcodes_a_repo_path(self):
        for fn in (admin_api.start_platform_update, admin_api.check_for_platform_update):
            assert "/repo" not in _code_of(fn)


class TestTheGitPreambleIsSharedToo:
    def test_it_carries_the_ownership_exception_and_the_credential_helper(self):
        assert 'safe.directory "$PG_REPO_ROOT"' in admin_api._GIT_SETUP
        assert 'cd "$PG_REPO_ROOT"' in admin_api._GIT_SETUP
        assert "credential.helper" in admin_api._GIT_SETUP

    def test_the_path_reaches_the_script_through_the_environment(self):
        assert '"PG_REPO_ROOT": repo_root' in _code_of(admin_api._repo_container_spec)

    def test_both_scripts_compose_it_rather_than_repeating_it(self):
        for fn in (admin_api.start_platform_update, admin_api.check_for_platform_update):
            assert "_GIT_SETUP" in _code_of(fn)

    def test_the_token_is_never_interpolated_into_the_script(self):
        """A token in the script text lands in the container's Config.Cmd."""
        assert "{git_token}" not in admin_api._GIT_SETUP
        assert "$GIT_ASKPASS_TOKEN" in admin_api._GIT_SETUP


class TestTheHostPathComesFromTheApiContainersOwnMount:
    def test_it_reads_the_app_bind_mounts_source(self):
        """The host's view, not this container's -- /app means nothing to a
        sibling container."""
        src = inspect.getsource(admin_api._host_repo_root)
        assert '"/app"' in src and "Source" in src


def test_the_update_endpoints_still_exist():
    """The spec refactor edits a large function; this catches losing one."""
    tree = ast.parse(inspect.getsource(admin_api))
    names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    for required in (
        "start_platform_update",
        "check_for_platform_update",
        "get_platform_update_status",
        "set_update_credential",
        "get_update_credential",
        "delete_update_credential",
        "_host_repo_root",
        "_find_update_job",
    ):
        assert required in names, f"{required} went missing"
