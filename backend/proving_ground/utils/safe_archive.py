"""Extract an uploaded archive without letting it write outside the directory we chose.

Every archive this platform extracts arrives from whoever can reach an import endpoint, and the
extraction runs in the API container. A member named ``../../etc/cron.d/x``, or a symlink aimed at
the shared data volume, is therefore a write to anywhere the API can reach -- which is most of the
install.

`tarfile.extractall` performs that write. Measured on the interpreter this runs on, a member named
``../owned`` landed outside the destination directory, so the unguarded tar paths were exploitable
and are the reason this module exists. `zipfile.extractall` does not: CPython sanitises member
names itself, stripping ``..`` and leading separators, so the same archive was silently imported
with its paths rewritten rather than escaping. The ZIP half of this module is therefore defence in
depth -- an explicit refusal in place of a silent rename, and one that does not depend on a CPython
implementation detail nothing here pins.

`image_import_service` already got this right for OVA uploads, and its guard is the one below --
moved here rather than rewritten, because the version that was already reviewed is the version
worth keeping, and because the alternative is what this module exists to end: the same rule
implemented twice and fixed once. CLAUDE.md records what that costs for access checks; it costs
the same here.

The rules, applied to every member before a single byte is written:

* no links, symbolic or hard -- a link is how a write is redirected after the path check passes
* nothing that is not a regular file or a directory -- no devices, no FIFOs
* no absolute paths
* no ``..`` component
* and, after resolving symlinks, the target must still be inside the destination

`tarfile`'s own ``data`` filter refuses much of the same and is applied as well where the
interpreter has it. It is not relied on alone: it arrived in 3.11.4, becomes the default only in
3.14, and its rules have moved between point releases -- so the answer must not depend on which
interpreter this lands on. `zipfile` has no equivalent filter at all, which is why the checks here
are the whole defence for a ZIP.
"""

from __future__ import annotations

import os
import tarfile
import zipfile
from pathlib import Path

__all__ = [
    "UnsafeArchiveError",
    "reject_unsafe_tar_members",
    "reject_unsafe_zip_members",
    "safe_extract_tar",
    "safe_extract_zip",
    "safe_extract",
]

# tarfile grew a "data" extraction filter in 3.11.4 and it becomes the default in 3.14; on anything
# older, passing filter= is a TypeError. Detected the way PEP 706 prescribes rather than by version
# number, so this keeps working on whichever interpreter the image lands on.
_HAS_DATA_FILTER = hasattr(tarfile, "data_filter")


class UnsafeArchiveError(ValueError):
    """An archive member would have been written outside the extraction directory.

    A `ValueError` subclass on purpose: the callers this was extracted from already handle
    `ValueError` as "this upload is bad input", and a new exception type would have turned a
    refusal into a 500 at each of them.
    """


def _check_name(name: str, root: str, label: str) -> None:
    if os.path.isabs(name) or name.startswith("/"):
        raise UnsafeArchiveError(f"{label} rejected: '{name}' is an absolute path")
    if ".." in Path(name).parts:
        raise UnsafeArchiveError(f"{label} rejected: '{name}' traverses out of the archive")
    target = os.path.realpath(os.path.join(root, name))
    if target != root and not target.startswith(root + os.sep):
        raise UnsafeArchiveError(
            f"{label} rejected: '{name}' resolves outside the extraction directory"
        )


def reject_unsafe_tar_members(
    tar: tarfile.TarFile, dest: Path | str, *, label: str = "Archive"
) -> None:
    """Raise if any member of `tar` would be written outside `dest`. Writes nothing.

    Separate from the extraction on purpose: the guarantee worth testing is that a bad archive is
    refused *before* a byte is written, and that is only assertable if the inspection can be called
    by itself.
    """
    root = os.path.realpath(dest)
    for member in tar.getmembers():
        if member.issym() or member.islnk():
            raise UnsafeArchiveError(
                f"{label} rejected: '{member.name}' is a link, which can redirect a "
                f"write outside the archive"
            )
        if not (member.isfile() or member.isdir()):
            raise UnsafeArchiveError(
                f"{label} rejected: '{member.name}' is neither a regular file nor a directory"
            )
        _check_name(member.name, root, label)


def safe_extract_tar(tar: tarfile.TarFile, dest: Path | str, *, label: str = "Archive") -> None:
    """Extract a tar archive, refusing it entirely if any member would escape `dest`.

    Inspected in full first, so a rejected archive leaves no partial extraction behind for a caller
    to mistake for a good one.
    """
    reject_unsafe_tar_members(tar, dest, label=label)
    if _HAS_DATA_FILTER:
        tar.extractall(dest, filter="data")
    else:
        tar.extractall(dest)


def reject_unsafe_zip_members(
    zf: zipfile.ZipFile, dest: Path | str, *, label: str = "Archive"
) -> None:
    """Raise if any member of `zf` would be written outside `dest`. Writes nothing.

    A ZIP entry carries a Unix mode in its external attributes, and a symlink is an entry whose
    mode says so with the link target as its contents. `zipfile.extractall` writes that as a
    regular file rather than creating the link, so it is not the escape it is in a tar -- but it is
    refused anyway. Nothing this platform imports has any business containing one, and the
    alternative is depending on that implementation detail staying true.

    Same reasoning as the traversal check above: the value here is saying no out loud.
    """
    root = os.path.realpath(dest)
    for info in zf.infolist():
        mode = info.external_attr >> 16
        if mode and (mode & 0o170000) == 0o120000:
            raise UnsafeArchiveError(f"{label} rejected: '{info.filename}' is a symbolic link")
        _check_name(info.filename, root, label)


def safe_extract_zip(zf: zipfile.ZipFile, dest: Path | str, *, label: str = "Archive") -> None:
    """Extract a zip archive, refusing it entirely if any member would escape `dest`."""
    reject_unsafe_zip_members(zf, dest, label=label)
    zf.extractall(dest)


def safe_extract(archive, dest: Path | str, *, label: str = "Archive") -> None:
    """Dispatch on the open archive's type, for callers that handle both formats in one branch."""
    if isinstance(archive, tarfile.TarFile):
        safe_extract_tar(archive, dest, label=label)
    elif isinstance(archive, zipfile.ZipFile):
        safe_extract_zip(archive, dest, label=label)
    else:
        raise TypeError(f"not an archive this can extract: {type(archive).__name__}")
