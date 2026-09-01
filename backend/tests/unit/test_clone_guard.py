"""The clone must not mistake leftover scratch files for a learner's disk.

Observed live: a failed deploy left a tmp/ directory in the VM's storage. The
next deploy saw a non-empty directory, decided the VM already had a disk, and
skipped the clone. The VM booted an empty /storage, dockur fell through to
downloading Windows from Microsoft, and on an isolated range that hangs -- with
nothing in the VM's own logs to explain it.

Skipping the clone protects a learner's work, so the check has to be "is there
a disk here", not "is this directory non-empty".
"""

import inspect

from proving_ground.services.range_deployment_service import _has_usable_disk


def test_an_empty_directory_is_not_a_disk(tmp_path):
    d = tmp_path / "storage"
    d.mkdir()
    assert _has_usable_disk(str(d)) is False


def test_a_missing_directory_is_not_a_disk(tmp_path):
    assert _has_usable_disk(str(tmp_path / "nope")) is False


def test_dockur_scratch_is_not_a_disk(tmp_path):
    """The exact shape that caused the silent failure."""
    d = tmp_path / "storage"
    (d / "tmp").mkdir(parents=True)
    assert _has_usable_disk(str(d)) is False, (
        "A leftover tmp/ was treated as an existing disk, so the clone was "
        "skipped and the VM booted with nothing to boot from."
    )


def test_a_zero_length_image_is_not_a_disk(tmp_path):
    d = tmp_path / "storage"
    d.mkdir()
    (d / "data.img").touch()
    assert (
        _has_usable_disk(str(d)) is False
    ), "A truncated or half-written image must be re-cloned, not preserved."


def test_a_real_disk_is_preserved(tmp_path):
    d = tmp_path / "storage"
    d.mkdir()
    (d / "data.img").write_bytes(b"\0" * 4096)
    assert (
        _has_usable_disk(str(d)) is True
    ), "Re-deploying must not discard a VM disk a learner has been using."


def test_qcow2_counts_too(tmp_path):
    d = tmp_path / "storage"
    d.mkdir()
    (d / "disk.qcow2").write_bytes(b"x")
    assert _has_usable_disk(str(d)) is True


def test_both_clone_paths_use_the_check():
    from proving_ground.services import range_deployment_service

    src = inspect.getsource(range_deployment_service)
    assert src.count("_has_usable_disk(vm_storage)") == 2, (
        "Both the deploy and sync clone paths must decide the same way; "
        f"found {src.count('_has_usable_disk(vm_storage)')}."
    )
    assert "not os.listdir(vm_storage)" not in src, (
        "The non-emptiness check is back, so scratch files will again be " "mistaken for a disk."
    )
