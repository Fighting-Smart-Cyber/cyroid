"""The capability contract -- ADR-0004, COSMOS PG #266.

These pin the rules that are easy to erode later: scope is required, images are digest-pinned,
charts are used unmodified, and verify() returns evidence rather than a bare boolean.
"""

import inspect

import pytest

from proving_ground.capability.models import Scope
from proving_ground.capability.runtime import (
    CapabilityRuntime,
    CapabilitySpec,
    ChartRef,
    HookSpec,
    ImageRef,
    VerifyResult,
)

DIGEST = "registry.example/logc2@sha256:" + "a" * 64


def chart(**kw):
    base = dict(name="logc2", version="1.4.2", repository="https://charts.example/logc2")
    base.update(kw)
    return ChartRef(**base)


def hook(**kw):
    base = dict(image=ImageRef(DIGEST), command=("/bin/verify",))
    base.update(kw)
    return HookSpec(**base)


def spec(**kw):
    base = dict(
        name="logc2", version="1.0.0", chart=chart(), scope=Scope.PER_LEARNER, verify=hook()
    )
    base.update(kw)
    return CapabilitySpec(**base)


class TestScopeIsRequiredWithNoDefault:
    def test_a_valid_scope_is_accepted(self):
        assert spec(scope=Scope.SHARED).scope is Scope.SHARED

    def test_none_is_rejected(self):
        with pytest.raises(ValueError, match="must declare a scope"):
            spec(scope=None)

    def test_a_bare_string_is_rejected(self):
        # StrEnum compares equal to its value, so a plain string is easy to smuggle in.
        with pytest.raises(ValueError, match="must declare a scope"):
            spec(scope="per-learner")

    def test_capability_spec_has_no_scope_default(self):
        sig = inspect.signature(CapabilitySpec)
        assert sig.parameters["scope"].default is inspect.Parameter.empty


class TestImagesArePinnedByDigest:
    def test_digest_is_accepted(self):
        assert ImageRef(DIGEST).ref == DIGEST

    @pytest.mark.parametrize(
        "ref",
        [
            "registry.example/logc2:1.4.2",
            "registry.example/logc2:latest",
            "registry.example/logc2",
            "registry.example/logc2@sha256:tooshort",
            "registry.example/logc2@md5:" + "a" * 32,
        ],
    )
    def test_anything_unpinned_is_rejected(self, ref):
        with pytest.raises(ValueError, match="digest-pinned"):
            ImageRef(ref)


class TestChartsAreUsedUnmodified:
    def test_chart_version_must_be_pinned(self):
        with pytest.raises(ValueError, match="pinned"):
            chart(version="latest")
        with pytest.raises(ValueError, match="pinned"):
            chart(version="")

    def test_there_is_no_way_to_patch_the_chart(self):
        # A wrapper, never a fork: values and hooks are ours, the chart is upstream's.
        fields = set(inspect.signature(ChartRef).parameters)
        assert fields == {"name", "version", "repository"}
        assert not (fields & {"patch", "patches", "overlay", "template", "source"})


class TestVerifyReturnsEvidence:
    def test_evidence_is_required(self):
        with pytest.raises(ValueError, match="evidence"):
            VerifyResult(passed=True, evidence={}, observed_at="2026-09-13T00:00:00Z")

    def test_a_pass_carries_its_evidence(self):
        r = VerifyResult(
            passed=True,
            evidence={"convoy_id": "C-17", "status": "DISPATCHED"},
            observed_at="2026-09-13T00:00:00Z",
        )
        assert r.passed and r.evidence["status"] == "DISPATCHED"


class TestThereIsExactlyOneInterface:
    def test_the_five_operations_plus_uninstall_are_the_whole_surface(self):
        assert CapabilityRuntime.__abstractmethods__ == frozenset(
            {"install", "seed", "reset", "verify", "exec", "uninstall"}
        )

    def test_reset_documents_that_it_is_scope_aware(self):
        # The scope-awareness of reset() is the contract, not an implementation detail.
        assert "scope" in (CapabilityRuntime.reset.__doc__ or "").lower()

    def test_the_runtime_cannot_be_instantiated_directly(self):
        with pytest.raises(TypeError):
            CapabilityRuntime()
