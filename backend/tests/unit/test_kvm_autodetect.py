"""Guests get hardware virtualisation when the range can actually provide it.

The DinD deploy path hardcoded `environment["KVM"] = "N"` in three places,
commented "Disable KVM requirement for Docker Desktop / nested virtualization".
That was right for Docker Desktop and for hosts without nested virt, and it
silently cost a ~10x slowdown on every host that had it -- dockur prints

    Warning: KVM acceleration is disabled, this will cause the machine to run
    about 10 times slower!

which reads like a capability check and is not one: it only reports that the
KVM env var was set to off. Directly beneath the hardcoded "N" sat
`privileged = True  # Required for KVM access`, so the code granted the access
and then declined to use it.

The obvious repair -- os.path.exists("/dev/kvm"), as the legacy docker_service
path does -- is also wrong here, and wrong in a way that looks right: the API
and worker containers have no /dev/kvm mapped in, so it answers about the wrong
machine and returns False on a host that has it. The probe therefore runs
inside the range's own DinD container, which is where the guest runs.
"""

import inspect
from unittest.mock import AsyncMock, MagicMock

import pytest

from proving_ground.services import range_deployment_service as rds


def _svc(probe_exit=0, probe_raises=None):
    s = rds.RangeDeploymentService.__new__(rds.RangeDeploymentService)
    s.dind_service = MagicMock()
    if probe_raises is not None:
        s.dind_service.exec_in_container = AsyncMock(side_effect=probe_raises)
    else:
        s.dind_service.exec_in_container = AsyncMock(return_value=(probe_exit, ""))
    return s


def _with_setting(monkeypatch, value):
    cfg = MagicMock()
    cfg.range_kvm = value
    monkeypatch.setattr(rds, "get_settings", lambda: cfg)


class TestAutoAsksTheRangeNotTheHost:
    @pytest.mark.asyncio
    async def test_kvm_present_in_the_range_enables_it(self, monkeypatch):
        _with_setting(monkeypatch, "auto")
        s = _svc(probe_exit=0)
        assert await s._kvm_for_range("r1") == "Y"

    @pytest.mark.asyncio
    async def test_kvm_absent_in_the_range_leaves_it_off(self, monkeypatch):
        _with_setting(monkeypatch, "auto")
        s = _svc(probe_exit=1)
        assert await s._kvm_for_range("r1") == "N"

    @pytest.mark.asyncio
    async def test_it_probes_the_dind_container(self, monkeypatch):
        """Not the host, and not this process -- neither is where the guest runs."""
        _with_setting(monkeypatch, "auto")
        s = _svc(probe_exit=0)
        await s._kvm_for_range("r1")
        s.dind_service.exec_in_container.assert_awaited_once_with("r1", ["test", "-e", "/dev/kvm"])


class TestItFailsClosed:
    @pytest.mark.asyncio
    async def test_a_probe_error_does_not_enable_kvm(self, monkeypatch):
        """KVM="Y" where KVM is unusable makes QEMU refuse to start. A slow guest
        beats one that never boots."""
        _with_setting(monkeypatch, "auto")
        s = _svc(probe_raises=RuntimeError("dind gone"))
        assert await s._kvm_for_range("r1") == "N"

    @pytest.mark.asyncio
    async def test_a_probe_error_does_not_fail_the_deploy(self, monkeypatch):
        _with_setting(monkeypatch, "auto")
        s = _svc(probe_raises=RuntimeError("dind gone"))
        await s._kvm_for_range("r1")  # must not raise


class TestTheOverride:
    @pytest.mark.asyncio
    async def test_off_never_probes(self, monkeypatch):
        """A host where nested virt is present but broken is a real failure mode
        and miserable to diagnose from a guest that merely hangs."""
        _with_setting(monkeypatch, "off")
        s = _svc(probe_exit=0)
        assert await s._kvm_for_range("r1") == "N"
        s.dind_service.exec_in_container.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_on_never_probes(self, monkeypatch):
        _with_setting(monkeypatch, "on")
        s = _svc(probe_exit=1)
        assert await s._kvm_for_range("r1") == "Y"
        s.dind_service.exec_in_container.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_off_beats_a_present_kvm(self, monkeypatch):
        _with_setting(monkeypatch, "off")
        s = _svc(probe_exit=0)
        assert await s._kvm_for_range("r1") == "N"


class TestCrossArchIsAlwaysEmulated:
    @pytest.mark.asyncio
    async def test_emulated_arch_gets_no_kvm_even_when_forced_on(self, monkeypatch):
        """KVM cannot accelerate instructions the host CPU does not have."""
        _with_setting(monkeypatch, "on")
        monkeypatch.setattr(rds, "requires_emulation", lambda a: True)
        s = _svc(probe_exit=0)
        assert await s._kvm_for_range("r1", "arm64") == "N"

    @pytest.mark.asyncio
    async def test_native_arch_is_unaffected(self, monkeypatch):
        _with_setting(monkeypatch, "auto")
        monkeypatch.setattr(rds, "requires_emulation", lambda a: False)
        s = _svc(probe_exit=0)
        assert await s._kvm_for_range("r1", "x86_64") == "Y"


class TestNothingHardcodesItOffAnyMore:
    def test_the_dind_path_has_no_literal_kvm_n(self):
        src = inspect.getsource(rds)
        code = "\n".join(ln for ln in src.splitlines() if not ln.lstrip().startswith("#"))
        assert 'environment["KVM"] = "N"' not in code, (
            "a hardcoded KVM=N is back in the DinD path; that is the ~10x "
            "slowdown this change removed"
        )

    def test_every_guest_creation_site_consults_the_helper(self):
        """Guest creation now goes through _virt_env_for_range, which decides
        KVM and the Hyper-V enlightenments together because HV only means
        anything when KVM is on. The requirement is unchanged -- no site may
        decide for itself -- only the name it calls.
        """
        src = inspect.getsource(rds)
        assert (
            src.count("_virt_env_for_range(") >= 4
        ), "expected the definition plus all three guest-creation sites"
        assert "_kvm_for_range(" in src, "the KVM decision was dropped entirely"


class TestTheSetting:
    def test_off_is_the_default(self, monkeypatch):
        """Deliberately not "auto".

        Detection is correct, but a Windows golden image built under TCG does
        not survive being given real virtualisation -- measured on a pristine
        disk, the guest boots and shuts down again every ~17s and never reaches
        RDP. Defaulting to auto would break every existing Windows range on any
        KVM-capable host. Enabling it is safe only once that host's golden
        images have been rebuilt under KVM.
        """
        from proving_ground.config import Settings

        monkeypatch.delenv("RANGE_KVM", raising=False)
        assert Settings(_env_file=None).range_kvm == "off"

    @pytest.mark.parametrize("value", ["auto", "on", "off", "AUTO", " off "])
    def test_accepted_values(self, value):
        from proving_ground.config import Settings

        assert Settings(range_kvm=value).range_kvm == value.strip().lower()

    def test_a_typo_is_refused(self):
        from pydantic import ValidationError

        from proving_ground.config import Settings

        with pytest.raises(ValidationError):
            Settings(range_kvm="yes")
