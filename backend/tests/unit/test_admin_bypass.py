"""The event log, the MSEL and the connection list answer to the range's rules.

All three routers carried their own authorization idiom -- a bare
`range_obj.created_by != current_user.id` repeated once per route -- and it was
strictly narrower than any of the three shared checks. The consequences ran
both ways.

An administrator was refused on a range they did not create. The event log is
where the deploy worker writes its placement reason and its failure text, so
the one person who can be asked why a learner's range failed was the one person
who could not read the answer. And an assigned learner was refused the history
and the traffic of the range assigned to them.

A fourth idiom is also how the next route gets added without a check at all:
there was nothing to copy but a comparison, and nothing auditing whether it had
been copied. So these routes now defer to `check_range_access` and
`check_range_control` in api/deps.py, and the reads and the writes take
deliberately different ones -- reading a scenario is not firing an inject,
which runs commands inside the range's machines.
"""

import pathlib
import re
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi import HTTPException

from proving_ground.api import connections as connections_api
from proving_ground.api import events as events_api
from proving_ground.api import msel as msel_api
from proving_ground.models.inject import InjectStatus
from proving_ground.models.range import RangeVisibility

API = pathlib.Path(__file__).resolve().parents[2] / "proving_ground" / "api"
MODULES = ("events.py", "msel.py", "connections.py")

RANGE_ID = uuid4()
VM_ID = uuid4()
MSEL_ID = uuid4()
INJECT_ID = uuid4()
OWNER_ID = uuid4()


# --- a session that answers the queries the helpers actually make -----------


def _key(entity):
    """Seed key for a query target.

    `db.query(Range)` and `db.query(Range.id)` are different questions here:
    the first loads the range, the second asks which ranges a student is
    assigned to. Keying on the exact target keeps the two seedable apart, which
    is what lets an assigned student and an unassigned one be told apart below.
    """
    if isinstance(entity, type):
        return entity.__name__
    return f"{entity.class_.__name__}.{entity.key}"


class _Query:
    def __init__(self, rows):
        self._rows = list(rows)

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def options(self, *a, **k):
        return self

    def offset(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def count(self):
        return len(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def all(self):
        return list(self._rows)


class FakeSession:
    """Serves seeded rows and ignores filter predicates.

    Every route here decides authorization before it reads anything the answer
    depends on, so what a WHERE clause would have matched is not what is under
    test. Ignoring predicates keeps the fake small enough to be obviously
    right; the seed keys carry the distinctions that matter.
    """

    def __init__(self, rows):
        self.rows = rows
        self.added = []
        self.deleted = []
        self.commits = 0

    def query(self, *entities):
        return _Query(self.rows.get(_key(entities[0]), []))

    def add(self, obj):
        self.added.append(obj)

    def delete(self, obj):
        self.deleted.append(obj)

    def commit(self):
        self.commits += 1

    def refresh(self, obj):
        # A real flush assigns the primary key; the response model requires it.
        if getattr(obj, "id", None) is None:
            obj.id = uuid4()


def _walkthrough():
    """A guide written inside the MSEL, with a Knowledge Check in it.

    This is the shape api/walkthrough.py strips before a browser sees it: every
    option carries its own `correct` flag and every question its explanation.
    """
    return {
        "title": "Restore the service",
        "phases": [
            {
                "name": "Triage",
                "steps": [
                    {
                        "title": "Find the failed pod",
                        "quiz": [
                            {
                                "id": "q1",
                                "prompt": "Which namespace holds it?",
                                "explanation": "The range namespace, always.",
                                "options": [
                                    {"id": "a", "text": "kube-system", "correct": False},
                                    {"id": "b", "text": "the range namespace", "correct": True},
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }


def _range():
    return SimpleNamespace(
        id=RANGE_ID,
        created_by=OWNER_ID,
        visibility=RangeVisibility.PRIVATE,
        assigned_to_user_id=None,
        training_event_id=None,
    )


def _session(*, assigned_to=None):
    """A range owned by OWNER_ID, plus the rows each route reads after its check.

    `assigned_to` seeds the student-assignment lookup, which is the only thing
    separating a learner who was given this lab from one who was not.
    """
    return FakeSession(
        {
            "Range": [_range()],
            "Range.id": [SimpleNamespace(id=RANGE_ID)] if assigned_to else [],
            "EventParticipant.range_id": [],
            "VM": [SimpleNamespace(id=VM_ID, range_id=RANGE_ID, hostname="host-1")],
            "EventLog": [],
            "Connection": [],
            "MSEL": [
                SimpleNamespace(
                    id=MSEL_ID,
                    name="scenario",
                    range_id=RANGE_ID,
                    content="# Scenario\n",
                    walkthrough=_walkthrough(),
                )
            ],
            "Inject": [
                SimpleNamespace(
                    id=INJECT_ID,
                    msel_id=MSEL_ID,
                    sequence_number=1,
                    inject_time_minutes=0,
                    title="first inject",
                    description="",
                    actions=[],
                    status=InjectStatus.PENDING,
                    executed_at=None,
                    execution_log=None,
                )
            ],
        }
    )


def _user(*, admin=False, student=False, uid=None):
    roles = (["admin"] if admin else []) + (["student"] if student else [])
    return SimpleNamespace(
        id=uid or uuid4(),
        is_admin=admin,
        roles=roles,
        tags=[],
        has_any_tag=lambda *tags: False,
    )


# --- the routes, by the question each one answers ---------------------------

READS = {
    "GET /events/{range_id}": lambda u, db: events_api.get_range_events(
        range_id=RANGE_ID, limit=100, offset=0, event_types=None, db=db, current_user=u
    ),
    "GET /events/vm/{vm_id}": lambda u, db: events_api.get_vm_events(
        vm_id=VM_ID, limit=50, db=db, current_user=u
    ),
    "GET /connections/{range_id}": lambda u, db: connections_api.get_range_connections(
        range_id=RANGE_ID, limit=100, offset=0, active_only=False, db=db, current_user=u
    ),
    "GET /connections/vm/{vm_id}": lambda u, db: connections_api.get_vm_connections(
        vm_id=VM_ID, direction="both", limit=50, db=db, current_user=u
    ),
    "GET /msel/{range_id}": lambda u, db: msel_api.get_msel(
        range_id=RANGE_ID, db=db, current_user=u
    ),
}

CHANGES = {
    "POST /msel/{range_id}/import": lambda u, db: msel_api.import_msel(
        range_id=RANGE_ID,
        data=msel_api.MSELImport(name="scenario", content="# Scenario\n"),
        db=db,
        current_user=u,
    ),
    "DELETE /msel/{range_id}": lambda u, db: msel_api.delete_msel(
        range_id=RANGE_ID, db=db, current_user=u
    ),
    "POST /msel/inject/{inject_id}/execute": lambda u, db: msel_api.execute_inject(
        inject_id=INJECT_ID, db=db, current_user=u, docker_service=MagicMock()
    ),
    "POST /msel/inject/{inject_id}/skip": lambda u, db: msel_api.skip_inject(
        inject_id=INJECT_ID, db=db, current_user=u
    ),
}

ALL_ROUTES = {**READS, **CHANGES}


def _status_of(call, user, db):
    """Run a route and report the HTTP status it refused with, or None."""
    try:
        call(user, db)
    except HTTPException as exc:
        return exc.status_code
    return None


class TestAnAdministratorIsNotRefused:
    """The reported defect. Admin is the first clause of all three shared
    checks and was absent from all three routers."""

    @pytest.mark.parametrize("name", sorted(ALL_ROUTES))
    def test_an_admin_reaches_a_range_they_do_not_own(self, name):
        status = _status_of(ALL_ROUTES[name], _user(admin=True), _session())
        assert status is None, f"{name} refused an administrator with {status}"


class TestTheOwnerStillReachesEverything:
    """Widening must not have cost the owner anything; that would trade one
    lockout for another."""

    @pytest.mark.parametrize("name", sorted(ALL_ROUTES))
    def test_the_owner_is_admitted(self, name):
        status = _status_of(ALL_ROUTES[name], _user(uid=OWNER_ID), _session())
        assert status is None, f"{name} refused the range's owner with {status}"


class TestAnUnrelatedStudentIsStillRefused:
    """The other direction, and the one that matters more. A learner with no
    relationship to this range must not gain its history, its traffic or its
    scenario because the check moved to a shared helper."""

    @pytest.mark.parametrize("name", sorted(ALL_ROUTES))
    def test_a_student_with_no_assignment_is_refused(self, name):
        status = _status_of(ALL_ROUTES[name], _user(student=True), _session())
        assert status == 403, f"{name} admitted an unassigned student (status {status})"


class TestAnAssignedLearnerReadsTheirOwnRange:
    """The second half of the defect: the shared read check admits the learner
    the range is assigned to, and the owner-only comparison did not."""

    @pytest.mark.parametrize("name", sorted(READS))
    def test_the_assigned_learner_may_read(self, name):
        learner = _user(student=True)
        status = _status_of(READS[name], learner, _session(assigned_to=learner.id))
        assert status is None, f"{name} refused the assigned learner with {status}"

    @pytest.mark.parametrize("name", sorted(CHANGES))
    def test_the_assigned_learner_may_not_change_the_exercise(self, name):
        """An assignment is permission to use a lab, not to rewrite its
        scenario or fire its injects."""
        learner = _user(student=True)
        status = _status_of(CHANGES[name], learner, _session(assigned_to=learner.id))
        assert status == 403, f"{name} let an assigned learner change the exercise"


class TestAMissingRangeIsA404NotA500:
    """Both VM routes fetched the VM's range and dereferenced it unchecked, so
    a VM whose range row is gone produced an AttributeError, which reaches the
    user as a bare 500."""

    @pytest.mark.parametrize("name", ["GET /events/vm/{vm_id}", "GET /connections/vm/{vm_id}"])
    def test_an_orphaned_vm_is_refused_cleanly(self, name):
        db = _session()
        db.rows["Range"] = []
        assert _status_of(READS[name], _user(admin=True), db) == 404


class TestTheScenarioIsNotHandedToTheLearner:
    """Admitting the learner to this route is not admitting them to all of it.

    The lab page reads /msel/{range_id} for the guide an instructor may have
    written inside the MSEL rather than linked from the library, so the route
    has to answer a learner. But an MSEL is the exercise script: the raw
    scenario, and an inject timeline saying what will be done to their machines
    and when. api/content.py already says the MSEL type is never handed to a
    learner, and api/walkthrough.py already strips the Knowledge Check answer
    key before a browser sees a guide. Moving this read onto the shared check
    without honouring either of those would route around both.
    """

    def _learner_view(self):
        learner = _user(student=True)
        return msel_api.get_msel(
            range_id=RANGE_ID, db=_session(assigned_to=learner.id), current_user=learner
        )

    def test_the_learner_gets_the_guide(self):
        """Withholding it would break the lab page, which is the reason this
        route admits a learner at all."""
        assert self._learner_view().walkthrough is not None

    def test_the_guide_carries_no_answer_key(self):
        quiz = self._learner_view().walkthrough["phases"][0]["steps"][0]["quiz"][0]
        assert "explanation" not in quiz
        assert all("correct" not in option for option in quiz["options"])

    def test_the_learner_gets_neither_the_raw_scenario_nor_the_inject_timeline(self):
        view = self._learner_view()
        assert view.content is None
        assert view.injects == []

    def test_a_student_who_owns_the_range_still_gets_all_of_it(self):
        """Anyone may create a range, so a student-only account can be the
        author of this MSEL. Withholding their own document from them would
        trade the reported lockout for a quieter one."""
        author = _user(student=True, uid=OWNER_ID)
        view = msel_api.get_msel(range_id=RANGE_ID, db=_session(), current_user=author)
        assert view.content == "# Scenario\n"
        assert len(view.injects) == 1
        assert (
            view.walkthrough["phases"][0]["steps"][0]["quiz"][0]["options"][0]["correct"] is False
        )

    @pytest.mark.parametrize("staff", ["admin", "owner"])
    def test_staff_still_see_the_whole_scenario(self, staff):
        user = _user(admin=True) if staff == "admin" else _user(uid=OWNER_ID)
        view = msel_api.get_msel(range_id=RANGE_ID, db=_session(), current_user=user)
        assert view.content == "# Scenario\n"
        assert len(view.injects) == 1
        assert "correct" in view.walkthrough["phases"][0]["steps"][0]["quiz"][0]["options"][0]


# --- the idiom must not come back -------------------------------------------


_DECORATOR = re.compile(r"@router\.(get|put|post|patch|delete|head|options|websocket)\b")


def _routes(filename):
    """Every route in a module, as (VERB, path, source from its decorator on).

    The verb is matched without requiring the path on the same line. A route
    whose decorator the formatter wrapped over several lines, or one declared
    with a verb this module happens not to use today, would otherwise be
    invisible here -- and an audit that cannot see a route reports it as
    guarded.
    """
    src = (API / filename).read_text().splitlines()
    starts = [
        (i, m.group(1)) for i, line in enumerate(src) if (m := _DECORATOR.match(line.strip()))
    ]

    out = []
    for n, (line_no, verb) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(src)
        body = "\n".join(src[line_no:end])
        path = re.search(r"""["']([^"']*)["']""", body)
        out.append((verb.upper(), path.group(1) if path else "", body))
    return out


class TestEveryRouteDefersToTheSharedChecks:
    @pytest.mark.parametrize("filename", MODULES)
    def test_no_route_compares_the_owner_by_hand(self, filename):
        """A hand-rolled comparison is a fourth authorization model, and it was
        narrower than all three of the real ones."""
        src = (API / filename).read_text()
        assert "created_by != current_user.id" not in src
        assert "created_by != current_user" not in src

    @pytest.mark.parametrize("filename", MODULES)
    def test_every_route_is_guarded(self, filename):
        routes = _routes(filename)
        assert routes, f"{filename}: parsed no routes, so this proves nothing"
        missing = [
            f"{verb} {path or '(collection)'}"
            for verb, path, body in routes
            if "check_range_access" not in body and "check_range_control" not in body
        ]
        assert not missing, f"{filename} has unguarded routes:\n  " + "\n  ".join(missing)

    @pytest.mark.parametrize("filename", MODULES)
    def test_routes_that_change_something_require_control(self, filename):
        """The read check is a visibility model. Reaching for it on a route
        that changes state is how the earlier defects survived their first fix,
        so assert the split rather than trusting it."""
        weak = [
            f"{verb} {path}"
            for verb, path, body in _routes(filename)
            if verb in {"POST", "PUT", "PATCH", "DELETE"} and "check_range_control" not in body
        ]
        assert not weak, f"{filename}: changes a range on a read check:\n  " + "\n  ".join(weak)

    @pytest.mark.parametrize("filename", MODULES)
    def test_reads_are_not_locked_to_owners(self, filename):
        """Over-tightening is the quieter regression: it looks like a broken
        page, not like an authorization decision."""
        overtight = [
            f"GET {path}"
            for verb, path, body in _routes(filename)
            if verb == "GET" and "check_range_control" in body
        ]
        assert not overtight, f"{filename}: reads demand ownership:\n  " + "\n  ".join(overtight)
