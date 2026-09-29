# tests/unit/test_live_vm_resources.py
"""Unit tests for live CPU/memory updates on a running container.

These pin the shape of the request sent to the Docker engine. The behaviour was
verified against a real daemon (Engine v1.55) before it was written down here:

  - container.update(cpu_quota=...) on a NanoCpus container returns
    409 Conflict ("Nano CPUs and CPU Period cannot both be set"), which is why
    the raw engine API is used instead of the SDK's update_container()
  - the raw NanoCpus update returns 200 and survives a container restart
  - the daemon enforces no floor: a NanoCpus of 1000 is accepted

That last point is why VMResourceUpdate carries the bounds - nothing downstream
will catch an absurd value.
"""

import pytest
from unittest.mock import MagicMock, patch


class TestUpdateContainerResources:
    """The request sent to the engine, and how failures come back."""

    @patch("docker.from_env")
    def test_cpu_is_sent_as_nano_cpus(self, mock_docker):
        from proving_ground.services.docker_service import DockerService

        mock_client = MagicMock()
        mock_docker.return_value = mock_client

        service = DockerService()
        applied = service.update_container_resources("abc123", cpu_limit=4)

        _, kwargs = mock_client.api._post_json.call_args
        assert kwargs["data"]["NanoCpus"] == 4_000_000_000
        # CPU-only must not disturb memory.
        assert "Memory" not in kwargs["data"]
        assert applied == {"cpu": 4}

    @patch("docker.from_env")
    def test_memory_is_sent_with_double_swap(self, mock_docker):
        from proving_ground.services.docker_service import DockerService

        mock_client = MagicMock()
        mock_docker.return_value = mock_client

        service = DockerService()
        applied = service.update_container_resources("abc123", memory_limit_mb=2048)

        _, kwargs = mock_client.api._post_json.call_args
        data = kwargs["data"]
        assert data["Memory"] == 2048 * 1024 * 1024
        # MemorySwap must be twice Memory - the daemon's own create-time default.
        # Omitting it makes any raise past the old swap ceiling fail.
        assert data["MemorySwap"] == 2 * data["Memory"]
        # Memory-only must not disturb CPU.
        assert "NanoCpus" not in data
        assert applied == {"ram_mb": 2048}

    @patch("docker.from_env")
    def test_no_fields_makes_no_request(self, mock_docker):
        from proving_ground.services.docker_service import DockerService

        mock_client = MagicMock()
        mock_docker.return_value = mock_client

        service = DockerService()
        assert service.update_container_resources("abc123") == {}
        mock_client.api._post_json.assert_not_called()

    @patch("docker.from_env")
    def test_dind_targets_the_range_daemon(self, mock_docker):
        """A range's containers live inside its own DinD, not on the host."""
        from proving_ground.services.docker_service import DockerService

        mock_client = MagicMock()
        mock_docker.return_value = mock_client
        range_client = MagicMock()

        service = DockerService()
        service.get_range_client_sync = MagicMock(return_value=range_client)

        service.update_container_resources(
            "abc123", cpu_limit=2, range_id="r-1", docker_url="tcp://10.0.0.5:2375"
        )

        service.get_range_client_sync.assert_called_once_with("r-1", "tcp://10.0.0.5:2375")
        range_client.api._post_json.assert_called_once()
        # The host daemon must not have been touched.
        mock_client.api._post_json.assert_not_called()

    @patch("docker.from_env")
    def test_missing_container_raises_domain_error(self, mock_docker):
        """Translated at the service boundary so api/ need not import docker."""
        from docker.errors import NotFound
        from proving_ground.services.docker_service import (
            ContainerNotFoundError,
            DockerService,
        )

        mock_client = MagicMock()
        mock_docker.return_value = mock_client
        mock_client.api._raise_for_status.side_effect = NotFound("no such container")

        service = DockerService()
        with pytest.raises(ContainerNotFoundError):
            service.update_container_resources("gone", cpu_limit=2)

    @patch("docker.from_env")
    def test_daemon_refusal_raises_domain_error(self, mock_docker):
        from docker.errors import APIError
        from proving_ground.services.docker_service import (
            DockerService,
            ResourceUpdateRejectedError,
        )

        mock_client = MagicMock()
        mock_docker.return_value = mock_client
        mock_client.api._raise_for_status.side_effect = APIError("cannot update")

        service = DockerService()
        with pytest.raises(ResourceUpdateRejectedError):
            service.update_container_resources("abc123", memory_limit_mb=1)


class TestVMResourceUpdateSchema:
    """The bounds live here because the daemon has none."""

    def test_rejects_empty_body(self):
        from pydantic import ValidationError
        from proving_ground.schemas.vm import VMResourceUpdate

        with pytest.raises(ValidationError):
            VMResourceUpdate()

    def test_accepts_one_field(self):
        from proving_ground.schemas.vm import VMResourceUpdate

        assert VMResourceUpdate(cpu=4).ram_mb is None
        assert VMResourceUpdate(ram_mb=2048).cpu is None

    @pytest.mark.parametrize("cpu", [0, -1, 33])
    def test_rejects_out_of_range_cpu(self, cpu):
        from pydantic import ValidationError
        from proving_ground.schemas.vm import VMResourceUpdate

        with pytest.raises(ValidationError):
            VMResourceUpdate(cpu=cpu)

    @pytest.mark.parametrize("ram_mb", [0, 511, 131073])
    def test_rejects_out_of_range_ram(self, ram_mb):
        from pydantic import ValidationError
        from proving_ground.schemas.vm import VMResourceUpdate

        with pytest.raises(ValidationError):
            VMResourceUpdate(ram_mb=ram_mb)
