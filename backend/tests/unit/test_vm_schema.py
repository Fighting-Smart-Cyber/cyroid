# backend/tests/unit/test_vm_schema.py
"""Unit tests for VM schema validation.

These were written against an earlier two-field contract — `template_id` and
`snapshot_id` — which no longer exists. `VMCreate` now takes exactly one of three
Image Library sources: `base_image_id`, `golden_image_id` or `snapshot_id`. The
tests below exercise that contract instead; the schema was not changed to suit
them, because the three-way union is the deliberate design.
"""
import pytest
from pydantic import ValidationError

from proving_ground.schemas.vm import VMCreate

BASE = dict(
    range_id="00000000-0000-0000-0000-000000000001",
    network_id="00000000-0000-0000-0000-000000000002",
    hostname="test-vm",
    ip_address="10.0.1.10",
    cpu=2,
    ram_mb=4096,
    disk_gb=40,
)

BASE_IMAGE = "00000000-0000-0000-0000-000000000003"
GOLDEN_IMAGE = "00000000-0000-0000-0000-000000000004"
SNAPSHOT = "00000000-0000-0000-0000-000000000005"

SOURCES = [
    ("base_image_id", BASE_IMAGE),
    ("golden_image_id", GOLDEN_IMAGE),
    ("snapshot_id", SNAPSHOT),
]


class TestVMCreateImageSource:
    """Exactly one image source is required."""

    def test_rejects_no_image_source(self):
        with pytest.raises(ValidationError) as exc_info:
            VMCreate(**BASE)
        assert "must provide exactly one of" in str(exc_info.value).lower()

    @pytest.mark.parametrize("field,value", SOURCES)
    def test_accepts_exactly_one_source(self, field, value):
        vm = VMCreate(**BASE, **{field: value})
        assert getattr(vm, field) is not None
        # ...and the other two stay unset.
        for other, _ in SOURCES:
            if other != field:
                assert getattr(vm, other) is None

    @pytest.mark.parametrize(
        "first,second",
        [
            ("base_image_id", "golden_image_id"),
            ("base_image_id", "snapshot_id"),
            ("golden_image_id", "snapshot_id"),
        ],
    )
    def test_rejects_two_sources(self, first, second):
        values = dict(SOURCES)
        with pytest.raises(ValidationError) as exc_info:
            VMCreate(**BASE, **{first: values[first], second: values[second]})
        assert "cannot specify multiple image sources" in str(exc_info.value).lower()

    def test_rejects_all_three_sources(self):
        with pytest.raises(ValidationError) as exc_info:
            VMCreate(**BASE, **dict(SOURCES))
        assert "cannot specify multiple image sources" in str(exc_info.value).lower()

    def test_template_id_is_gone(self):
        """The old field is not silently accepted.

        Pydantic ignores unknown keyword arguments by default, so passing
        `template_id` would otherwise look like it worked while setting nothing —
        which is exactly how these tests rotted without anyone noticing.
        """
        vm = VMCreate(**BASE, base_image_id=BASE_IMAGE, template_id=BASE_IMAGE)
        assert not hasattr(vm, "template_id")
