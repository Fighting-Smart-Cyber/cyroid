# backend/tests/unit/test_catalog_install_api.py
"""The one-click install API surface and its progress contract (PG-149)."""

import json
from uuid import uuid4

import pytest
from fastapi import HTTPException

from proving_ground.api.catalog import get_install_plan
from proving_ground.models.catalog import CatalogSource, CatalogSourceType
from proving_ground.tasks import jobs as jobs_module
from proving_ground.tasks.jobs import COMPLETED, FAILED, JobLog, JobStore, RUNNING
from tests.unit.test_catalog_installer import build_catalog


@pytest.fixture
def source(db_session, tmp_path):
    root = build_catalog(tmp_path / "catalog")
    src = CatalogSource(name="Test", source_type=CatalogSourceType.LOCAL, url=str(root))
    db_session.add(src)
    db_session.commit()
    return src


def test_install_plan_endpoint_lists_the_work_without_doing_it(db_session, source):
    plan = get_install_plan(source.id, "red-team-lab", db_session, None)

    assert plan.item_id == "red-team-lab"
    assert [s.key for s in plan.steps] == [
        "base_image/ubuntu-22-04",
        "image/custom-web",
        "blueprint/red-team-lab",
    ]
    assert plan.total_steps == 3
    assert plan.summary == "3 step(s)"
    assert all(not s.satisfied for s in plan.steps)
    # Nothing was installed by asking.
    from proving_ground.models.blueprint import RangeBlueprint

    assert db_session.query(RangeBlueprint).count() == 0


def test_install_plan_for_an_unknown_item_is_a_404(db_session, source):
    with pytest.raises(HTTPException) as exc:
        get_install_plan(source.id, "nope", db_session, None)
    assert exc.value.status_code == 404


def test_install_plan_for_a_non_blueprint_is_a_404(db_session, source):
    with pytest.raises(HTTPException) as exc:
        get_install_plan(source.id, "ubuntu-22-04", db_session, None)
    assert exc.value.status_code == 404
    assert "only blueprints" in exc.value.detail


# --- the progress contract the modal polls -------------------------------


class FakeRedis:
    def __init__(self):
        self.data = {}

    def setex(self, key, ttl, value):
        self.data[key] = value

    def get(self, key):
        return self.data.get(key)

    def delete(self, key):
        self.data.pop(key, None)


@pytest.fixture
def store(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(JobStore, "_redis", lambda self: fake)
    return JobStore("test_install:")


def test_job_log_keeps_the_whole_story_not_just_the_last_line(store):
    """A modal that connects late must still see every stage."""
    log = JobLog(store, "job-1", total_steps=2)
    log.say("Resolving dependencies...")
    log.completed = 0
    log.say("Installing VM image: Ubuntu 22.04", level="step", current_item="Ubuntu 22.04")
    log.completed = 1
    log.say("Installing blueprint: Red Team Lab", level="step", current_item="Red Team Lab")

    status = store.get("job-1")
    assert [line["message"] for line in status["log"]] == [
        "Resolving dependencies...",
        "Installing VM image: Ubuntu 22.04",
        "Installing blueprint: Red Team Lab",
    ]
    assert status["status"] == RUNNING
    assert status["progress"] == 1 and status["total_steps"] == 2
    assert status["current_item"] == "Red Team Lab"


def test_a_failed_job_records_which_step_failed(store):
    log = JobLog(store, "job-2", total_steps=3)
    log.completed = 1
    log.say(
        "Building image: custom-web failed: docker build exited 1",
        status=FAILED,
        level="error",
        error="Building image: custom-web failed: docker build exited 1",
        result={"failed_step": "image/custom-web"},
    )

    status = store.get("job-2")
    assert status["status"] == FAILED
    assert status["result"]["failed_step"] == "image/custom-web"
    assert "docker build exited 1" in status["error"]
    assert status["progress"] == 1  # the step before it did finish


def test_completion_carries_the_blueprint_id(store):
    log = JobLog(store, "job-3", total_steps=1)
    log.completed = 1
    blueprint_id = str(uuid4())
    log.say("Install complete", status=COMPLETED, result={"blueprint_id": blueprint_id})

    status = store.get("job-3")
    assert status["status"] == COMPLETED
    assert status["result"]["blueprint_id"] == blueprint_id
    assert status["progress"] == status["total_steps"]


def test_only_unfinished_jobs_can_be_cancelled(store):
    log = JobLog(store, "job-4", total_steps=2)
    log.say("Installing...")
    assert store.cancel("job-4") is True
    assert store.is_cancelled("job-4") is True
    # Cancelling again, or cancelling a finished job, is refused.
    assert store.cancel("job-4") is False

    JobLog(store, "job-5", total_steps=1).say("Install complete", status=COMPLETED)
    assert store.cancel("job-5") is False


def test_cancelling_preserves_the_log_so_the_modal_keeps_its_history(store):
    log = JobLog(store, "job-6", total_steps=2)
    log.say("Installing VM image: Ubuntu 22.04", level="step")
    store.cancel("job-6")

    status = store.get("job-6")
    assert status["status"] == "cancelled"
    assert [line["message"] for line in status["log"]] == ["Installing VM image: Ubuntu 22.04"]


def test_an_unknown_job_reads_as_nothing(store):
    assert store.get("missing") is None
    assert store.is_cancelled("missing") is False
    assert store.cancel("missing") is False


def test_job_keys_are_namespaced_by_prefix(store, monkeypatch):
    """Two job families must not collide on the same id."""
    other = JobStore("other_install:")
    store.update("same-id", RUNNING, "one")
    other.update("same-id", RUNNING, "two")

    assert store.get("same-id")["step"] == "one"
    assert other.get("same-id")["step"] == "two"


def test_status_payload_is_json_serialisable(store):
    """It goes through Redis as JSON; a stray object would fail at runtime."""
    log = JobLog(store, "job-7", total_steps=1)
    log.say("Install complete", status=COMPLETED, result={"installed": ["blueprint/x"]})
    raw = store._redis().get(store.key("job-7"))
    assert json.loads(raw)["result"] == {"installed": ["blueprint/x"]}
