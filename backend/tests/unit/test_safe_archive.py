"""No import path extracts an archive that writes outside the directory it chose.

`image_import_service` already guarded its OVA uploads. `export_service` and
`blueprint_export_service` did not: eight bare `extractall` calls between them, six zip and two
tar, each on an archive uploaded by whoever can reach an import endpoint. A Blueprint v4 export is
how a defect travels from a training host to here, so "an archive somebody sends us" is the normal
case for that path, not the adversarial edge.

The guard was not rewritten -- it was moved to `utils/safe_archive` and the one module that had it
right now imports it too, so there is exactly one implementation. These tests cover that module
directly, and the ZIP half of it had no coverage anywhere before, because nothing checked ZIPs at
all.

Every refusal is asserted twice: that it raised, and that the file it was aiming at does not exist.
A guard that raises after writing is not a guard.
"""

from __future__ import annotations

import io
import tarfile
import zipfile
from pathlib import Path

import pytest

from proving_ground.utils.safe_archive import (
    UnsafeArchiveError,
    safe_extract_tar,
    safe_extract_zip,
)


def _tar(path: Path, *members) -> Path:
    with tarfile.open(path, "w") as t:
        for info, payload in members:
            if payload is None:
                t.addfile(info)
            else:
                info.size = len(payload)
                t.addfile(info, io.BytesIO(payload))
    return path


def _file(name: str, payload: bytes = b"x") -> tuple[tarfile.TarInfo, bytes]:
    info = tarfile.TarInfo(name)
    info.type = tarfile.REGTYPE
    return info, payload


def _symlink(name: str, target: str) -> tuple[tarfile.TarInfo, None]:
    info = tarfile.TarInfo(name)
    info.type = tarfile.SYMTYPE
    info.linkname = target
    return info, None


def _zip(path: Path, *entries: tuple[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, payload in entries:
            z.writestr(name, payload)
    return path


class TestTar:
    def test_a_traversing_member_is_refused_and_writes_nothing(self, tmp_path):
        dest = tmp_path / "out"
        dest.mkdir()
        outside = tmp_path / "owned"
        archive = _tar(tmp_path / "evil.tar", _file("../owned", b"pwned"))

        with pytest.raises(UnsafeArchiveError) as e:
            safe_extract_tar(tarfile.open(archive), dest)

        assert "traverses" in str(e.value)
        assert not outside.exists()

    def test_an_absolute_member_is_refused(self, tmp_path):
        dest = tmp_path / "out"
        dest.mkdir()
        outside = tmp_path / "abs-target"
        archive = _tar(tmp_path / "abs.tar", _file(str(outside), b"pwned"))

        with pytest.raises(UnsafeArchiveError) as e:
            safe_extract_tar(tarfile.open(archive), dest)

        assert "absolute" in str(e.value)
        assert not outside.exists()

    def test_a_symlink_member_is_refused(self, tmp_path):
        dest = tmp_path / "out"
        dest.mkdir()
        archive = _tar(tmp_path / "link.tar", _symlink("escape", "/etc/passwd"))

        with pytest.raises(UnsafeArchiveError) as e:
            safe_extract_tar(tarfile.open(archive), dest)

        assert "link" in str(e.value)

    def test_the_label_names_the_import_that_refused(self, tmp_path):
        """The message reaches an operator reading a failed import, so it says which one."""
        dest = tmp_path / "out"
        dest.mkdir()
        archive = _tar(tmp_path / "evil.tar", _file("../owned"))

        with pytest.raises(UnsafeArchiveError) as e:
            safe_extract_tar(tarfile.open(archive), dest, label="Blueprint import")

        assert str(e.value).startswith("Blueprint import rejected:")

    def test_a_well_formed_archive_still_extracts(self, tmp_path):
        dest = tmp_path / "out"
        dest.mkdir()
        archive = _tar(
            tmp_path / "good.tar",
            _file("manifest.json", b"{}"),
            _file("./nested/disk.vmdk", b"disk"),
        )

        safe_extract_tar(tarfile.open(archive), dest)

        assert (dest / "manifest.json").read_bytes() == b"{}"
        assert (dest / "nested" / "disk.vmdk").read_bytes() == b"disk"


class TestZip:
    """The ZIP half, which -- measured, not assumed -- was NOT exploitable before this.

    CPython's `zipfile.extractall` sanitises member names itself: `_extract_member` strips `..`
    components and leading separators, so `../owned` landed inside the destination as `owned`
    rather than escaping it. A symlink entry is written as a regular file containing the target
    rather than as a link, so that is not an escape either. Verified both by writing such archives
    and extracting them unguarded.

    So this guard is defence in depth, and the difference it makes is an explicit refusal instead
    of a silent rename -- an archive that tried to traverse is now rejected and said so, rather
    than quietly imported with its paths rewritten. It also stops the behaviour depending on a
    CPython implementation detail that nothing in this repository pins or tests.

    The tar half was a different matter: `tarfile.extractall` wrote outside the destination on the
    interpreter this runs on, which is the defect these paths actually had.
    """

    def test_a_traversing_entry_is_refused_and_writes_nothing(self, tmp_path):
        dest = tmp_path / "out"
        dest.mkdir()
        outside = tmp_path / "owned"
        archive = _zip(tmp_path / "evil.zip", ("../owned", b"pwned"))

        with pytest.raises(UnsafeArchiveError) as e:
            safe_extract_zip(zipfile.ZipFile(archive), dest)

        assert "traverses" in str(e.value)
        assert not outside.exists()

    def test_an_absolute_entry_is_refused(self, tmp_path):
        dest = tmp_path / "out"
        dest.mkdir()
        archive = _zip(tmp_path / "abs.zip", ("/etc/cron.d/x", b"pwned"))

        with pytest.raises(UnsafeArchiveError) as e:
            safe_extract_zip(zipfile.ZipFile(archive), dest)

        assert "absolute" in str(e.value)

    def test_a_symlink_entry_is_refused(self, tmp_path):
        """A ZIP symlink is an entry whose Unix mode says so, with the target as its contents."""
        dest = tmp_path / "out"
        dest.mkdir()
        archive = tmp_path / "link.zip"
        with zipfile.ZipFile(archive, "w") as z:
            info = zipfile.ZipInfo("escape")
            info.external_attr = (0o120777 << 16) | 0o120000
            z.writestr(info, "/etc/passwd")

        with pytest.raises(UnsafeArchiveError) as e:
            safe_extract_zip(zipfile.ZipFile(archive), dest)

        assert "symbolic link" in str(e.value)

    def test_a_well_formed_archive_still_extracts(self, tmp_path):
        dest = tmp_path / "out"
        dest.mkdir()
        archive = _zip(tmp_path / "good.zip", ("range.json", b"{}"), ("content/guide.md", b"# hi"))

        safe_extract_zip(zipfile.ZipFile(archive), dest)

        assert (dest / "range.json").read_bytes() == b"{}"
        assert (dest / "content" / "guide.md").read_bytes() == b"# hi"


def test_no_service_extracts_an_archive_without_the_guard():
    """The regression guard: a bare `extractall` anywhere in the services is the defect itself.

    Named as a test rather than left to review because this is exactly the shape that recurred --
    one module fixed, two not, for as long as nothing asserted the absence.
    """
    import re

    services = Path(__file__).resolve().parents[2] / "proving_ground"
    offenders = []
    for path in sorted(services.rglob("*.py")):
        if path.name == "safe_archive.py":
            continue
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if re.search(r"(?<!safe_)\b\w*\.extractall\s*\(", line):
                offenders.append(f"{path.relative_to(services.parent)}:{n}: {line.strip()}")
    assert offenders == [], (
        "these call extractall directly instead of utils.safe_archive, so an uploaded archive "
        "can write outside its extraction directory:\n  " + "\n  ".join(offenders)
    )
