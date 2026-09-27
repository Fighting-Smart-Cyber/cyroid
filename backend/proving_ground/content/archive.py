"""Packing a content bundle for transport (PG-104).

The bundle's real home is a directory in git. This is how it travels when it
cannot be a directory -- an HTTP download, or a file dropped into an air-gapped
transfer -- and it is deterministic for the same reason the bundle is: two
packs of unchanged content must be the same bytes, or checksums recorded on one
side of an air gap mean nothing on the other.

tar and gzip both want to record the current time. Both are told not to.
"""

from __future__ import annotations

import gzip
import io
import posixpath
import tarfile
from typing import Dict

# Anything a tar member can be that is not a plain file inside the bundle.
# tarfile will happily write outside the destination if asked; it is asked
# surprisingly often.
MAX_MEMBERS = 10_000
MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024


class ArchiveError(ValueError):
    """The archive is not one we are willing to read."""


def pack(files: Dict[str, bytes], root: str) -> bytes:
    """Pack bundle files into a deterministic tar.gz.

    Args:
        files: path -> bytes, relative to the bundle root.
        root: directory name the bundle unpacks into.
    """
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w", format=tarfile.PAX_FORMAT) as tar:
        for name in sorted(files):
            payload = files[name]
            info = tarfile.TarInfo(posixpath.join(root, name))
            info.size = len(payload)
            # Every field that would otherwise carry the current time, this
            # machine's user, or this machine's umask.
            info.mtime = 0
            info.mode = 0o644
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            tar.addfile(info, io.BytesIO(payload))

    packed = io.BytesIO()
    # mtime=0, or gzip stamps the current time into the header.
    with gzip.GzipFile(fileobj=packed, mode="wb", mtime=0) as gz:
        gz.write(raw.getvalue())
    return packed.getvalue()


def unpack(data: bytes) -> Dict[str, bytes]:
    """Read a bundle back out of a tar.gz, refusing anything unsafe.

    Returns:
        path -> bytes, relative to the bundle root, with the root stripped.

    Raises:
        ArchiveError: on an unreadable archive, a member that is not a regular
            file, a path that escapes the bundle root, or an archive large
            enough to be a decompression bomb.
    """
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            members = tar.getmembers()
            if len(members) > MAX_MEMBERS:
                raise ArchiveError(f"archive has more than {MAX_MEMBERS} entries")

            total = 0
            out: Dict[str, bytes] = {}
            for member in members:
                if member.isdir():
                    continue
                if not member.isfile():
                    raise ArchiveError(
                        f"'{member.name}' is not a regular file; bundles contain only files"
                    )
                _check_path(member.name)

                total += member.size
                if total > MAX_UNCOMPRESSED_BYTES:
                    raise ArchiveError("archive expands to more than 256 MB")

                handle = tar.extractfile(member)
                if handle is None:
                    raise ArchiveError(f"'{member.name}' could not be read")
                out[_strip_root(member.name)] = handle.read()
    except tarfile.TarError as exc:
        raise ArchiveError(f"not a readable tar.gz archive: {exc}") from exc

    if not out:
        raise ArchiveError("archive is empty")
    return out


def _check_path(name: str) -> None:
    """Refuse absolute paths and anything that climbs out of the bundle."""
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise ArchiveError(f"'{name}' is an absolute path")
    normalised = posixpath.normpath(name)
    if normalised.startswith("../") or normalised == "..":
        raise ArchiveError(f"'{name}' escapes the bundle root")


def _strip_root(name: str) -> str:
    """Drop the single leading directory the bundle packs into."""
    normalised = posixpath.normpath(name)
    head, _, tail = normalised.partition("/")
    return tail if tail else head
