"""The team-exercise delivery mode, and the one role vocabulary an event has -- COSMOS PG-41.

`resolve_placement` has three outcomes and the architecture turns on all three, but only two were
reachable. `placement_for_assignment` produces TEAM_EXERCISE for a range that hangs off an event
with nobody assigned to it, and every path that created a range under an event assigned it to a
learner -- so the mode the platform was designed around had never once run. These tests pin the
path that makes it reachable, and pin self-paced unchanged beside it, because self-paced is what
runs today and the mode is a choice made at the same endpoint.

The role tests are the second half of the same coherence problem: the event roles and the
platform's own roles are different sets rendered in the same form, and the event role was a free
string on the way in. The briefing reads that string back to decide what a person may see.
"""

import pathlib
import re
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from proving_ground.api import kubernetes_ranges
from proving_ground.api import training_events as events_api
from proving_ground.capability import Delivery, Isolation, placement_for_assignment
from proving_ground.capability.blueprint import read_blueprint
from proving_ground.models.blueprint import RangeBlueprint, RangeInstance
from proving_ground.models.event import EventParticipant, EventStatus, TrainingEvent
from proving_ground.models.range import Range
from proving_ground.models.user import User, UserRole
from proving_ground.schemas.event import EventParticipantCreate, ParticipantRole

from .test_kubernetes_range_service import v2_config

PAGE = (
    pathlib.Path(__file__).resolve().parents[3]
    / "frontend"
    / "src"
    / "pages"
    / "TrainingEventDetail.tsx"
)


def participant_roles_block() -> str:
    """The one declaration of the event roles the form offers.

    Split on the assignment rather than the first `]`, because the type annotation carries a
    `[]` of its own and cutting there would compare against nothing.
    """
    source = PAGE.read_text(encoding="utf-8")
    return source.split("const PARTICIPANT_ROLES", 1)[1].split("= [", 1)[1].split("\n]", 1)[0]


@pytest.fixture(autouse=True)
def permitted_chart_repository(monkeypatch):
    """Permit the chart the fixture blueprint names, the way an operator would."""
    from proving_ground.config import get_settings

    monkeypatch.setattr(get_settings(), "chart_repository", "https://stefanprodan.github.io")


@pytest.fixture(autouse=True)
def on_kubernetes(monkeypatch):
    """Era B, so the placement the start produces is the thing under test."""
    monkeypatch.setattr(kubernetes_ranges, "is_kubernetes", lambda: True)


@pytest.fixture(autouse=True)
def queued(monkeypatch):
    """Capture the deploy dispatch. `.send()` would want a broker this suite does not have."""
    sent: list[str] = []
    monkeypatch.setattr(
        "proving_ground.tasks.deployment.deploy_range_task.send",
        lambda range_id: sent.append(range_id),
    )
    return sent


def cohort_config(scope="per-cohort"):
    """A blueprint whose capability a shared environment can satisfy.

    The suite's default capability is per-learner, which a team exercise refuses by design --
    there is no learner to slice it on -- and that refusal has a test of its own below.
    """
    config = v2_config()
    config["capabilities"][0]["scope"] = scope
    return config


@pytest.fixture
def user(db_session):
    def make(role=UserRole.ADMIN):
        tag = uuid.uuid4().hex[:8]
        u = User(
            username=f"u-{tag}",
            email=f"u-{tag}@x.invalid",
            hashed_password="x",
            role=role,
            is_active=True,
            is_approved=True,
        )
        db_session.add(u)
        db_session.flush()
        return u

    return make


@pytest.fixture
def admin(user):
    return user()


@pytest.fixture
def event(db_session, admin, user):
    """A scheduled event with a blueprint and a cohort of students, ready to be started."""

    def make(*, students=2, config=None):
        blueprint = RangeBlueprint(
            name=f"bp-{uuid.uuid4().hex[:6]}",
            version=1,
            config=config if config is not None else cohort_config(),
            created_by=admin.id,
        )
        db_session.add(blueprint)
        db_session.flush()

        ev = TrainingEvent(
            name=f"ev-{uuid.uuid4().hex[:6]}",
            start_datetime=datetime.now(timezone.utc),
            blueprint_id=blueprint.id,
            content_ids=[],
            allowed_roles=[],
            tags=[],
            created_by_id=admin.id,
            status=EventStatus.SCHEDULED,
        )
        db_session.add(ev)
        db_session.flush()

        learners = []
        for _ in range(students):
            learner = user(UserRole.STUDENT)
            db_session.add(
                EventParticipant(
                    event_id=ev.id,
                    user_id=learner.id,
                    role=ParticipantRole.STUDENT.value,
                    is_confirmed=True,
                )
            )
            learners.append(learner)
        db_session.commit()
        return ev, learners

    return make


def ranges_of(db_session, ev):
    return db_session.query(Range).filter(Range.training_event_id == ev.id).all()


def placement_of(db_session, range_obj):
    """Where this range would go, asked of the policy the deploy asks."""
    instance = db_session.query(RangeInstance).filter(RangeInstance.range_id == range_obj.id).one()
    spec = read_blueprint(instance.blueprint.config)
    return placement_for_assignment(
        range_id=range_obj.id,
        training_event_id=range_obj.training_event_id,
        assigned_to_user_id=range_obj.assigned_to_user_id,
        capabilities=list(spec.capabilities),
    )


class TestTeamExercise:
    def test_one_range_for_the_cohort_not_one_per_student(self, db_session, admin, event, queued):
        ev, _ = event(students=3)

        events_api.start_event(
            ev.id, db_session, admin, auto_deploy=True, delivery=Delivery.TEAM_EXERCISE
        )

        created = ranges_of(db_session, ev)
        assert len(created) == 1
        assert len(queued) == 1
        # Assigned to nobody: that absence is the declaration the placement policy reads.
        assert created[0].assigned_to_user_id is None

    def test_every_student_is_linked_to_the_one_range(self, db_session, admin, event):
        ev, learners = event(students=3)

        events_api.start_event(
            ev.id, db_session, admin, auto_deploy=True, delivery=Delivery.TEAM_EXERCISE
        )

        shared = ranges_of(db_session, ev)[0]
        linked = {
            p.range_id
            for p in db_session.query(EventParticipant)
            .filter(EventParticipant.event_id == ev.id)
            .all()
        }
        assert linked == {shared.id}
        # That link is also what entitles each of them to open it.
        from proving_ground.api.deps import get_student_accessible_range_ids

        for learner in learners:
            assert get_student_accessible_range_ids(learner.id, db_session) == [shared.id]

    def test_the_cohort_range_places_in_a_vcluster(self, db_session, admin, event):
        ev, _ = event()

        events_api.start_event(
            ev.id, db_session, admin, auto_deploy=True, delivery=Delivery.TEAM_EXERCISE
        )

        placement = placement_of(db_session, ranges_of(db_session, ev)[0])
        assert placement.isolation is Isolation.VCLUSTER
        assert placement.vcluster
        assert "team exercise" in placement.reason

    def test_the_event_reports_the_mode_it_was_started_in(self, db_session, admin, event):
        ev, _ = event()

        response = events_api.start_event(
            ev.id, db_session, admin, auto_deploy=True, delivery=Delivery.TEAM_EXERCISE
        )

        assert response["delivery"] is Delivery.TEAM_EXERCISE
        assert response["team_range_id"] == ranges_of(db_session, ev)[0].id

    def test_a_late_joining_student_joins_the_exercise_rather_than_a_copy_of_it(
        self, db_session, admin, event, user, queued
    ):
        ev, _ = event(students=1)
        events_api.start_event(
            ev.id, db_session, admin, auto_deploy=True, delivery=Delivery.TEAM_EXERCISE
        )
        shared = ranges_of(db_session, ev)[0]
        queued.clear()

        latecomer = user(UserRole.STUDENT)
        added = events_api.add_participant(
            ev.id,
            EventParticipantCreate(user_id=latecomer.id, role=ParticipantRole.STUDENT),
            db_session,
            admin,
        )

        assert added.range_id == shared.id
        assert len(ranges_of(db_session, ev)) == 1
        assert queued == [], "a second range for a team exercise is a second exercise"

    def test_ending_the_event_destroys_the_shared_range_once(
        self, db_session, admin, event, monkeypatch
    ):
        ev, _ = event(students=3)
        events_api.start_event(
            ev.id, db_session, admin, auto_deploy=True, delivery=Delivery.TEAM_EXERCISE
        )
        shared_id = ranges_of(db_session, ev)[0].id

        # The destroy itself is covered by test_kubernetes_teardown; what is under test here is
        # how many times it is asked for. Counting participants asked three times and then found
        # the row it had already dropped, which reads as the cluster refusing to release it.
        destroyed: list[uuid.UUID] = []
        monkeypatch.setattr(
            kubernetes_ranges,
            "destroy_for_cleanup",
            lambda db, range_obj: destroyed.append(range_obj.id),
        )

        deleted = events_api._delete_event_ranges(ev.id, db_session)

        assert destroyed == [shared_id]
        assert deleted == 1
        # Flushed because the caller commits, not this function: the row deletion is pending in
        # the same transaction the endpoint would commit on its way out.
        db_session.flush()
        assert db_session.query(Range).filter(Range.id == shared_id).first() is None

    def test_a_per_learner_blueprint_is_refused_with_the_reason(self, db_session, admin, event):
        ev, _ = event(config=cohort_config("per-learner"))

        with pytest.raises(HTTPException) as refused:
            events_api.start_event(
                ev.id, db_session, admin, auto_deploy=True, delivery=Delivery.TEAM_EXERCISE
            )

        assert refused.value.status_code == 400
        assert "per-learner" in refused.value.detail
        # The rows the refusal took with it. The request's session is rolled back on the way out,
        # which is what makes "nothing was created" true rather than merely uncommitted.
        db_session.rollback()
        assert ranges_of(db_session, ev) == []

    def test_a_team_exercise_with_nothing_to_deploy_is_refused_not_downgraded(
        self, db_session, admin, event
    ):
        ev, _ = event()

        with pytest.raises(HTTPException) as refused:
            events_api.start_event(
                ev.id, db_session, admin, auto_deploy=False, delivery=Delivery.TEAM_EXERCISE
            )

        assert refused.value.status_code == 400
        db_session.expire_all()
        assert ev.status is EventStatus.SCHEDULED


class TestALostLearnerIsNotADeliveryMode:
    """`Range.assigned_to_user_id` is ondelete="SET NULL", so "assigned to nobody" has a second
    cause: not "the cohort shares this" but "the learner this belonged to is gone". Deriving the
    mode from the unassigned test alone could not tell them apart.
    """

    @staticmethod
    def orphan(db_session, ev, learner):
        """A learner leaves the class and their account is deleted, which is what SET NULL does."""
        events_api.remove_participant(ev.id, learner.id, db_session, db_session.merge(learner))
        theirs = db_session.query(Range).filter(Range.assigned_to_user_id == learner.id).one()
        theirs.assigned_to_user_id = None
        db_session.commit()
        return theirs

    def test_a_self_paced_event_does_not_become_a_team_exercise(self, db_session, admin, event):
        ev, learners = event(students=3)
        events_api.start_event(ev.id, db_session, admin, auto_deploy=True)

        self.orphan(db_session, ev, learners[1])

        response = events_api.build_event_response(ev, db_session)
        assert response["delivery"] is Delivery.SELF_PACED
        assert response["team_range_id"] is None

    def test_a_late_joiner_is_not_handed_the_departed_learners_lab(
        self, db_session, admin, event, user, queued
    ):
        ev, learners = event(students=3)
        events_api.start_event(ev.id, db_session, admin, auto_deploy=True)
        abandoned = self.orphan(db_session, ev, learners[1])
        queued.clear()

        latecomer = user(UserRole.STUDENT)
        added = events_api.add_participant(
            ev.id,
            EventParticipantCreate(user_id=latecomer.id, role=ParticipantRole.STUDENT),
            db_session,
            admin,
        )

        assert added.range_id != abandoned.id, "a late joiner inherited a stranger's lab"
        assert len(queued) == 1, "a self-paced late joiner gets a lab of their own, deployed"

    def test_ending_the_event_still_takes_the_abandoned_lab_down(
        self, db_session, admin, event, monkeypatch
    ):
        """No participant points at it any more, and the event row is the only record it exists."""
        ev, learners = event(students=3)
        events_api.start_event(ev.id, db_session, admin, auto_deploy=True)
        abandoned = self.orphan(db_session, ev, learners[1])

        destroyed: list[uuid.UUID] = []
        monkeypatch.setattr(
            kubernetes_ranges,
            "destroy_for_cleanup",
            lambda db, range_obj: destroyed.append(range_obj.id),
        )

        events_api._delete_event_ranges(ev.id, db_session)

        assert abandoned.id in destroyed


class TestSelfPacedIsUnchanged:
    def test_one_range_per_student_each_assigned_to_them(self, db_session, admin, event, queued):
        ev, learners = event(students=3)

        events_api.start_event(ev.id, db_session, admin, auto_deploy=True)

        created = ranges_of(db_session, ev)
        assert len(created) == 3
        assert len(queued) == 3
        assert {r.assigned_to_user_id for r in created} == {learner.id for learner in learners}

    def test_each_learner_places_in_a_namespace_of_their_own(self, db_session, admin, event):
        ev, _ = event(students=3)

        events_api.start_event(ev.id, db_session, admin, auto_deploy=True)

        namespaces = {placement_of(db_session, r) for r in ranges_of(db_session, ev)}
        assert {p.isolation for p in namespaces} == {Isolation.NAMESPACE}
        assert len({p.namespace for p in namespaces}) == 3

    def test_the_event_reports_itself_self_paced(self, db_session, admin, event):
        ev, _ = event()

        response = events_api.start_event(ev.id, db_session, admin, auto_deploy=True)

        assert response["delivery"] is Delivery.SELF_PACED
        assert response["team_range_id"] is None

    def test_the_default_mode_is_self_paced(self, db_session, admin, event):
        """The mode the platform has always run, so it is what an unchanged caller still gets."""
        ev, _ = event(students=2)

        events_api.start_event(ev.id, db_session, admin, auto_deploy=True)

        assert len(ranges_of(db_session, ev)) == 2


class TestTheRoleVocabulary:
    def test_the_api_accepts_exactly_the_roles_the_form_offers(self):
        """One vocabulary, in one place.

        The form used to spell its four options inline next to the platform roles, so the two
        sets could drift apart -- and be read as one set while they did.
        """
        if not PAGE.exists():
            pytest.skip("the frontend is not present in this checkout")
        offered = set(re.findall(r"value:\s*'([a-z_]+)'", participant_roles_block()))
        assert offered == {role.value for role in ParticipantRole}

    def test_every_role_the_form_offers_changes_what_the_briefing_shows(self):
        """A label that changes nothing is a question with no answer."""
        if not PAGE.exists():
            pytest.skip("the frontend is not present in this checkout")
        block = participant_roles_block()
        for role in ParticipantRole:
            assert f"'{role.value}'" in block
        # Each entry carries the consequence it has, rather than only a label.
        assert block.count("effect:") == len(ParticipantRole)

    def test_a_role_outside_the_vocabulary_is_rejected(self):
        with pytest.raises(ValueError):
            EventParticipantCreate(user_id=uuid.uuid4(), role="white_cell")

    def test_self_registration_cannot_take_an_instructors_view_of_the_briefing(
        self, db_session, event, user
    ):
        """Joining as an instructor served the instructor notes to anyone who could see the event."""
        ev, _ = event(students=0)
        outsider = user(UserRole.STUDENT)

        with pytest.raises(HTTPException) as refused:
            events_api.join_event(ev.id, db_session, outsider, role=ParticipantRole.INSTRUCTOR)

        assert refused.value.status_code == 403
        assert (
            db_session.query(EventParticipant)
            .filter(EventParticipant.user_id == outsider.id)
            .first()
            is None
        )

    def test_joining_as_a_student_still_works(self, db_session, event, user):
        ev, _ = event(students=0)
        joiner = user(UserRole.STUDENT)

        joined = events_api.join_event(ev.id, db_session, joiner, role=ParticipantRole.STUDENT)

        assert joined.role == ParticipantRole.STUDENT.value

    def test_the_instructor_may_still_add_anyone_in_any_role(self, db_session, admin, event, user):
        ev, _ = event(students=0)
        evaluator = user(UserRole.EVALUATOR)

        added = events_api.add_participant(
            ev.id,
            EventParticipantCreate(user_id=evaluator.id, role=ParticipantRole.EVALUATOR),
            db_session,
            admin,
        )

        assert added.role == ParticipantRole.EVALUATOR.value
        assert added.range_id is None, "only students get a lab"
