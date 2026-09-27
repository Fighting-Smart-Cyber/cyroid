"""A VM installing from an ISO must still get the RAM it was configured with.

Observed live on 2026-09-09 building a Windows golden image. The VM was
configured with 8192 MiB; the container log said:

    Warning: Your configured RAM_SIZE of 8192M is too high for the 2.4 GB of
    free memory available, it will automatically be adjusted to a lower amount.
    Allocated 1931 MB of RAM for Windows.

and the install then ground for 71 minutes on a quarter of its RAM. Nothing
failed -- the deploy reported success and the range came up RUNNING.

The VM container was capped at guest + QEMU_OVERHEAD = 10 GiB. Extracting the
7.74 GiB install ISO left 7.96 GiB of page cache inside that cgroup, of which
7.95 GiB was inactive_file and reclaimable. dockur admits a guest by comparing
RAM_SIZE against memory.max - memory.current, page cache counts toward
memory.current, so it saw 2.4 GiB free and quietly shrank the guest.

Confirmed by restarting the container once the ISO was already extracted: with
a cold cache the same 10 GiB container ran the same guest at its full 8192 MiB
(cgroup anon went 2.08 GiB -> 8.65 GiB, no downgrade warning). The limit was
always adequate; only dockur's admission check was fooled.
"""

import pytest

from proving_ground.utils.memory import (
    DIND_DAEMON_RESERVE_MB,
    ISO_CACHE_MARGIN_MB,
    QEMU_OVERHEAD_MB,
    check_guest_fits_range,
    vm_container_memory_mb,
)

# The measured case, in MiB.
GUEST = 8192
ISO = 7738


class TestABootingDiskIsSizedExactlyAsBefore:
    """The common path is a VM booting a disk it already has. It must not pay
    for an ISO it is not mounting."""

    @pytest.mark.parametrize("guest", [2048, 4096, 8192, 16384])
    def test_no_iso_means_guest_plus_qemu(self, guest):
        assert vm_container_memory_mb(guest) == guest + QEMU_OVERHEAD_MB

    def test_an_explicit_none_is_the_same_as_omitting_it(self):
        assert vm_container_memory_mb(GUEST, install_iso_mb=None) == vm_container_memory_mb(GUEST)

    def test_a_zero_sized_iso_does_not_add_headroom(self):
        """0 is 'measured, and it is empty', not 'unknown'. Adding a bare
        margin for it would charge every VM whose ISO could not be sized."""
        assert vm_container_memory_mb(GUEST, install_iso_mb=0) == GUEST + QEMU_OVERHEAD_MB


class TestAnIsoInstallGetsRoomForItsCache:
    def test_the_iso_and_its_margin_are_added(self):
        assert (
            vm_container_memory_mb(GUEST, install_iso_mb=ISO)
            == GUEST + QEMU_OVERHEAD_MB + ISO + ISO_CACHE_MARGIN_MB
        )

    def test_the_measured_case_leaves_the_guest_admissible(self):
        """The regression, stated the way dockur decides it.

        dockur admits the guest when free (limit - cache) >= RAM_SIZE. Under the
        old limit that was 10240 - 8151 = 2089 MiB against a 8192 MiB guest, and
        it downgraded.
        """
        cache = 8151  # 7.96 GiB, as measured
        old = GUEST + QEMU_OVERHEAD_MB
        assert old - cache < GUEST, "the old limit should not have admitted this guest"

        new = vm_container_memory_mb(GUEST, install_iso_mb=ISO)
        assert new - cache >= GUEST, (
            f"an ISO install still cannot be admitted: {new - cache} MiB free "
            f"for a {GUEST} MiB guest"
        )

    @pytest.mark.parametrize("guest", [2048, 4096, 8192])
    def test_it_holds_across_guest_sizes(self, guest):
        """The cache is the ISO's, not the guest's, so the headroom has to work
        for a small guest as well as a large one."""
        limit = vm_container_memory_mb(guest, install_iso_mb=ISO)
        assert limit - (ISO + 413) >= guest  # 413 MiB = the measured overshoot


class TestTheRangeCapIsNeverExceeded:
    """Handing the VM container more than the range can back moves the
    OOM-restart-loop up a level: the range cgroup does the reclaiming and may
    pick its dockerd rather than QEMU."""

    def test_the_limit_is_clamped_to_the_range(self):
        cap = 16384
        limit = vm_container_memory_mb(GUEST, install_iso_mb=ISO, range_cap_mb=cap)
        assert limit <= cap - DIND_DAEMON_RESERVE_MB

    def test_a_roomy_cap_does_not_clamp(self):
        want = vm_container_memory_mb(GUEST, install_iso_mb=ISO)
        assert vm_container_memory_mb(GUEST, install_iso_mb=ISO, range_cap_mb=65536) == want

    def test_clamping_never_returns_less_than_the_guest_itself(self):
        """A cap too small to hold the guest is a validation problem, reported
        by check_guest_fits_range. Returning less than the guest here would
        instead hand Docker a limit that OOM-kills QEMU immediately."""
        limit = vm_container_memory_mb(GUEST, install_iso_mb=ISO, range_cap_mb=2048)
        assert limit >= GUEST

    def test_no_cap_given_means_no_clamp(self):
        assert vm_container_memory_mb(
            GUEST, install_iso_mb=ISO, range_cap_mb=None
        ) == vm_container_memory_mb(GUEST, install_iso_mb=ISO)


class TestValidationStillJudgesResidentMemoryOnly:
    """The ISO allowance is transient reclaimable cache, not memory the guest
    needs to keep. Charging it to the fit check would start refusing guests
    that run perfectly well -- including every one that works today."""

    def test_a_guest_that_fitted_before_still_fits(self):
        assert check_guest_fits_range(GUEST, 16384) is None

    def test_the_fit_check_takes_no_iso_argument(self):
        import inspect

        assert "install_iso_mb" not in inspect.signature(check_guest_fits_range).parameters


class TestTheDeployPathDetectsAnIsoBoot:
    """_install_iso_mb reads the same two things both creation paths set."""

    def _fn(self):
        from proving_ground.services.range_deployment_service import _install_iso_mb

        return _install_iso_mb

    def test_a_mounted_iso_is_measured(self, tmp_path):
        iso = tmp_path / "win11.iso"
        iso.write_bytes(b"\0" * (3 * 1024 * 1024))
        got = self._fn()({"BOOT": "/boot.iso"}, {str(iso): {"bind": "/boot.iso", "mode": "ro"}})
        assert got == 3

    def test_no_boot_variable_means_no_iso(self, tmp_path):
        iso = tmp_path / "win11.iso"
        iso.write_bytes(b"\0" * 1024)
        assert self._fn()({}, {str(iso): {"bind": "/boot.iso"}}) is None

    def test_no_volumes_means_no_iso(self):
        assert self._fn()({"BOOT": "/boot.iso"}, None) is None

    def test_a_volume_that_is_not_the_boot_target_is_ignored(self, tmp_path):
        other = tmp_path / "storage"
        other.mkdir()
        assert self._fn()({"BOOT": "/boot.iso"}, {str(other): {"bind": "/storage"}}) is None

    def test_an_unmeasurable_iso_degrades_to_none(self):
        """The allowance sits on top of a working budget, so losing it must not
        fail the deploy."""
        assert (
            self._fn()({"BOOT": "/boot.iso"}, {"/nonexistent/win.iso": {"bind": "/boot.iso"}})
            is None
        )
