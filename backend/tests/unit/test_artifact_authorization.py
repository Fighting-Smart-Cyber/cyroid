"""The artifact library was login-only, on every route.

`list_artifacts` returned every artifact in the install, `get`, `download`, `update` and
`delete` took any id, and `list_placements` returned a map of which sample sits on which
machine in whose exercise. None of it was reachable from the UI, which is the only reason it
had not been noticed -- and the decision to build an Artifacts page is what made it urgent.

`check_resource_access` is deliberately not the guard here. Its "no tags means public" rule is
a visibility model for ranges; applied to a library whose model carries a `malicious_indicator`
column it would hand every learner the whole thing.
"""

import io
import uuid

import pytest
from fastapi import HTTPException

from proving_ground.api import artifacts as api
from proving_ground.models.artifact import Artifact
from proving_ground.models.user import User, UserAttribute, UserRole


def user(db, role=UserRole.STUDENT):
    """A user whose role is a UserAttribute row, which is where `roles` actually reads from.

    Setting only the legacy `role` column leaves `User.roles` empty, so `is_admin` and
    `has_any_role` are both False -- a fixture that does that tests nothing about roles.
    """
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


def artifact(db, owner, name="sample"):
    a = Artifact(
        name=name,
        file_path=f"artifacts/{uuid.uuid4().hex}",
        sha256_hash="0" * 64,
        file_size=1,
        uploaded_by=owner.id,
    )
    db.add(a)
    db.commit()
    return a


class TestTheLibraryIsNotPublicToEverySignedInAccount:
    def test_a_student_does_not_see_someone_elses_artifact_in_the_listing(self, db_session):
        engineer = user(db_session, UserRole.ENGINEER)
        artifact(db_session, engineer)
        student = user(db_session, UserRole.STUDENT)

        assert api.list_artifacts(db_session, student) == []

    def test_an_engineer_sees_the_whole_library_not_just_their_own(self, db_session):
        uploader = user(db_session, UserRole.ENGINEER)
        artifact(db_session, uploader)
        other_engineer = user(db_session, UserRole.ENGINEER)

        assert len(api.list_artifacts(db_session, other_engineer)) == 1

    def test_a_student_cannot_fetch_one_by_id(self, db_session):
        engineer = user(db_session, UserRole.ENGINEER)
        a = artifact(db_session, engineer)
        student = user(db_session, UserRole.STUDENT)

        with pytest.raises(HTTPException) as exc:
            api.get_artifact(a.id, db_session, student)
        assert exc.value.status_code == 403

    def test_a_student_cannot_download_the_bytes(self, db_session):
        engineer = user(db_session, UserRole.ENGINEER)
        a = artifact(db_session, engineer)
        student = user(db_session, UserRole.STUDENT)

        with pytest.raises(HTTPException) as exc:
            api.download_artifact(a.id, db_session, student)
        assert exc.value.status_code == 403

    def test_a_student_cannot_list_placements(self, db_session):
        student = user(db_session, UserRole.STUDENT)

        with pytest.raises(HTTPException) as exc:
            api.list_placements(None, None, db_session, student)
        assert exc.value.status_code == 403


class TestChangingOneIsStricterThanSeeingIt:
    """An engineer may browse the library. That never implied editing someone else's sample."""

    def test_one_engineer_cannot_delete_anothers(self, db_session):
        owner = user(db_session, UserRole.ENGINEER)
        a = artifact(db_session, owner)
        other = user(db_session, UserRole.ENGINEER)

        with pytest.raises(HTTPException) as exc:
            api.delete_artifact(a.id, db_session, other)
        assert exc.value.status_code == 403

    def test_the_uploader_may(self, db_session):
        owner = user(db_session, UserRole.ENGINEER)
        a = artifact(db_session, owner)

        api.check_artifact_control(a, owner)  # does not raise

    def test_an_admin_may(self, db_session):
        owner = user(db_session, UserRole.ENGINEER)
        a = artifact(db_session, owner)
        admin = user(db_session, UserRole.ADMIN)

        api.check_artifact_control(a, admin)  # does not raise
