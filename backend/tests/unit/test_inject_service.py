# backend/tests/unit/test_inject_service.py
from unittest.mock import MagicMock
from uuid import uuid4
from proving_ground.services.inject_service import InjectService
from proving_ground.models.inject import InjectStatus


def test_execute_inject_runs_command():
    mock_db = MagicMock()
    mock_docker = MagicMock()
    mock_docker.exec_command.return_value = (0, "command output")

    service = InjectService(mock_db, mock_docker)

    inject = MagicMock()
    inject.id = uuid4()
    inject.actions = [
        {
            "action_type": "run_command",
            "parameters": {"target_vm": "test-vm", "command": "echo hello"},
        }
    ]

    vm = MagicMock()
    vm.container_id = "test-container"
    vm.hostname = "test-vm"

    result = service.execute_inject(inject, {"test-vm": vm})

    assert result["success"] == True
    mock_docker.exec_command.assert_called_once()


def test_execute_inject_place_file():
    mock_db = MagicMock()
    mock_docker = MagicMock()

    service = InjectService(mock_db, mock_docker)

    inject = MagicMock()
    inject.id = uuid4()
    inject.actions = [
        {
            "action_type": "place_file",
            "parameters": {
                "target_vm": "test-vm",
                "filename": "test.exe",
                "target_path": "/tmp/test.exe",
            },
        }
    ]

    vm = MagicMock()
    vm.container_id = "test-container"
    vm.hostname = "test-vm"

    result = service.execute_inject(inject, {"test-vm": vm})

    # A place_file action refuses. Nothing in this path ever copied a file, on either
    # substrate, and the inject reported success anyway -- an exercise controller fired an
    # inject during a live exercise, was told it landed, and nothing had been placed. The
    # refusal is what puts the inject into FAILED, which is what the controller has to see.
    assert result["success"] is False
    assert len(result["results"]) == 1
    assert "error" in result["results"][0]["result"]


def test_execute_inject_vm_not_found():
    mock_db = MagicMock()
    mock_docker = MagicMock()

    service = InjectService(mock_db, mock_docker)

    inject = MagicMock()
    inject.id = uuid4()
    inject.actions = [
        {
            "action_type": "run_command",
            "parameters": {"target_vm": "nonexistent", "command": "echo hello"},
        }
    ]

    result = service.execute_inject(inject, {})

    assert result["success"] == False
    assert "error" in str(result["results"][0])


def test_execute_inject_updates_status():
    mock_db = MagicMock()
    mock_docker = MagicMock()
    mock_docker.exec_command.return_value = (0, "success")

    service = InjectService(mock_db, mock_docker)

    inject = MagicMock()
    inject.id = uuid4()
    inject.status = InjectStatus.PENDING
    inject.actions = [
        {
            "action_type": "run_command",
            "parameters": {"target_vm": "test-vm", "command": "echo hello"},
        }
    ]

    vm = MagicMock()
    vm.container_id = "test-container"

    service.execute_inject(inject, {"test-vm": vm})

    # Check that status was updated to COMPLETED
    assert inject.status == InjectStatus.COMPLETED
    assert mock_db.commit.called


def test_execute_inject_handles_command_failure():
    mock_db = MagicMock()
    mock_docker = MagicMock()
    mock_docker.exec_command.side_effect = Exception("Docker error")

    service = InjectService(mock_db, mock_docker)

    inject = MagicMock()
    inject.id = uuid4()
    inject.actions = [
        {
            "action_type": "run_command",
            "parameters": {"target_vm": "test-vm", "command": "echo hello"},
        }
    ]

    vm = MagicMock()
    vm.container_id = "test-container"

    result = service.execute_inject(inject, {"test-vm": vm})

    assert result["success"] == False
    assert inject.status == InjectStatus.FAILED
