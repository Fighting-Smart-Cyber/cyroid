"""Capturing a golden image has to talk to the range's daemon, not the host's.

create_golden_image() resolves the container on the HOST client and then reads
the /storage mount straight off the host filesystem. Under DinD range isolation
neither holds -- the host daemon has never heard of the inner container, and the
mount source is a path inside the DinD. Capture fails outright.

Separately, copying the files is not sufficient on its own: the VM builder lists
golden images from the database, so an image with no GoldenImage row never shows
up as a boot source no matter what is on disk.
"""

import inspect

from proving_ground.services.docker_service import DockerService


def test_a_range_aware_capture_exists():
    assert hasattr(DockerService, "create_golden_image_from_range"), (
        "Without a range-aware capture there is no way to make a golden image "
        "from a VM in a DinD range, which is every VM."
    )


def test_range_capture_uses_the_range_daemon_not_the_host():
    src = inspect.getsource(DockerService.create_golden_image_from_range)
    assert "get_range_client_sync" in src, (
        "Capture must resolve the container on the range's own daemon; the host "
        "daemon cannot see containers inside a DinD."
    )
    assert (
        "self.client.containers.get" not in src
    ), "Capture is using the host client again -- that is the original bug."


def test_range_capture_streams_rather_than_pulling_a_helper_image():
    """A range network is isolated by default and cannot pull an image."""
    src = inspect.getsource(DockerService.create_golden_image_from_range)
    assert "get_archive" in src, (
        "Capture must stream the archive out of the container. Copying via a "
        "helper container needs an image the isolated range cannot pull."
    )


def test_range_capture_writes_into_template_storage():
    src = inspect.getsource(DockerService.create_golden_image_from_range)
    assert "template_storage_dir" in src, (
        "Golden images must land in template_storage_dir, which is where "
        "clone_from reads them back from."
    )


def test_capture_endpoint_registers_a_database_row():
    """Files on disk alone are invisible to the VM builder."""
    from proving_ground.api import cache

    src = inspect.getsource(cache.capture_golden_image_from_range)
    assert "GoldenImage(" in src, (
        "Capture must create a GoldenImage row. The builder lists golden images "
        "from the database, so a file-only capture is never selectable."
    )


def test_capture_request_targets_a_vm_not_a_raw_container_id():
    from proving_ground.api.cache import CaptureGoldenImageRequest

    fields = CaptureGoldenImageRequest.model_fields
    assert "vm_id" in fields, "Callers should name a VM, not hunt for a DinD container id"
    assert "name" in fields


class TestArchiveIsUntrusted:
    """The archive comes out of a range VM, which is hostile by construction.

    A member name or symlink target containing ".." would otherwise let a guest
    write anywhere the worker can reach (tar slip, CVE-2007-4559 class).
    """

    def test_traversal_and_links_are_rejected(self, tmp_path):
        import io
        import os
        import tarfile
        from unittest.mock import MagicMock, patch

        # An archive a malicious guest could produce: a normal file, a member
        # escaping via "..", and a symlink pointing at the filesystem root.
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            payload = b"x" * 16
            for name in ("storage/ok.bin", "storage/../../escaped.bin"):
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                tar.addfile(info, io.BytesIO(payload))
            link = tarfile.TarInfo("storage/evil-link")
            link.type = tarfile.SYMTYPE
            link.linkname = "../../../../etc"
            tar.addfile(link)
        buf.seek(0)

        outside = tmp_path / "escaped.bin"
        golden_root = tmp_path / "templates"
        golden_root.mkdir()

        container = MagicMock()
        container.attrs = {"Mounts": [{"Destination": "/storage"}]}
        container.status = "exited"
        container.get_archive.return_value = (iter([buf.read()]), {})

        svc = MagicMock()
        svc.get_range_client_sync.return_value.containers.get.return_value = container

        from proving_ground.services.docker_service import DockerService

        settings = MagicMock()
        settings.template_storage_dir = str(golden_root)
        with patch("proving_ground.services.docker_service.get_settings", return_value=settings):
            result = DockerService.create_golden_image_from_range(
                svc, "range-1", "tcp://x:2375", "abc123", "win-test"
            )

        assert not outside.exists(), (
            "A '..' member escaped the target directory -- this is the tar slip "
            "the extraction is supposed to prevent."
        )
        assert not (tmp_path / "etc").exists()
        assert not os.path.islink(os.path.join(result["path"], "evil-link")), (
            "A symlink pointing outside the target was kept; writes through it "
            "would land outside the golden image."
        )
        assert os.path.isfile(
            os.path.join(result["path"], "ok.bin")
        ), "The legitimate member should still be extracted."


class TestSparsenessSurvivesCapture:
    """A Windows disk is mostly holes, and tar does not know that.

    Docker's archive endpoint reads a sparse file's holes as zeros and tarfile
    writes them back as real blocks, so a 64 GiB disk occupying ~9 GiB in the
    guest lands as a fully allocated 64 GiB file. Every range cloned from that
    image inherits the full size, and the capacity model budgets for the
    allocated figure, not the apparent one.
    """

    def test_zero_regions_are_not_written_as_real_blocks(self, tmp_path):
        import io
        import os
        import tarfile
        from unittest.mock import MagicMock, patch

        zeros = b"\0" * (16 * 1024 * 1024)  # 16 MiB, entirely holes
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo("storage/data.img")
            info.size = len(zeros)
            tar.addfile(info, io.BytesIO(zeros))
        buf.seek(0)

        golden_root = tmp_path / "templates"
        golden_root.mkdir()

        container = MagicMock()
        container.attrs = {"Mounts": [{"Destination": "/storage"}]}
        container.status = "exited"
        container.get_archive.return_value = (iter([buf.read()]), {})

        svc = MagicMock()
        svc.get_range_client_sync.return_value.containers.get.return_value = container

        from proving_ground.services.docker_service import DockerService

        settings = MagicMock()
        settings.template_storage_dir = str(golden_root)
        with patch("proving_ground.services.docker_service.get_settings", return_value=settings):
            result = DockerService.create_golden_image_from_range(
                svc, "range-1", "tcp://x:2375", "abc123", "win-test"
            )

        img = os.path.join(result["path"], "data.img")
        st = os.stat(img)

        assert st.st_size == len(zeros), "The image must still be the full apparent size."
        allocated = st.st_blocks * 512
        assert allocated < len(zeros) // 10, (
            f"Holes were written as real blocks: {allocated} bytes allocated for a "
            f"{len(zeros)}-byte file of zeros. A 64 GiB Windows disk would land "
            f"fully allocated instead of ~9 GiB."
        )

    def test_reported_size_is_what_it_costs_on_disk(self, tmp_path):
        """size_bytes feeds the DB row and capacity planning, so it must be the
        allocated figure -- reporting apparent size would call a 9 GB image 64 GB."""
        import inspect

        from proving_ground.services.docker_service import DockerService

        src = inspect.getsource(DockerService.create_golden_image_from_range)
        assert "st_blocks" in src, (
            "size_bytes is being computed from apparent size; a sparse image "
            "would be reported at its full virtual size."
        )

    def test_cloning_a_golden_image_keeps_it_sparse(self, tmp_path):
        """Capture and clone are two separate inflation points.

        shutil.copy2 uses os.sendfile, which writes holes as real blocks, so a
        sparse golden image would still land fully allocated in every range
        cloned from it. Measured before the fix: a 64 MiB hole copied to 64 MiB
        of allocated blocks.
        """
        import os

        from proving_ground.services.docker_service import _copy_sparse, _copytree_sparse

        src_dir = tmp_path / "golden"
        src_dir.mkdir()
        img = src_dir / "data.img"
        with open(img, "wb") as f:
            f.truncate(32 * 1024 * 1024)  # 32 MiB, entirely holes
        assert os.stat(img).st_blocks * 512 == 0, "precondition: source is sparse"

        dst = tmp_path / "clone.img"
        _copy_sparse(str(img), str(dst))
        st = os.stat(dst)
        assert st.st_size == 32 * 1024 * 1024
        assert st.st_blocks * 512 < st.st_size // 10, (
            f"_copy_sparse allocated {st.st_blocks * 512} bytes for a file of "
            f"holes; every cloned range would pay the full apparent size."
        )

        tree_dst = tmp_path / "clonetree"
        _copytree_sparse(str(src_dir), str(tree_dst))
        st2 = os.stat(tree_dst / "data.img")
        assert st2.st_blocks * 512 < st2.st_size // 10, "_copytree_sparse lost the holes."

    def test_both_clone_sites_are_sparse_aware(self):
        """There are two clone call sites; fixing one silently leaves the other."""
        import inspect

        from proving_ground.services import docker_service

        src = inspect.getsource(docker_service)
        assert "shutil.copytree(src, dst)" not in src, (
            "A clone site still uses shutil.copytree, which inflates a sparse "
            "golden image to its full apparent size."
        )
        assert "shutil.copy2(src, dst)" not in src, "A clone site still uses shutil.copy2."
        assert "shutil.copytree(src, dst" not in src, (
            "A copy site still uses shutil.copytree, which inflates a sparse "
            "image to its full apparent size."
        )
        # Three sites inflate independently: the two range-clone paths (host
        # and DinD) and the legacy host-daemon capture. Fixing one silently
        # leaves the others, which is how the third was nearly missed.
        assert src.count("_copytree_sparse(src, dst)") == 3, (
            "Every disk-copy site must be sparse-aware; found "
            f"{src.count('_copytree_sparse(src, dst)')} of 3."
        )
