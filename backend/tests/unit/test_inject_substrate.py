"""What an inject and an artifact placement do on each substrate -- MSEL-055.

One bar for both halves, and it is not "does it work": it is that nothing reports success it did
not earn. An inject fires during a live exercise and its status is the record of what the
exercise did, so a COMPLETED inject that placed nothing corrupts that record in a way nobody
notices until somebody reads it back.

Two defects are pinned here. `place_file` logged "Would place ..." and returned
``{"placed": True}`` on every substrate. And inject execution and artifact placement both resolve
their target through ``VM.container_id``, which a KubeVirt machine does not have -- on Kubernetes
they must refuse by name rather than fail on a missing container, which reads as a machine
somebody forgot to switch on.
"""

from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from proving_ground.api import artifacts as artifacts_api
from proving_ground.api import kubernetes_ranges
from proving_ground.models.artifact import PlacementStatus
from proving_ground.models.inject import InjectStatus
from proving_ground.services.inject_service import InjectService

RUN_COMMAND = {
    "action_type": "run_command",
    "parameters": {"target_vm": "workstation-1", "command": "echo hello"},
}
PLACE_FILE = {
    "action_type": "place_file",
    "parameters": {
        "target_vm": "workstation-1",
        "filename": "brief.pdf",
        "target_path": "/tmp/brief.pdf",
    },
}


@pytest.fixture
def on_kubernetes(monkeypatch):
    monkeypatch.setattr(kubernetes_ranges, "is_kubernetes", lambda: True)


@pytest.fixture
def on_docker(monkeypatch):
    monkeypatch.setattr(kubernetes_ranges, "is_kubernetes", lambda: False)


def make_inject(actions):
    inject = MagicMock()
    inject.id = uuid4()
    inject.status = InjectStatus.PENDING
    inject.actions = actions
    return inject


def a_running_machine():
    vm = MagicMock()
    vm.hostname = "workstation-1"
    vm.container_id = "c0ffee"
    return vm


# ----------------------------------------------------------------- inject execution


@pytest.mark.parametrize("actions", [[RUN_COMMAND], [PLACE_FILE], [RUN_COMMAND, PLACE_FILE]])
def test_kubernetes_inject_refuses_and_names_the_substrate(actions):
    docker = MagicMock()
    inject = make_inject(actions)

    result = InjectService(MagicMock(), docker, substrate="kubernetes").execute_inject(
        inject, {"workstation-1": a_running_machine()}
    )

    assert result["success"] is False
    assert inject.status == InjectStatus.FAILED
    reason = str(result["results"])
    assert "Kubernetes" in reason
    assert "nothing was placed" in reason.lower()
    # The refusal has to survive in the exercise record, not only in the response body.
    assert "Kubernetes" in inject.execution_log
    # Refused, not attempted: the Docker path must not be reached at all.
    docker.exec_command.assert_not_called()


def test_kubernetes_inject_never_reports_success_even_with_no_actions():
    """An empty action list used to walk the loop zero times and close COMPLETED."""
    inject = make_inject([])

    result = InjectService(MagicMock(), MagicMock(), substrate="kubernetes").execute_inject(
        inject, {}
    )

    assert result["success"] is False
    assert inject.status == InjectStatus.FAILED


def test_docker_run_command_still_executes():
    """Era A is frozen but shipped -- a command inject keeps working exactly as it did."""
    docker = MagicMock()
    docker.exec_command.return_value = (0, "hello")
    inject = make_inject([RUN_COMMAND])

    result = InjectService(MagicMock(), docker, substrate="dind").execute_inject(
        inject, {"workstation-1": a_running_machine()}
    )

    assert result["success"] is True
    assert inject.status == InjectStatus.COMPLETED
    docker.exec_command.assert_called_once_with("c0ffee", "echo hello")


def test_docker_place_file_refuses_instead_of_claiming_success():
    inject = make_inject([PLACE_FILE])

    result = InjectService(MagicMock(), MagicMock(), substrate="dind").execute_inject(
        inject, {"workstation-1": a_running_machine()}
    )

    assert result["success"] is False
    assert inject.status == InjectStatus.FAILED
    reason = str(result["results"])
    assert "Nothing was placed" in reason
    # The one path that does copy a file is named, so the refusal leaves somewhere to go.
    assert "artifact placement" in reason


def test_docker_place_file_does_not_report_placed():
    """The exact shape of the old lie: a `placed: True` with no copy behind it."""
    inject = make_inject([PLACE_FILE])

    result = InjectService(MagicMock(), MagicMock(), substrate="dind").execute_inject(
        inject, {"workstation-1": a_running_machine()}
    )

    assert "'placed': True" not in str(result["results"])


def test_a_command_inject_fails_whole_when_one_action_cannot_run():
    """A mixed inject must not close COMPLETED on the strength of its command half."""
    docker = MagicMock()
    docker.exec_command.return_value = (0, "hello")
    inject = make_inject([RUN_COMMAND, PLACE_FILE])

    result = InjectService(MagicMock(), docker, substrate="dind").execute_inject(
        inject, {"workstation-1": a_running_machine()}
    )

    assert result["success"] is False
    assert inject.status == InjectStatus.FAILED


def test_substrate_defaults_to_the_host_setting(monkeypatch):
    """A caller that passes nothing must get this host's substrate, not a guess."""
    import proving_ground.services.inject_service as inject_service

    settings = MagicMock()
    settings.range_substrate = "kubernetes"
    monkeypatch.setattr(inject_service, "get_settings", lambda: settings)

    assert InjectService(MagicMock(), MagicMock()).substrate == "kubernetes"


# -------------------------------------------------------------- artifact placement


def test_execute_placement_refuses_on_kubernetes(on_kubernetes, monkeypatch):
    def no_docker():
        raise AssertionError("a refused placement must not reach for a Docker daemon")

    monkeypatch.setattr(artifacts_api, "get_docker_service", no_docker)

    with pytest.raises(HTTPException) as caught:
        artifacts_api.execute_placement(uuid4(), MagicMock(), MagicMock())

    assert caught.value.status_code == 409
    assert "Kubernetes" in caught.value.detail
    assert "nothing was placed" in caught.value.detail.lower()


def test_execute_placement_leaves_a_refused_placement_pending(on_kubernetes, monkeypatch):
    """Nothing was attempted, so nothing may be recorded as having failed."""

    def not_reached():
        raise AssertionError("a refused placement must not read the artifact out of storage")

    monkeypatch.setattr(artifacts_api, "get_storage_service", not_reached)

    db = MagicMock()
    placement = MagicMock()
    # PENDING explicitly. A placement whose status is a bare MagicMock trips the "cannot execute
    # in <status>" guard first, and this test would then pass on a build that has no substrate
    # gate at all -- proving the endpoint refuses something, but not that it refuses this.
    placement.status = PlacementStatus.PENDING
    db.query.return_value.filter.return_value.first.return_value = placement

    with pytest.raises(HTTPException):
        artifacts_api.execute_placement(uuid4(), db, MagicMock())

    assert not db.commit.called
    assert placement.status is PlacementStatus.PENDING


def test_create_placement_refuses_on_kubernetes(on_kubernetes):
    """Staging a placement that could never execute is a promise the substrate cannot keep."""
    with pytest.raises(HTTPException) as caught:
        artifacts_api.create_placement(MagicMock(), MagicMock(), MagicMock())

    assert caught.value.status_code == 409
    assert "Kubernetes" in caught.value.detail


def test_placement_substrate_gate_is_off_on_docker(on_docker):
    """On Era A the gate must be invisible: a missing placement is still a plain 404."""
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None

    with pytest.raises(HTTPException) as caught:
        artifacts_api.execute_placement(uuid4(), db, MagicMock())

    assert caught.value.status_code == 404
