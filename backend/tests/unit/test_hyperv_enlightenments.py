"""Nested Hyper-V enlightenments are withheld from guests on a Hyper-V host.

dockur sets HV_FEATURES="hv_passthrough" whenever KVM is enabled. On a host
that is ITSELF a Hyper-V guest -- any Azure VM -- handing those enlightenments
to a nested Windows guest makes it shut down a few seconds after the boot
manager starts. Nothing is logged by QEMU or KVM; the container simply exits
and restarts, so it reads as a corrupt disk or a broken image rather than a
single wrong flag.

Measured on pg-ubu with the same image, ISO, CPU and memory, changing only HV:

    HV default (hv_passthrough)  ->  exited, "Shutdown completed!"
    HV=false                     ->  installed Windows 11 and ran for days

The first attempt at that comparison also changed the dockur version by
accident (a `:latest` pull, QEMU v11.1.0, against the platform's pinned
v10.0.11), so the control was re-run on the same image to isolate HV.

Detection uses the clocksource, which is what dockur itself reads:

    pg-ec2 (AWS)    tsc
    pg-ubu (Azure)  hyperv_clocksource_tsc_page
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from proving_ground.services import range_deployment_service as rds

HYPERV = "hyperv_clocksource_tsc_page\n"
BARE = "tsc\n"


def _svc(kvm="Y", clocksource=BARE, clock_raises=None):
    s = rds.RangeDeploymentService.__new__(rds.RangeDeploymentService)
    s.dind_service = MagicMock()
    if clock_raises is not None:
        s.dind_service.exec_in_container = AsyncMock(side_effect=clock_raises)
    else:
        s.dind_service.exec_in_container = AsyncMock(return_value=(0, clocksource))
    s._kvm_for_range = AsyncMock(return_value=kvm)
    return s


def _cfg(monkeypatch, hyperv="auto"):
    c = MagicMock()
    c.range_hyperv = hyperv
    monkeypatch.setattr(rds, "get_settings", lambda: c)


class TestAutoFollowsTheHost:
    @pytest.mark.asyncio
    async def test_hyperv_host_gets_enlightenments_disabled(self, monkeypatch):
        _cfg(monkeypatch)
        env = await _svc(kvm="Y", clocksource=HYPERV)._virt_env_for_range("r1")
        assert env["KVM"] == "Y"
        assert env["HV"] == "false"

    @pytest.mark.asyncio
    async def test_bare_host_keeps_dockurs_default(self, monkeypatch):
        """Passthrough is a real speedup where it works; only withhold it where
        it is measured to break the guest."""
        _cfg(monkeypatch)
        env = await _svc(kvm="Y", clocksource=BARE)._virt_env_for_range("r1")
        assert env["KVM"] == "Y"
        assert "HV" not in env


class TestItOnlyMattersWhenKvmIsOn:
    @pytest.mark.asyncio
    async def test_no_hv_key_when_kvm_is_off(self, monkeypatch):
        """dockur ignores HV under emulation; setting it would be noise that
        implies a decision was made."""
        _cfg(monkeypatch)
        env = await _svc(kvm="N", clocksource=HYPERV)._virt_env_for_range("r1")
        assert env == {"KVM": "N"}

    @pytest.mark.asyncio
    async def test_it_does_not_probe_the_clocksource_when_kvm_is_off(self, monkeypatch):
        _cfg(monkeypatch)
        s = _svc(kvm="N", clocksource=HYPERV)
        await s._virt_env_for_range("r1")
        s.dind_service.exec_in_container.assert_not_awaited()


class TestTheOverride:
    @pytest.mark.asyncio
    async def test_on_keeps_passthrough_even_on_a_hyperv_host(self, monkeypatch):
        _cfg(monkeypatch, "on")
        env = await _svc(kvm="Y", clocksource=HYPERV)._virt_env_for_range("r1")
        assert "HV" not in env

    @pytest.mark.asyncio
    async def test_off_disables_them_even_on_a_bare_host(self, monkeypatch):
        _cfg(monkeypatch, "off")
        env = await _svc(kvm="Y", clocksource=BARE)._virt_env_for_range("r1")
        assert env["HV"] == "false"

    @pytest.mark.asyncio
    async def test_explicit_settings_do_not_probe(self, monkeypatch):
        for value in ("on", "off"):
            _cfg(monkeypatch, value)
            s = _svc(kvm="Y", clocksource=HYPERV)
            await s._virt_env_for_range("r1")
            s.dind_service.exec_in_container.assert_not_awaited()


class TestTheProbeFailsSafe:
    @pytest.mark.asyncio
    async def test_a_probe_error_leaves_the_default_alone(self, monkeypatch):
        """Not knowing must not silently change what the guest is given."""
        _cfg(monkeypatch)
        s = _svc(kvm="Y", clock_raises=RuntimeError("dind gone"))
        env = await s._virt_env_for_range("r1")
        assert env == {"KVM": "Y"}

    @pytest.mark.asyncio
    async def test_a_nonzero_exit_is_not_read_as_hyperv(self, monkeypatch):
        _cfg(monkeypatch)
        s = rds.RangeDeploymentService.__new__(rds.RangeDeploymentService)
        s.dind_service = MagicMock()
        s.dind_service.exec_in_container = AsyncMock(return_value=(1, "No such file"))
        assert await s._host_is_hyperv_guest("r1") is False


class TestTheSetting:
    def test_auto_is_the_default(self, monkeypatch):
        from proving_ground.config import Settings

        monkeypatch.delenv("RANGE_HYPERV", raising=False)
        assert Settings(_env_file=None).range_hyperv == "auto"

    @pytest.mark.parametrize("value", ["auto", "on", "off", "AUTO", " off "])
    def test_accepted_values(self, value):
        from proving_ground.config import Settings

        assert Settings(range_hyperv=value).range_hyperv == value.strip().lower()

    def test_a_typo_is_refused(self):
        from pydantic import ValidationError

        from proving_ground.config import Settings

        with pytest.raises(ValidationError):
            Settings(range_hyperv="disabled")


class TestNothingSetsHvDirectly:
    def test_guest_creation_goes_through_the_helper(self):
        import inspect

        src = inspect.getsource(rds)
        code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
        assert 'environment["HV"]' not in code, "HV is being set outside _virt_env_for_range"
        assert src.count("_virt_env_for_range(") >= 4, "expected the definition plus three sites"
