"""A claimed pool container's volume name cannot be derived from the range id."""

import pytest


def test_create_returns_volume_name():
    from proving_ground.services.dind_service import DinDService
    import inspect

    src = inspect.getsource(DinDService.create_range_container)
    assert (
        '"volume_name"' in src or "'volume_name'" in src
    ), "create_range_container must report the volume it created"


def test_delete_accepts_an_explicit_volume_name():
    from proving_ground.services.dind_service import DinDService
    import inspect

    sig = inspect.signature(DinDService.delete_range_container)
    assert (
        "volume_name" in sig.parameters
    ), "teardown must accept a recorded volume name, not only derive one"


def test_range_model_has_volume_column():
    from proving_ground.models.range import Range

    assert hasattr(Range, "dind_volume_name")
