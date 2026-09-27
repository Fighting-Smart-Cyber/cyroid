"""The Content API authenticated and never authorised.

Every route took `current_user`, so it required a login, and then compared it
to nothing. A student's token could `GET /api/v1/content` and read every
draft, every Instructor Notes document and every MSEL body, create content,
import content, and clone anyone's draft into a document of their own. The
Content Library is hidden from students in the browser by `ProtectedRoute`,
which is not a control -- the token is.

Content is not a range, so none of the three range checks in api/deps.py
applies: there are no tags over content, no assignment and no console. The
rule the API needed is its own. A read answers to the author's act of
publishing; a write answers to whether the caller writes training material at
all, through the same `require_any_role` dependency the admin routes use.

The quiz half is here too, because it is the same document. Every option in a
Knowledge Check used to arrive carrying its own `correct` flag, so the answer
key sat in the page source before the learner picked anything -- and the score
was never written down at all, which left the learner record holding nothing
about the product's only assessment instrument.

These drive the HTTP surface rather than calling the route functions, because
a guard declared in a signature is only real once FastAPI runs it.
"""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from proving_ground.api import content as content_api
from proving_ground.api import walkthrough as walkthrough_api
from proving_ground.api.deps import get_current_user
from proving_ground.database import get_db
from proving_ground.main import app
from proving_ground.models.content import Content, ContentType
from proving_ground.models.range import Range
from proving_ground.models.user import User, UserAttribute, UserRole
from proving_ground.models.walkthrough_progress import WalkthroughProgress
from proving_ground.schemas.content import (
    LearnerQuizOptionSchema,
    LearnerQuizQuestionSchema,
)

CONTENT = "/api/v1/content"


def make_user(db, *, username, role):
    user = User(
        username=username,
        email=f"{username}@example.test",
        hashed_password="x",
        role=UserRole.STUDENT if role == "student" else UserRole.ENGINEER,
    )
    db.add(user)
    db.commit()
    db.add(UserAttribute(user_id=user.id, attribute_type="role", attribute_value=role))
    db.commit()
    db.refresh(user)
    return user


def make_content(db, *, owner, content_type=ContentType.STUDENT_GUIDE, published=False, title="t"):
    item = Content(
        title=title,
        content_type=content_type,
        body_markdown="body",
        is_published=published,
        created_by_id=owner.id,
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


@pytest.fixture
def author(db_session):
    return make_user(db_session, username="author", role="engineer")


@pytest.fixture
def admin(db_session):
    return make_user(db_session, username="boss", role="admin")


@pytest.fixture
def student(db_session):
    return make_user(db_session, username="learner", role="student")


@pytest.fixture
def evaluator(db_session):
    return make_user(db_session, username="judge", role="evaluator")


@pytest.fixture
def as_user(db_session):
    """A client that authenticates as whichever user it is handed.

    TestClient is built without its context manager on purpose: the app's
    lifespan wants Redis and a real database, and none of these tests need
    either.
    """
    app.dependency_overrides[get_db] = lambda: db_session

    def _as(user):
        app.dependency_overrides[get_current_user] = lambda: user
        return TestClient(app)

    try:
        yield _as
    finally:
        app.dependency_overrides.clear()


class TestReadsAreGatedOnPublicationAndRole:
    def test_a_student_is_refused_an_unpublished_document(
        self, db_session, as_user, author, student
    ):
        draft = make_content(db_session, owner=author, published=False)
        assert as_user(student).get(f"{CONTENT}/{draft.id}").status_code == 403

    def test_a_student_is_refused_instructor_notes_even_when_published(
        self, db_session, as_user, author, student
    ):
        """Publishing releases a document to its audience.

        Instructor notes and MSELs were never written for the learner's
        audience, so 'published' is not consent to hand them to one.
        """
        notes = make_content(
            db_session, owner=author, content_type=ContentType.INSTRUCTOR_NOTES, published=True
        )
        assert as_user(student).get(f"{CONTENT}/{notes.id}").status_code == 403

    def test_a_student_may_read_a_published_student_guide(
        self, db_session, as_user, author, student
    ):
        guide = make_content(db_session, owner=author, published=True)
        response = as_user(student).get(f"{CONTENT}/{guide.id}")
        assert response.status_code == 200
        assert response.json()["id"] == str(guide.id)

    def test_the_export_routes_answer_to_the_same_rule(self, db_session, as_user, author, student):
        """Three ways out of the library, one rule.

        The markdown export and the git-native bundle carry the same body as
        the document itself, so a read check on the document alone would just
        move the leak one route sideways.
        """
        draft = make_content(db_session, owner=author, published=False)
        client = as_user(student)

        assert (
            client.get(f"{CONTENT}/{draft.id}/export", params={"format": "md"}).status_code == 403
        )
        assert client.get(f"{CONTENT}/{draft.id}/bundle").status_code == 403

    def test_an_author_reads_a_colleagues_draft(self, db_session, as_user, author):
        other = make_user(db_session, username="second", role="engineer")
        draft = make_content(db_session, owner=other, published=False)
        assert as_user(author).get(f"{CONTENT}/{draft.id}").status_code == 200

    def test_an_admin_reads_everything(self, db_session, as_user, author, admin):
        msel = make_content(
            db_session, owner=author, content_type=ContentType.MSEL, published=False
        )
        assert as_user(admin).get(f"{CONTENT}/{msel.id}").status_code == 200

    def test_an_evaluator_reads_the_instructor_side(self, db_session, as_user, author, evaluator):
        """Judging how an exercise ran means reading what drove it.

        The browser already lets an evaluator into /content; gating them out
        of the MSEL here would leave them on a page with nothing on it.
        """
        msel = make_content(
            db_session, owner=author, content_type=ContentType.MSEL, published=False
        )
        assert as_user(evaluator).get(f"{CONTENT}/{msel.id}").status_code == 200

    def test_a_missing_document_is_still_404(self, as_user, student):
        assert as_user(student).get(f"{CONTENT}/{uuid4()}").status_code == 404


class TestTheListShowsWhatAGetWouldReturn:
    def test_a_student_sees_only_published_learner_facing_content(
        self, db_session, as_user, author, student
    ):
        make_content(db_session, owner=author, published=False, title="draft guide")
        make_content(db_session, owner=author, published=True, title="published guide")
        make_content(
            db_session,
            owner=author,
            content_type=ContentType.INSTRUCTOR_NOTES,
            published=True,
            title="notes",
        )
        make_content(
            db_session, owner=author, content_type=ContentType.MSEL, published=True, title="msel"
        )

        listed = as_user(student).get(CONTENT).json()
        assert {c["title"] for c in listed} == {"published guide"}

    def test_an_author_sees_the_drafts_too(self, db_session, as_user, author):
        make_content(db_session, owner=author, published=False, title="draft guide")
        make_content(
            db_session,
            owner=author,
            content_type=ContentType.INSTRUCTOR_NOTES,
            published=False,
            title="notes",
        )

        listed = as_user(author).get(CONTENT).json()
        assert {c["title"] for c in listed} == {"draft guide", "notes"}

    def test_the_list_and_the_get_agree(self, db_session, as_user, author, student):
        """A listing that hides a document a direct GET returns is theatre."""
        items = [
            make_content(db_session, owner=author, published=p, content_type=t, title=f"{t}-{p}")
            for t in ContentType
            for p in (True, False)
        ]
        client = as_user(student)
        listed = {c["id"] for c in client.get(CONTENT).json()}

        for item in items:
            fetched = client.get(f"{CONTENT}/{item.id}").status_code == 200
            assert fetched is (str(item.id) in listed), item.title


class TestWritesRequireAnAuthoringRole:
    def _bundle_upload(self):
        return {"file": ("bundle.tar.gz", b"not a tarball", "application/gzip")}

    def test_a_student_may_not_create_or_import_content(self, db_session, as_user, student):
        client = as_user(student)

        assert client.post(CONTENT, json={"title": "mine"}).status_code == 403
        assert (
            client.post(f"{CONTENT}/import", json={"title": "m", "body_markdown": "x"}).status_code
            == 403
        )
        assert (
            client.post(f"{CONTENT}/bundle/preview", files=self._bundle_upload()).status_code == 403
        )
        assert (
            client.post(f"{CONTENT}/bundle/import", files=self._bundle_upload()).status_code == 403
        )
        assert db_session.query(Content).count() == 0

    def test_a_student_may_not_clone_a_draft_into_their_own_document(
        self, db_session, as_user, author, student
    ):
        """The version route copies the body into a row the caller owns.

        Without it, cloning is a way to lift a draft out from behind the read
        rule and then read it back as your own.
        """
        draft = make_content(db_session, owner=author, published=False)

        response = as_user(student).post(
            f"{CONTENT}/{draft.id}/version", params={"new_version": "2.0"}
        )
        assert response.status_code == 403
        assert db_session.query(Content).count() == 1

    def test_a_student_may_not_edit_publish_or_delete(self, db_session, as_user, author, student):
        guide = make_content(db_session, owner=author, published=True)
        client = as_user(student)
        url = f"{CONTENT}/{guide.id}"

        assert client.put(url, json={"title": "hijacked"}).status_code == 403
        assert client.post(f"{url}/publish").status_code == 403
        assert client.post(f"{url}/unpublish").status_code == 403
        assert client.delete(url).status_code == 403

        db_session.refresh(guide)
        assert guide.title == "t"
        assert guide.is_published is True

    def test_an_evaluator_reads_but_does_not_write(self, db_session, as_user, author, evaluator):
        """Reading the library is not the same permission as writing to it."""
        draft = make_content(db_session, owner=author, published=False)
        client = as_user(evaluator)

        assert client.get(f"{CONTENT}/{draft.id}").status_code == 200
        assert client.post(CONTENT, json={"title": "mine"}).status_code == 403
        assert client.put(f"{CONTENT}/{draft.id}", json={"title": "edited"}).status_code == 403

    def test_an_author_creates_their_own_content(self, db_session, as_user, author):
        response = as_user(author).post(CONTENT, json={"title": "mine"})
        assert response.status_code == 201
        assert response.json()["created_by_id"] == str(author.id)

    def test_reading_a_colleagues_draft_does_not_mean_rewriting_it(
        self, db_session, as_user, author
    ):
        """may_author is a read rule, not a write rule.

        An engineer sees a colleague's draft because authoring is
        collaborative. Changing it is still the owner's call.
        """
        other = make_user(db_session, username="second", role="engineer")
        draft = make_content(db_session, owner=other, published=False)
        client = as_user(author)

        assert client.get(f"{CONTENT}/{draft.id}").status_code == 200
        assert client.put(f"{CONTENT}/{draft.id}", json={"title": "rewritten"}).status_code == 403

    def test_every_write_route_declares_the_author_dependency(self):
        """A guard nobody can forget to call.

        The two range defects that survived a first fix both looked guarded.
        Declaring the role in the signature is what keeps a new write route
        from shipping open, so assert the declaration, not just the behaviour.
        """
        import inspect

        for name in (
            "create_content",
            "create_content_version",
            "import_content",
            "preview_content_bundle",
            "import_content_bundle",
        ):
            fn = getattr(content_api, name)
            annotation = inspect.signature(fn).parameters["current_user"].annotation
            assert annotation is content_api.AuthorUser, name


def _walkthrough_with_quiz():
    return {
        "title": "Guide",
        "phases": [
            {
                "id": "phase-1",
                "name": "Phase",
                "steps": [
                    {
                        "id": "phase-1-step-1",
                        "title": "Step",
                        "content": "do the thing",
                        "quiz": [
                            {
                                "id": "q1",
                                "prompt": "Which one?",
                                "explanation": "Because b is the one.",
                                "options": [
                                    {"id": "a", "text": "A", "correct": False},
                                    {"id": "b", "text": "B", "correct": True},
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }


def _all_options(walkthrough):
    for phase in walkthrough["phases"]:
        for step in phase["steps"]:
            for question in step.get("quiz", []):
                for option in question["options"]:
                    yield question, option


class TestTheAnswerKeyDoesNotReachTheBrowser:
    def test_an_unanswered_quiz_carries_no_correct_flag_and_no_explanation(self):
        delivered = walkthrough_api.strip_quiz_answers(_walkthrough_with_quiz(), None)

        for question, option in _all_options(delivered):
            assert "correct" not in option, "the answer key is still in the payload"
            assert "explanation" not in question
            assert "answered" not in question

    def test_stripping_does_not_damage_the_stored_document(self):
        """The walkthrough handed in is the row's own JSON, not a copy."""
        stored = _walkthrough_with_quiz()
        walkthrough_api.strip_quiz_answers(stored, None)

        assert stored["phases"][0]["steps"][0]["quiz"][0]["options"][1]["correct"] is True

    def test_an_answered_question_gets_its_own_answer_back(self):
        recorded = {
            "q1": {
                "question_id": "q1",
                "selected_option_id": "a",
                "correct": False,
                "correct_option_id": "b",
                "explanation": "Because b is the one.",
            }
        }
        delivered = walkthrough_api.strip_quiz_answers(_walkthrough_with_quiz(), recorded)
        question = delivered["phases"][0]["steps"][0]["quiz"][0]

        assert question["answered"]["correct_option_id"] == "b"
        for _, option in _all_options(delivered):
            assert "correct" not in option

    def test_the_learner_schema_has_nowhere_to_put_an_answer_key(self):
        """Belt and braces: even an unstripped question loses the key here."""
        question = LearnerQuizQuestionSchema.model_validate(
            {
                "id": "q1",
                "prompt": "Which one?",
                "explanation": "Because b is the one.",
                "options": [{"id": "b", "text": "B", "correct": True}],
            }
        )
        dumped = question.model_dump()

        assert "explanation" not in dumped
        assert "correct" not in dumped["options"][0]
        assert not hasattr(LearnerQuizOptionSchema.model_validate({"id": "b"}), "correct")


def _range_with_quiz(db_session, owner, *, assigned_to=None):
    guide = make_content(db_session, owner=owner, published=True)
    guide.walkthrough_data = _walkthrough_with_quiz()
    range_obj = Range(
        name="lab",
        created_by=owner.id,
        student_guide_id=guide.id,
        assigned_to_user_id=assigned_to.id if assigned_to else None,
    )
    db_session.add(range_obj)
    db_session.commit()
    db_session.refresh(range_obj)
    return range_obj


class TestTheAnswerKeyHasOnlyOneWayOut:
    """Stripping the walkthrough route alone moves the leak, it does not close it.

    A guide is a Content row, the range hands out its id, and the Content API
    returns walkthrough_data as the author wrote it. So a learner never had to
    go near the walkthrough route: read the range, read the guide, read the
    key. The document itself and the bundle that carries it have to answer to
    the same rule as the delivery route.
    """

    def test_a_learner_reading_the_guide_directly_gets_no_answer_key(
        self, db_session, as_user, author, student
    ):
        range_obj = _range_with_quiz(db_session, author, assigned_to=student)
        client = as_user(student)

        guide_id = client.get(f"/api/v1/ranges/{range_obj.id}").json()["student_guide_id"]
        body = client.get(f"{CONTENT}/{guide_id}")

        assert body.status_code == 200
        question = body.json()["walkthrough_data"]["phases"][0]["steps"][0]["quiz"][0]
        assert "explanation" not in question
        assert all("correct" not in o for o in question["options"])

    def test_the_library_still_gets_the_document_as_written(
        self, db_session, as_user, author, evaluator
    ):
        """The editor loads through this route, so stripping it for an author
        would empty the Knowledge Check out of their own document."""
        range_obj = _range_with_quiz(db_session, author)
        guide_id = str(range_obj.student_guide_id)

        for user in (author, evaluator):
            question = (
                as_user(user)
                .get(f"{CONTENT}/{guide_id}")
                .json()["walkthrough_data"]["phases"][0]["steps"][0]["quiz"][0]
            )
            assert question["explanation"] == "Because b is the one."
            assert [o.get("correct") for o in question["options"]] == [False, True]

    def test_withholding_the_key_does_not_edit_the_stored_document(
        self, db_session, as_user, author, student
    ):
        range_obj = _range_with_quiz(db_session, author, assigned_to=student)
        guide_id = range_obj.student_guide_id
        as_user(student).get(f"{CONTENT}/{guide_id}")

        stored = db_session.query(Content).filter(Content.id == guide_id).one()
        assert stored.walkthrough_data["phases"][0]["steps"][0]["quiz"][0]["options"][1]["correct"]

    def test_a_learner_may_not_export_a_bundle_of_a_guide_they_may_read(
        self, db_session, as_user, author, student
    ):
        """The bundle is the authored form, gzipped -- the key travels in it."""
        range_obj = _range_with_quiz(db_session, author, assigned_to=student)
        guide_id = range_obj.student_guide_id

        assert as_user(student).get(f"{CONTENT}/{guide_id}").status_code == 200
        assert as_user(student).get(f"{CONTENT}/{guide_id}/bundle").status_code == 403
        assert as_user(author).get(f"{CONTENT}/{guide_id}/bundle").status_code == 200


class TestTheAttemptReachesTheLearnerRecord:
    """A score that exists only in React state is evidence of nothing."""

    def test_the_delivered_walkthrough_carries_no_answer_key(self, db_session, as_user, author):
        range_obj = _range_with_quiz(db_session, author)

        body = as_user(author).get(f"/api/v1/ranges/{range_obj.id}/walkthrough").json()
        question = body["walkthrough"]["phases"][0]["steps"][0]["quiz"][0]

        assert question["answered"] is None
        assert "explanation" not in question
        assert all("correct" not in o for o in question["options"])

    def test_submitting_grades_server_side_and_writes_the_answer(self, db_session, as_user, author):
        range_obj = _range_with_quiz(db_session, author)

        response = as_user(author).post(
            f"/api/v1/ranges/{range_obj.id}/walkthrough/quiz",
            json={"question_id": "q1", "option_id": "b"},
        )
        assert response.status_code == 200
        assert response.json()["correct"] is True
        assert response.json()["step_id"] == "phase-1-step-1"

        stored = (
            db_session.query(WalkthroughProgress)
            .filter(WalkthroughProgress.range_id == range_obj.id)
            .one()
        )
        assert stored.quiz_answers["q1"]["selected_option_id"] == "b"
        assert stored.quiz_answers["q1"]["correct"] is True
        assert stored.quiz_answers["q1"]["answered_at"]

    def test_a_wrong_answer_is_recorded_as_wrong(self, db_session, as_user, author):
        range_obj = _range_with_quiz(db_session, author)

        body = (
            as_user(author)
            .post(
                f"/api/v1/ranges/{range_obj.id}/walkthrough/quiz",
                json={"question_id": "q1", "option_id": "a"},
            )
            .json()
        )
        assert body["correct"] is False
        assert body["correct_option_id"] == "b"

    def test_an_answered_question_comes_back_answered(self, db_session, as_user, author):
        """A reload must not present a quiz the learner has already sat."""
        range_obj = _range_with_quiz(db_session, author)
        client = as_user(author)
        client.post(
            f"/api/v1/ranges/{range_obj.id}/walkthrough/quiz",
            json={"question_id": "q1", "option_id": "a"},
        )

        body = client.get(f"/api/v1/ranges/{range_obj.id}/walkthrough").json()
        question = body["walkthrough"]["phases"][0]["steps"][0]["quiz"][0]

        assert question["answered"]["selected_option_id"] == "a"
        assert question["answered"]["correct"] is False
        assert all("correct" not in o for o in question["options"])

    def test_a_question_may_only_be_answered_once(self, db_session, as_user, author):
        range_obj = _range_with_quiz(db_session, author)
        client = as_user(author)
        url = f"/api/v1/ranges/{range_obj.id}/walkthrough/quiz"
        client.post(url, json={"question_id": "q1", "option_id": "a"})

        assert client.post(url, json={"question_id": "q1", "option_id": "b"}).status_code == 409

        stored = (
            db_session.query(WalkthroughProgress)
            .filter(WalkthroughProgress.range_id == range_obj.id)
            .one()
        )
        assert stored.quiz_answers["q1"]["selected_option_id"] == "a"

    def test_a_question_or_option_that_was_never_offered_is_refused(
        self, db_session, as_user, author
    ):
        range_obj = _range_with_quiz(db_session, author)
        client = as_user(author)
        url = f"/api/v1/ranges/{range_obj.id}/walkthrough/quiz"

        assert client.post(url, json={"question_id": "nope", "option_id": "b"}).status_code == 404
        assert client.post(url, json={"question_id": "q1", "option_id": "z"}).status_code == 400
        assert db_session.query(WalkthroughProgress).count() == 0


class TestWalkthroughRoutesCheckTheRange:
    """check_range_access, not check_resource_control.

    A learner assigned to a lab is exactly who these routes are for and never
    controls the range, so control would refuse the only user the feature has.
    Access is still required: a record of work against a range you were never
    given is not evidence of anything.
    """

    def test_the_assigned_learner_is_still_admitted(self, db_session, as_user, author, student):
        """The check has to let in the only user the feature has.

        check_resource_control would have refused this learner: a student is
        assigned a lab, never given control of it.
        """
        range_obj = _range_with_quiz(db_session, author, assigned_to=student)
        client = as_user(student)
        base = f"/api/v1/ranges/{range_obj.id}/walkthrough"

        assert client.get(base).status_code == 200
        assert client.get(f"{base}/progress").status_code == 200
        assert client.put(f"{base}/progress", json={"completed_steps": ["s1"]}).status_code == 200

        graded = client.post(f"{base}/quiz", json={"question_id": "q1", "option_id": "b"})
        assert graded.status_code == 200
        assert graded.json()["correct"] is True

    def test_a_stranger_is_refused_the_walkthrough_and_the_progress(
        self, db_session, as_user, author, student
    ):
        range_obj = _range_with_quiz(db_session, author)
        client = as_user(student)
        base = f"/api/v1/ranges/{range_obj.id}/walkthrough"

        assert client.get(base).status_code == 403
        assert client.get(f"{base}/progress").status_code == 403
        assert client.put(f"{base}/progress", json={"completed_steps": ["s1"]}).status_code == 403

    def test_a_stranger_cannot_forge_a_quiz_answer(self, db_session, as_user, author, student):
        range_obj = _range_with_quiz(db_session, author)

        response = as_user(student).post(
            f"/api/v1/ranges/{range_obj.id}/walkthrough/quiz",
            json={"question_id": "q1", "option_id": "b"},
        )
        assert response.status_code == 403
        assert db_session.query(WalkthroughProgress).count() == 0
