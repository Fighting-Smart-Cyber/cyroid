"""An uploaded OVA must not be able to write outside the directory we chose.

An OVA is a tar archive, and `_convert_ova` unpacked it with a bare
`tar.extractall(extract_dir)`. tar archives can name members with `..`, with an
absolute path, or as symlinks pointing anywhere, and unfiltered extractall
honours all three. The process doing the extracting is the API, which runs as
root in its container and mounts the shared data volume, so a crafted upload
was a write to essentially anywhere the install can reach.

The fix is two things on purpose: tarfile's own "data" filter where the
interpreter has one, and an explicit inspection of every member before a byte
is written. The filter is the one that keeps up with attacks nobody has thought
of yet; the inspection is the one that does not change under us between point
releases, and it is what these tests assert.
"""

import asyncio
import io
import tarfile
from pathlib import Path

import pytest

from proving_ground.services.image_import_service import (
    ImageImportService,
    _reject_unsafe_members,
)


def _archive(path: Path, *members) -> Path:
    """Write a tar of (TarInfo, payload-or-None) pairs."""
    with tarfile.open(path, "w") as tar:
        for info, payload in members:
            if payload is None:
                tar.addfile(info)
            else:
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
    return path


def _file(name: str, payload: bytes = b"disk"):
    info = tarfile.TarInfo(name)
    info.size = len(payload)
    return info, payload


def _symlink(name: str, target: str):
    info = tarfile.TarInfo(name)
    info.type = tarfile.SYMTYPE
    info.linkname = target
    return info, None


def _device(name: str):
    info = tarfile.TarInfo(name)
    info.type = tarfile.CHRTYPE
    info.devmajor, info.devminor = 1, 3
    return info, None


@pytest.fixture
def service(tmp_path, monkeypatch):
    """A service whose storage directory is the test's own, not /data."""
    monkeypatch.setattr(ImageImportService, "GOLDEN_IMAGES_DIR", tmp_path / "golden-images")
    return ImageImportService()


class _RecordingConvert:
    """Stands in for qemu-img, which unit tests cannot run."""

    def __init__(self):
        self.converted = None

    async def __call__(self, vmdk_path, temp_dir):
        self.converted = vmdk_path
        return vmdk_path


def _extract(service, tmp_path, ova):
    """Run the real OVA path, with the qemu-img call stubbed out."""
    stub = _RecordingConvert()
    service._convert_vmdk = stub
    work = tmp_path / "work"
    work.mkdir()
    asyncio.run(service._convert_ova(ova, work))
    return stub, work / "ova_contents"


class TestTheExtractionRefusesAnEscape:
    def test_a_traversal_member_is_refused_and_writes_nothing(self, service, tmp_path):
        ova = _archive(
            tmp_path / "traversal.ova",
            _file("vm.ovf", b"<Envelope/>"),
            _file("../escape.txt", b"owned"),
        )

        with pytest.raises(ValueError) as e:
            _extract(service, tmp_path, ova)

        assert "escape.txt" in str(e.value), "the refusal must name the member it refused"
        assert not (tmp_path / "escape.txt").exists()
        assert not (tmp_path / "work" / "escape.txt").exists()

    def test_nothing_is_written_before_the_archive_is_judged(self, service, tmp_path):
        """The check runs over every member first, so a good member sitting
        ahead of a bad one is not already on disk when the bad one is found.
        Extraction stops where it hits trouble; refusing up front is the only
        way the directory is left as it was."""
        ova = _archive(
            tmp_path / "mixed.ova",
            _file("vm.ovf", b"<Envelope/>"),
            _file("vm-disk1.vmdk", b"disk"),
            _file("../escape.txt", b"owned"),
        )

        with pytest.raises(ValueError):
            _extract(service, tmp_path, ova)

        assert list((tmp_path / "work" / "ova_contents").iterdir()) == []

    def test_a_symlink_member_is_refused(self, service, tmp_path):
        """A symlink is the indirect version of the same write: extract
        `disk.vmdk -> /etc/passwd` and the next member named `disk.vmdk`
        follows it."""
        ova = _archive(
            tmp_path / "symlink.ova",
            _file("vm.ovf", b"<Envelope/>"),
            _symlink("vm-disk1.vmdk", "/etc/passwd"),
        )

        with pytest.raises(ValueError) as e:
            _extract(service, tmp_path, ova)

        assert "vm-disk1.vmdk" in str(e.value)
        assert "link" in str(e.value)

    def test_an_absolute_member_is_refused(self, service, tmp_path):
        outside = tmp_path / "absolute-escape"
        ova = _archive(
            tmp_path / "absolute.ova",
            _file(str(outside), b"owned"),
        )

        with pytest.raises(ValueError) as e:
            _extract(service, tmp_path, ova)

        assert "absolute" in str(e.value)
        assert not outside.exists()

    def test_a_hard_link_member_is_refused(self, tmp_path):
        ova = _archive(tmp_path / "hardlink.ova", _file("vm.ovf", b"<Envelope/>"))
        with tarfile.open(ova, "r") as tar:
            _reject_unsafe_members(tar, tmp_path)

        info = tarfile.TarInfo("vm-disk1.vmdk")
        info.type = tarfile.LNKTYPE
        info.linkname = "vm.ovf"
        linked = _archive(tmp_path / "hardlink2.ova", _file("vm.ovf", b"<Envelope/>"), (info, None))

        with tarfile.open(linked, "r") as tar:
            with pytest.raises(ValueError) as e:
                _reject_unsafe_members(tar, tmp_path)
        assert "vm-disk1.vmdk" in str(e.value)

    def test_a_device_member_is_refused(self, tmp_path):
        ova = _archive(tmp_path / "device.ova", _device("vm-disk1.vmdk"))

        with tarfile.open(ova, "r") as tar:
            with pytest.raises(ValueError) as e:
                _reject_unsafe_members(tar, tmp_path)

        assert "vm-disk1.vmdk" in str(e.value)

    def test_a_relative_member_that_stays_inside_is_fine(self, tmp_path):
        """`./disks/../vm.vmdk` resolves inside the extraction root. Refusing
        it would reject archives that are merely written oddly, and a guard
        that fires on benign input gets switched off."""
        ova = _archive(tmp_path / "odd.ova", _file("./disks/vm-disk1.vmdk", b"disk"))

        with tarfile.open(ova, "r") as tar:
            _reject_unsafe_members(tar, tmp_path)


class TestAWellFormedOVAStillImports:
    def test_the_disk_is_extracted_and_handed_to_the_converter(self, service, tmp_path):
        ova = _archive(
            tmp_path / "good.ova",
            _file("vm.ovf", b"<Envelope/>"),
            _file("vm-disk1.vmdk", b"x" * 64),
        )

        stub, extracted = _extract(service, tmp_path, ova)

        assert (extracted / "vm.ovf").read_bytes() == b"<Envelope/>"
        assert stub.converted == extracted / "vm-disk1.vmdk"

    def test_an_ova_with_no_disk_is_a_clear_refusal(self, service, tmp_path):
        """Named here because api/images.py reports ValueError as a 400: this
        one is the caller's archive being wrong, not the server failing."""
        ova = _archive(tmp_path / "empty.ova", _file("vm.ovf", b"<Envelope/>"))

        with pytest.raises(ValueError) as e:
            _extract(service, tmp_path, ova)

        assert "VMDK" in str(e.value)
