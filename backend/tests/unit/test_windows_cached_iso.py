"""A Windows range must boot from the cached ISO, not the internet.

Range networks are isolated by default, so a range that falls through to
downloading Windows from Microsoft never comes up -- it loops on DNS failures
until someone notices. The host keeps a pre-seeded ISO for exactly this reason.

sync_range had this handling for some time; _deploy_with_dind did not, so the
cache was consulted on a sync but never on the deploy that actually creates the
VM. These are guards against that divergence returning.
"""

import inspect

from proving_ground.services import range_deployment_service


def _source_of(method_name: str) -> str:
    method = getattr(range_deployment_service.RangeDeploymentService, method_name)
    return inspect.getsource(method)


def test_deploy_path_consults_the_windows_iso_cache():
    """The regression: deploy ignored the cache entirely and downloaded instead."""
    src = _source_of("_deploy_with_dind")
    assert "windows-isos" in src, (
        "_deploy_with_dind no longer looks up the cached Windows ISO. An isolated "
        "range cannot download from Microsoft, so the lab will never come up."
    )
    assert "/boot.iso" in src, "_deploy_with_dind resolves an ISO but never mounts it"


def test_deploy_path_defaults_the_windows_version():
    """An empty windows_version built 'windows-.iso' and silently missed."""
    src = _source_of("_deploy_with_dind")
    assert 'vm.windows_version or "11"' in src, (
        "_deploy_with_dind must default the Windows version; without it the cache "
        "filename is malformed and the lookup always misses."
    )


def test_deploy_path_passes_the_guest_shape_to_dockur():
    """Without these dockur ignores the VM's configured CPU/RAM/disk."""
    src = _source_of("_deploy_with_dind")
    for key in ("CPU_CORES", "RAM_SIZE", "DISK_SIZE"):
        assert key in src, f"_deploy_with_dind no longer sets {key}"


def test_both_paths_agree():
    """sync_range and deploy must not diverge on this again."""
    deploy = _source_of("_deploy_with_dind")
    sync = _source_of("sync_range")
    for marker in ("windows-isos", "/boot.iso"):
        assert (marker in deploy) == (marker in sync), (
            f"'{marker}' is handled in one of _deploy_with_dind/sync_range but not "
            "the other -- that divergence is the original bug."
        )


def test_iso_version_is_updatable():
    """A PATCH silently dropped iso_version, so 'Unknown' could not be corrected."""
    from proving_ground.schemas.base_image import BaseImageUpdate

    for field in ("iso_version", "iso_path", "iso_source"):
        assert field in BaseImageUpdate.model_fields, (
            f"BaseImageUpdate must expose {field}; otherwise a PATCH accepts it and "
            "silently discards it."
        )


def test_base_image_windows_version_is_propagated():
    """The UI has no version field -- the base image is what knows it."""
    from proving_ground.api import vms

    src = inspect.getsource(vms.create_vm) if hasattr(vms, "create_vm") else inspect.getsource(vms)
    assert "iso_version" in src, (
        "VM creation must carry base_image.iso_version into vm.windows_version, or "
        "every Windows VM built through the UI starts with it empty."
    )
