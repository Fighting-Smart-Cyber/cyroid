"""A network isolation rule that fails to apply must not be reported as applied.

setup_network_isolation mixed check=True calls with bare capture_output=True ones, and the unchecked
set included the DROP rules for the host's own addresses - the rules that stop a range reaching the
machine it runs on. A failure there returned True, and both callers discarded the return value
anyway, so the database recorded is_isolated=True for a network with no such isolation. These tests
pin the failure path shut: the whole point is that the function reports False, loudly, rather than
claiming an isolation it did not achieve.
"""

import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from proving_ground.services.docker_service import DockerService

SUBNET = "10.90.1.0/24"
NETWORK_ID = "abcdef123456789"


def service():
    """A DockerService without __init__, so no Docker daemon is needed."""
    svc = object.__new__(DockerService)
    svc.client = MagicMock()
    svc.client.networks.get.return_value = SimpleNamespace(name="pg-test-net")
    return svc


def completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def runner(failing_args=None, fail_stderr="Permission denied", host_ips="192.168.1.50 10.90.1.1"):
    """Fake subprocess.run: succeeds unless the command contains all of `failing_args`."""
    failing = failing_args or []

    def _run(cmd, *a, **kw):
        if cmd[:1] == ["hostname"]:
            return completed(stdout=host_ips)
        if failing and all(token in cmd for token in failing):
            # Honour check=True the way subprocess does. Without this the fake is more forgiving
            # than reality, which would make these tests pass against code that only looked
            # correct - and would have made the before/after comparison meaningless.
            if kw.get("check"):
                raise subprocess.CalledProcessError(1, cmd, stderr=fail_stderr)
            return completed(returncode=1, stderr=fail_stderr)
        return completed()

    return _run


def test_all_rules_applied_reports_success():
    with patch("subprocess.run", side_effect=runner()):
        assert service().setup_network_isolation(NETWORK_ID, SUBNET) is True


def test_a_host_interface_drop_that_fails_is_not_reported_as_isolated():
    """The regression this file exists for: this rule used to fail silently and still return True."""
    with patch("subprocess.run", side_effect=runner(failing_args=["-d", "192.168.1.50/32"])):
        assert service().setup_network_isolation(NETWORK_ID, SUBNET) is False


def test_a_blocked_destination_drop_that_fails_is_not_reported_as_isolated():
    with patch("subprocess.run", side_effect=runner(failing_args=["-d", "172.17.0.0/16"])):
        assert service().setup_network_isolation(NETWORK_ID, SUBNET) is False


def test_an_unusable_iptables_is_not_reported_as_isolated():
    """No CAP_NET_ADMIN is the case that matters if these containers are ever run unprivileged."""
    with patch("subprocess.run", side_effect=runner(failing_args=["-N"])):
        assert service().setup_network_isolation(NETWORK_ID, SUBNET) is False


def test_an_existing_chain_is_not_treated_as_a_failure():
    """Re-deploying a range hits an existing chain; that is expected, not a failure."""
    run = runner(failing_args=["-N"], fail_stderr="iptables: Chain already exists.")
    with patch("subprocess.run", side_effect=run):
        assert service().setup_network_isolation(NETWORK_ID, SUBNET) is True


def test_not_knowing_the_host_addresses_is_not_reported_as_isolated():
    def _run(cmd, *a, **kw):
        if cmd[:1] == ["hostname"]:
            return completed(returncode=1, stderr="boom")
        return completed()

    with patch("subprocess.run", side_effect=_run):
        assert service().setup_network_isolation(NETWORK_ID, SUBNET) is False


def test_no_host_addresses_warns_rather_than_passing_quietly(caplog):
    with patch("subprocess.run", side_effect=runner(host_ips="")):
        with caplog.at_level("WARNING"):
            assert service().setup_network_isolation(NETWORK_ID, SUBNET) is True
    assert any("no host-interface DROP rules" in r.message for r in caplog.records)


def test_teardown_tolerates_rules_that_are_already_gone():
    run = runner(failing_args=["-D"], fail_stderr="No chain/target/match by that name")
    with patch("subprocess.run", side_effect=run):
        assert service().teardown_network_isolation(NETWORK_ID, SUBNET) is True


def test_teardown_logs_a_failure_it_cannot_explain(caplog):
    run = runner(failing_args=["-X"], fail_stderr="Permission denied")
    with patch("subprocess.run", side_effect=run):
        with caplog.at_level("WARNING"):
            assert service().teardown_network_isolation(NETWORK_ID, SUBNET) is True
    assert any("Permission denied" in r.message for r in caplog.records), (
        "a teardown failure that is not 'already gone' must be logged, or a stale jump rule is left "
        "behind with nothing recording it"
    )


@pytest.mark.parametrize("required", [True, False])
def test_the_helper_raises_only_for_required_rules(required):
    with patch("subprocess.run", side_effect=lambda *a, **k: completed(1, stderr="nope")):
        if required:
            with pytest.raises(RuntimeError, match="nope"):
                service()._iptables(["-A", "X"], required=True)
        else:
            assert service()._iptables(["-A", "X"], required=False) == 1
