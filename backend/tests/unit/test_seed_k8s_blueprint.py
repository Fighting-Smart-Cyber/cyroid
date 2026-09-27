"""The sample blueprint the in-cluster installer offers must itself be deployable."""

import uuid

from proving_ground.capability.blueprint import read_blueprint
from proving_ground.capability import placement_for_assignment
from proving_ground.models.blueprint import RangeBlueprint
from proving_ground.models.user import User, UserRole
from proving_ground.tools import seed_k8s_blueprint as seed


def test_the_sample_is_a_v2_blueprint_that_places_without_a_learner():
    spec = read_blueprint(seed.CONFIG)
    assert spec.deployable_on_kubernetes
    assert [w.name for w in spec.workloads] == ["web"]
    assert {n.name for n in spec.networks} == {"dmz", "internal"}
    # An instructor deploying it from the UI has no learner assigned; a per-learner scope would
    # be refused at deploy time, so the sample must not declare one.
    placement = placement_for_assignment(
        range_id=uuid.uuid4(),
        training_event_id=None,
        assigned_to_user_id=None,
        capabilities=list(spec.capabilities),
    )
    assert placement.namespace.startswith("pg-range-")


def test_every_image_in_the_sample_is_pinned_by_digest():
    text = str(seed.CONFIG)
    for ref in (seed.BUSYBOX, seed.CIRROS):
        assert "@sha256:" in ref and ref in text


def test_seeding_is_idempotent(db_session, monkeypatch):
    admin = User(
        username="root",
        email="root@x.invalid",
        hashed_password="x",
        role=UserRole.ADMIN,
        is_active=True,
        is_approved=True,
    )
    db_session.add(admin)
    db_session.commit()
    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(seed, "get_session_local", lambda: lambda: db_session)

    assert seed.main() == 0
    assert seed.main() == 0
    rows = db_session.query(RangeBlueprint).filter(RangeBlueprint.name == seed.NAME).all()
    assert len(rows) == 1 and rows[0].version == 2 and rows[0].created_by == admin.id


def test_seeding_without_an_admin_refuses(db_session, monkeypatch):
    monkeypatch.setattr(db_session, "close", lambda: None)
    monkeypatch.setattr(seed, "get_session_local", lambda: lambda: db_session)
    assert seed.main() == 1
