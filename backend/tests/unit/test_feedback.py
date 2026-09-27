"""Feedback is a private channel to the vendor, and it reads its own context.

Two decisions are under test, because both are easy to erode later:

* An author sees their own reports and nothing else. COSMOS makes every item readable by every
  member of the org, which is right for a project-management tool and wrong here: content
  feedback says "I could not finish this lab" about a learner whose demonstrated competence is
  the product's output, and a queue that doubles as a list of who struggled is not one anybody
  writes candidly into.
* The context rides in typed columns, not in prose, so one guide's feedback comes back as a
  group rather than by reading every report.
"""

import uuid

import pytest
from fastapi import HTTPException

from proving_ground.api import feedback as api
from proving_ground.api.feedback import FeedbackCreate, FeedbackStatusUpdate
from proving_ground.models.feedback import FeedbackKind, FeedbackSource, FeedbackStatus
from proving_ground.models.user import User, UserAttribute, UserRole


def user(db, role=UserRole.STUDENT):
    tag = uuid.uuid4().hex[:8]
    u = User(
        username=f"{role.value}-{tag}",
        email=f"{tag}@x.invalid",
        hashed_password="x",
        role=role,
        is_active=True,
        is_approved=True,
    )
    db.add(u)
    db.flush()
    db.add(UserAttribute(user_id=u.id, attribute_type="role", attribute_value=role.value))
    db.commit()
    db.refresh(u)
    return u


def submit(db, author, **kwargs):
    payload = FeedbackCreate(**{"title": "something is wrong", **kwargs})
    return api.submit_feedback(payload, db, author)


class TestAnAuthorSeesTheirOwnAndNothingElse:
    def test_a_learner_does_not_see_another_learners_report(self, db_session):
        mine = user(db_session)
        theirs = user(db_session)
        submit(db_session, theirs, title="their problem")

        assert api.list_feedback(db_session, mine) == []

    def test_a_learner_sees_their_own(self, db_session):
        author = user(db_session)
        submit(db_session, author, title="my problem")

        listed = api.list_feedback(db_session, author)
        assert [f.title for f in listed] == ["my problem"]

    def test_staff_see_everything(self, db_session):
        learner = user(db_session)
        submit(db_session, learner, title="a learner's problem")
        engineer = user(db_session, UserRole.ENGINEER)

        assert len(api.list_feedback(db_session, engineer)) == 1

    def test_fetching_someone_elses_by_id_is_a_404_not_a_403(self, db_session):
        """Whether another account's report exists is not this one's business either way, and
        the two answers must not be distinguishable."""
        theirs = submit(db_session, user(db_session), title="theirs")
        nosy = user(db_session)

        with pytest.raises(HTTPException) as exc:
            api.get_feedback(theirs.id, db_session, nosy)
        assert exc.value.status_code == 404


class TestOnlyStaffMoveItAlong:
    def test_an_author_may_not_set_their_own_status(self, db_session):
        author = user(db_session)
        item = submit(db_session, author)

        with pytest.raises(HTTPException) as exc:
            api.set_feedback_status(
                item.id, FeedbackStatusUpdate(status=FeedbackStatus.RESOLVED), db_session, author
            )
        assert exc.value.status_code == 403

    def test_an_engineer_may(self, db_session):
        item = submit(db_session, user(db_session))
        engineer = user(db_session, UserRole.ENGINEER)

        updated = api.set_feedback_status(
            item.id, FeedbackStatusUpdate(status=FeedbackStatus.PLANNED), db_session, engineer
        )
        assert updated.status == FeedbackStatus.PLANNED


class TestTheContextIsQueryable:
    """The point of typed columns: a guide's feedback is a query, not a read-through."""

    def test_one_guides_steps_come_back_as_a_group(self, db_session):
        author = user(db_session, UserRole.ENGINEER)
        for ref in ("intro/step-1", "intro/step-2", "other-guide/step-1"):
            submit(
                db_session,
                author,
                title=f"problem at {ref}",
                kind=FeedbackKind.CONTENT_PROBLEM,
                source=FeedbackSource.WALKTHROUGH,
                source_ref=ref,
            )

        found = api.list_feedback(db_session, author, source_ref_prefix="intro/")
        assert sorted(f.source_ref for f in found) == ["intro/step-1", "intro/step-2"]

    def test_filtering_by_kind_separates_content_from_platform(self, db_session):
        author = user(db_session, UserRole.ENGINEER)
        submit(db_session, author, title="platform", kind=FeedbackKind.BUG)
        submit(db_session, author, title="guide", kind=FeedbackKind.CONTENT_PROBLEM)

        content = api.list_feedback(db_session, author, kind=FeedbackKind.CONTENT_PROBLEM)
        assert [f.title for f in content] == ["guide"]


class TestTheContextSnapshotIsBounded:
    """The client decides what to send and the client is a browser, so this is the boundary."""

    def test_unknown_keys_are_dropped(self, db_session):
        author = user(db_session)
        item = submit(
            db_session,
            author,
            context={"route": "/ranges/x", "cookies": "secret", "token": "also secret"},
        )
        assert item.context == {"route": "/ranges/x"}

    def test_long_values_are_truncated(self, db_session):
        author = user(db_session)
        item = submit(db_session, author, context={"route": "x" * 5000})
        assert len(item.context["route"]) == 200
