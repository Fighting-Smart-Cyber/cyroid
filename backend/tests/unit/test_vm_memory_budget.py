"""A guest must leave room for the emulator and the range's own daemon.

Nothing validated this, so an oversized guest was accepted, deployed, and then
OOM-killed by its cgroup on a loop -- restarting every ~40s with the console
unreachable and nothing obviously wrong in the VM's logs. That is the failure
this turns into a message before anything is deployed.

Three things share a range's cap and only one is the guest:

    guest RAM  +  QEMU overhead (2 GiB, measured)  +  dockerd (512 MiB)
"""

import inspect

from proving_ground.utils.memory import (
    DIND_DAEMON_RESERVE_MB,
    QEMU_OVERHEAD_MB,
    check_guest_fits_range,
    max_guest_ram_mb,
    parse_memory_to_mb,
    vm_container_memory_mb,
)


class TestParsing:
    def test_docker_style_sizes(self):
        assert parse_memory_to_mb("8g") == 8192
        assert parse_memory_to_mb("24g") == 24576
        assert parse_memory_to_mb("512m") == 512
        assert parse_memory_to_mb(8192) == 8192, "an int is already MiB (ram_mb)"

    def test_a_unitless_string_is_refused_rather_than_guessed(self):
        """Docker reads "2048" as bytes, which rounds to 0 MiB.

        A zero cap would switch the budget check off silently, which is worse
        than not parsing it -- callers treat None as "unknown" and skip.
        """
        assert parse_memory_to_mb("2048") is None
        assert parse_memory_to_mb("garbage") is None
        assert parse_memory_to_mb(None) is None


class TestBudget:
    def test_a_guest_that_fits_is_accepted(self):
        assert check_guest_fits_range(4096, 8192) is None

    def test_a_guest_sized_to_the_whole_cap_is_rejected(self):
        problem = check_guest_fits_range(8192, 8192)
        assert problem is not None
        assert "8192" in problem

    def test_the_message_says_what_would_fit(self):
        problem = check_guest_fits_range(6144, 8192)
        assert problem is not None
        assert str(max_guest_ram_mb(8192)) in problem, (
            "The error should tell the user the largest guest that fits, not "
            "just that theirs does not."
        )

    def test_the_boundary_is_exact(self):
        cap = 8192
        largest = max_guest_ram_mb(cap)
        assert check_guest_fits_range(largest, cap) is None, "the largest fitting guest must pass"
        assert check_guest_fits_range(largest + 1, cap) is not None, "one MiB over must fail"

    def test_a_cap_too_small_for_any_guest_says_so(self):
        problem = check_guest_fits_range(1024, QEMU_OVERHEAD_MB)
        assert problem is not None
        assert "cannot host an emulated VM at all" in problem

    def test_an_unknown_cap_skips_the_check(self):
        """Better to deploy than to refuse a VM over a config string we failed
        to read."""
        assert check_guest_fits_range(4096, None) is None

    def test_the_container_limit_matches_what_the_budget_assumes(self):
        """The check and the deploy path must agree, or one will accept what
        the other cannot honour."""
        guest = 4096
        assert vm_container_memory_mb(guest) == guest + QEMU_OVERHEAD_MB
        assert max_guest_ram_mb(8192) == 8192 - QEMU_OVERHEAD_MB - DIND_DAEMON_RESERVE_MB


class TestWiredIn:
    """Every path that sets ram_mb has to check, or the check is decorative."""

    def test_all_three_api_write_paths_validate(self):
        from proving_ground.api import vms as vms_api

        for fn in ("create_vm", "update_vm", "update_vm_resources"):
            src = inspect.getsource(getattr(vms_api, fn))
            assert (
                "_assert_guest_ram_fits_range" in src
            ), f"{fn} can set ram_mb without checking it fits the range."

    def test_predeploy_validation_also_checks(self):
        from proving_ground.services.deployment_validator import DeploymentValidator

        src = inspect.getsource(DeploymentValidator.validate_range)
        assert "_validate_memory_budget" in src, (
            "Pre-deployment validation does not check the memory budget, so a "
            "range built before this existed still deploys into the loop."
        )

    def test_deploy_and_validation_share_one_definition(self):
        """Two copies of the overhead constant would drift apart."""
        from proving_ground.services import range_deployment_service

        src = inspect.getsource(range_deployment_service)
        assert "from proving_ground.utils.memory import" in src, (
            "range_deployment_service defines its own overhead instead of "
            "sharing the one the validation uses."
        )


class TestDraftRangesAreNotRejected:
    """A range that is not deployed yet has no cap to check against.

    It can be given a larger cap at deploy time and nothing records that on the
    Range row -- win11-golden runs at 24 GiB against an 8 GiB default. Rejecting
    at edit time against the default would block configuring a VM for a range
    that will have room for it, so the API only enforces against a cap it can
    actually read from a live container. Deployment is where the cap becomes
    real, and the pre-deploy validator gates it there.
    """

    def test_the_api_check_does_not_fall_back_to_the_default_cap(self):
        from proving_ground.api import vms as vms_api

        src = inspect.getsource(vms_api._assert_guest_ram_fits_range)
        assert "range_default_memory" not in src, (
            "The API rejects against the configured default, so a VM cannot be "
            "sized for a range that will be deployed with a larger cap."
        )
        assert (
            "if not cap_mb:\n        return" in src
        ), "An unknown cap must skip the check rather than guess at one."

    def test_predeploy_validation_still_uses_the_default(self):
        """At deploy time the cap is determined, so this is where it is caught."""
        from proving_ground.services.deployment_validator import DeploymentValidator

        src = inspect.getsource(DeploymentValidator._validate_memory_budget)
        assert "range_default_memory" in src, (
            "Pre-deployment validation must fall back to the cap the deploy "
            "will actually use, or nothing catches an oversized guest."
        )
