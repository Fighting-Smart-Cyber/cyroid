"""The pre-deploy disk check must actually measure a filesystem.

It asked the Docker daemon for DockerRootDir and stat'ed that. DockerRootDir
is a path on the HOST; stat'ing it from inside the API/worker container raises
FileNotFoundError whatever it is set to -- the default "/var/lib/docker" fails
exactly as a relocated "/data/docker" does. Verified on two hosts.

So the except branch ran on every deploy, logged "Could not check disk space",
and returned valid=True. A check that always abstains is worse than no check:
it occupies the place where a guard should be and reports nothing, so a range
that cannot fit is admitted with an approving message.

The estimate is about guest disk images, and those are written to
vm_storage_dir, which is bind-mounted into the container. That is what gets
measured now.
"""

import inspect

from proving_ground.services import deployment_validator as dv


def _code_of(fn) -> str:
    src = inspect.getsource(fn)
    parts = src.split('"""')
    src = parts[0] + "".join(parts[2:]) if len(parts) >= 3 else src
    return "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))


def _disk_check_source() -> str:
    """Source of the disk-space check.

    Selected by name and required to be callable: a bare "disk" in name.lower()
    also matches the class constants DISK_BUFFER_PERCENT and MIN_DISK_GB, and
    since dir() sorts uppercase first that picked a float and blew up in
    inspect.getsource rather than testing anything.
    """
    fn = getattr(dv.DeploymentValidator, "_validate_disk_space", None)
    assert callable(fn), "no _validate_disk_space method; this test proves nothing"
    return _code_of(fn)


class TestItMeasuresAPathTheProcessCanSee:
    def test_it_does_not_stat_the_daemons_root_dir(self):
        """DockerRootDir is a host path. From in here it is unstattable."""
        assert "DockerRootDir" not in _disk_check_source()

    def test_it_measures_where_guest_disks_go(self):
        src = _disk_check_source()
        assert "vm_storage_dir" in src

    def test_it_has_a_fallback_that_always_exists(self):
        """Falling back to "/" gives a coarse answer; abstaining gives none."""
        assert '"/"' in _disk_check_source()

    def test_the_chosen_path_is_checked_for_existence_first(self):
        src = _disk_check_source()
        assert "isdir" in src


class TestTheConfiguredPathsAreRealInThisProcess:
    def test_vm_storage_dir_is_an_absolute_path(self):
        from proving_ground.config import get_settings

        p = get_settings().vm_storage_dir
        assert p and p.startswith("/"), p

    def test_the_fallback_chain_always_resolves(self):
        """Whatever is or is not mounted, the chain must yield a stattable path
        -- otherwise the check silently returns to abstaining."""
        import os
        import shutil

        from proving_ground.config import get_settings

        s = get_settings()
        chosen = next(
            (c for c in [s.vm_storage_dir, s.template_storage_dir, "/"] if c and os.path.isdir(c)),
            "/",
        )
        usage = shutil.disk_usage(chosen)  # must not raise
        assert usage.total > 0
