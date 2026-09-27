"""Scenario and file writes require a role, and one bad file is one bad file.

Two defects met here.

The first: every route in `api/scenarios.py` and `api/files.py` asked only for
a login. Both write to the data volume every API and worker pod mounts -- the
image trees blueprints build from, and the scenario YAML ranges are built from
-- so a student account could overwrite another author's work, or plant a shell
script in an image tree that a later build would run. Neither resource is a
range, so none of the three range checks in `deps.py` has anything to answer
with: there is no owner row for `check_resource_control` to compare against and
no range visibility for `check_resource_access` to consult. Authoring is the
role, so the role is the check.

The second: a single unusable file took the whole list down. The scenarios list
was built in a comprehension, so the first file whose fields did not match the
response model raised and every caller got a 500 naming nothing. The product's
own "Training Scenario" file template writes exactly such a file -- it emits
`required_roles` as a list of mappings where the API declares a list of names --
so creating a scenario from the template blanked the Training Scenarios page and
the range builder's scenario picker for everyone until someone deleted the file
by hand. Combined with the first defect, any authenticated account could do it.
"""

import asyncio
import inspect
import io

import pytest
from fastapi import HTTPException, UploadFile

from proving_ground.api import files as files_api
from proving_ground.api import scenarios as scenarios_api
from proving_ground.services import scenario_filesystem as fs


class FakeUser:
    """Enough of a User for the role checks; they read nothing else."""

    def __init__(self, *roles):
        self.roles = list(roles)
        self.id = "00000000-0000-0000-0000-000000000001"
        self.username = "tester"

    @property
    def is_admin(self):
        return "admin" in self.roles

    def has_any_role(self, *roles):
        return any(r in self.roles for r in roles)


def dependency_of(alias):
    """The callable behind an Annotated[User, Depends(...)] alias."""
    return alias.__metadata__[0].dependency


STUDENT = FakeUser("student")
EVALUATOR = FakeUser("evaluator")
ENGINEER = FakeUser("engineer")
ADMIN = FakeUser("admin")


# The template the app's own Create File dialog writes for "Training Scenario".
# Reproduced verbatim from frontend/src/components/files/CreateFileModal.tsx so
# this test fails if the shapes ever agree again by accident on one side only.
APP_TEMPLATE = """id: new-scenario
name: "New Training Scenario"
description: "Brief description of this scenario"
category: red-team
difficulty: intermediate
duration_hours: 4

objectives:
  - "Primary learning objective"

required_roles:
  - role: attacker
    description: "Attack platform"
  - role: target
    description: "Target system to compromise"

events:
  - id: event-1
    time_offset: "00:00:00"
    type: INJECT
    title: "Initial Access"
    description: "Begin the scenario"
    target_role: attacker
"""

GOOD_SCENARIO = """seed_id: good-one
name: A Readable Scenario
description: This one parses.
category: blue-team
difficulty: beginner
duration_minutes: 45
required_roles:
  - defender
events:
  - sequence: 1
    delay_minutes: 0
    title: First inject
    target_role: defender
"""


@pytest.fixture
def scenarios_dir(tmp_path, monkeypatch):
    """Point the scenario service at an empty directory and drop its cache."""
    monkeypatch.setattr(fs, "SCENARIOS_DIR", tmp_path)
    fs.refresh_cache()
    yield tmp_path
    fs.refresh_cache()


class TestAStudentIsRefusedEveryWrite:
    """The defect itself: a login was the only thing either API asked for."""

    @pytest.mark.parametrize("alias", [scenarios_api.ScenarioAuthor, files_api.ContentAuthor])
    def test_a_student_is_refused(self, alias):
        with pytest.raises(HTTPException) as e:
            dependency_of(alias)(STUDENT)
        assert e.value.status_code == 403

    @pytest.mark.parametrize("alias", [scenarios_api.ScenarioAuthor, files_api.ContentAuthor])
    def test_an_evaluator_is_refused(self, alias):
        """Reading a scenario in the range builder is not authoring one."""
        with pytest.raises(HTTPException) as e:
            dependency_of(alias)(EVALUATOR)
        assert e.value.status_code == 403

    @pytest.mark.parametrize("alias", [scenarios_api.ScenarioAuthor, files_api.ContentAuthor])
    @pytest.mark.parametrize("user", [ENGINEER, ADMIN])
    def test_an_author_is_allowed(self, alias, user):
        assert dependency_of(alias)(user) is user


def endpoints(router):
    for route in router.routes:
        params = inspect.signature(route.endpoint).parameters
        yield route.path, sorted(route.methods - {"HEAD", "OPTIONS"}), params["current_user"]


class TestEveryRouteCarriesACheck:
    """An audit, so a route added later cannot quietly ship unguarded."""

    # The scenario reads that are deliberately authenticated-only: a scenario
    # definition is training design and the range builder's picker shows it to
    # every evaluator who can open a range. Any other read route added here
    # has to be justified by editing this list, which is the point.
    OPEN_TO_ANY_LOGIN = {("/scenarios", "GET"), ("/scenarios/{scenario_id}", "GET")}

    def test_every_scenario_write_requires_an_author(self):
        unguarded = []
        for path, methods, param in endpoints(scenarios_api.router):
            for method in methods:
                if (path, method) in self.OPEN_TO_ANY_LOGIN:
                    continue
                if param.annotation is not scenarios_api.ScenarioAuthor:
                    unguarded.append(f"{method} {path}")
        assert unguarded == []

    def test_the_reads_left_open_still_require_a_login(self):
        for path, methods, param in endpoints(scenarios_api.router):
            for method in methods:
                if (path, method) in self.OPEN_TO_ANY_LOGIN:
                    assert param.default.dependency is not None

    def test_every_file_route_requires_an_author(self):
        """Reads included: the trees hold .env files, build scripts and
        Dockerfiles, and the only surface that reaches this API is already
        limited to the same two roles."""
        unguarded = []
        for path, methods, param in endpoints(files_api.router):
            for method in methods:
                if param.annotation is not files_api.ContentAuthor:
                    unguarded.append(f"{method} {path}")
        assert unguarded == []


class TestATraversingPathIsRefused:
    @pytest.fixture(autouse=True)
    def bases(self, tmp_path, monkeypatch):
        images = tmp_path / "images"
        images.mkdir()
        (tmp_path / "images-old").mkdir()
        (tmp_path / "scenarios").mkdir()
        monkeypatch.setattr(
            files_api,
            "get_allowed_bases",
            lambda: {"images": images, "scenarios": tmp_path / "scenarios"},
        )
        self.root = tmp_path

    def test_a_path_inside_the_base_resolves(self):
        full, base = files_api._resolve_path("images/web/Dockerfile")
        assert base == "images"
        assert full == self.root / "images" / "web" / "Dockerfile"

    def test_climbing_out_of_the_base_is_refused(self):
        with pytest.raises(HTTPException) as e:
            files_api._resolve_path("images/../../etc/passwd")
        assert e.value.status_code == 403

    def test_a_sibling_whose_name_starts_with_the_base_is_refused(self):
        """The old check compared strings, so "/data/images-old/x" passed for
        beginning with "/data/images". A path comparison refuses it."""
        with pytest.raises(HTTPException) as e:
            files_api._resolve_path("images/../images-old/secret.env")
        assert e.value.status_code == 403

    def test_a_symlink_pointing_out_of_the_base_is_refused(self):
        (self.root / "images" / "escape").symlink_to(self.root / "images-old")
        with pytest.raises(HTTPException) as e:
            files_api._resolve_path("images/escape/secret.env")
        assert e.value.status_code == 403

    def test_a_path_under_no_base_is_refused(self):
        with pytest.raises(HTTPException) as e:
            files_api._resolve_path("etc/passwd")
        assert e.value.status_code == 400

    def test_a_null_byte_is_refused_rather_than_raising(self):
        """Path() raises ValueError on a NUL, which reaches the client as 500."""
        with pytest.raises(HTTPException) as e:
            files_api._resolve_path("images/foo\x00.txt")
        assert e.value.status_code == 400


class TestATraversingScenarioIdIsRefused:
    @pytest.mark.parametrize(
        "bad",
        [
            "../../etc/cron.d/evil",
            "..",
            ".",
            "../sibling",
            "sub/dir",
            "..\\windows",
            ".hidden",
            "line\nbreak",
            "",
            "x" * 200,
        ],
    )
    def test_it_is_refused(self, bad):
        with pytest.raises(HTTPException) as e:
            scenarios_api._validated_scenario_id(bad)
        assert e.value.status_code == 400

    @pytest.mark.parametrize(
        "good", ["ransomware-attack", "seed_01", "a.b.c", "S1", "An Installed Seed"]
    )
    def test_an_id_that_names_one_file_is_accepted(self, good):
        """Containment, not a slug: ids come from the seed_id inside catalog
        content, and refusing an existing one would be a second defect."""
        assert scenarios_api._validated_scenario_id(good) == good

    def test_an_uploaded_seed_id_cannot_escape_the_directory(self, scenarios_dir):
        """seed_id is a field inside a file the uploader controls, and it names
        the file that gets written."""
        upload = UploadFile(
            file=io.BytesIO(b"seed_id: ../../etc/cron.d/evil\nname: x\nevents: []\n"),
            filename="evil.yaml",
        )
        with pytest.raises(HTTPException) as e:
            asyncio.run(scenarios_api.upload_scenario(current_user=ADMIN, file=upload))
        assert e.value.status_code == 400
        assert not (scenarios_dir.parent / "etc").exists()

    def test_a_traversing_filename_writes_inside_the_directory(self, scenarios_dir):
        """A multipart filename is a name, not a path, whatever the client sent."""
        upload = UploadFile(
            file=io.BytesIO(GOOD_SCENARIO.encode()),
            filename="../../../evil.yaml",
        )
        asyncio.run(scenarios_api.upload_scenario(current_user=ADMIN, file=upload))
        assert [p.name for p in scenarios_dir.glob("*.yaml")] == ["good-one.yaml"]


class TestOneBadFileDoesNotTakeTheListDown:
    def test_the_good_ones_still_list(self, scenarios_dir):
        (scenarios_dir / "good.yaml").write_text(GOOD_SCENARIO)
        (scenarios_dir / "unparseable.yaml").write_text("name: [unclosed\n")
        (scenarios_dir / "from-template.yaml").write_text(APP_TEMPLATE)

        response = scenarios_api.list_scenarios()

        assert [s.id for s in response.scenarios] == ["good-one"]
        assert response.total == 1

    def test_the_bad_ones_are_named_with_the_parsers_message(self, scenarios_dir):
        (scenarios_dir / "good.yaml").write_text(GOOD_SCENARIO)
        (scenarios_dir / "unparseable.yaml").write_text("name: [unclosed\n")
        (scenarios_dir / "from-template.yaml").write_text(APP_TEMPLATE)

        problems = {p.file: p.error for p in scenarios_api.list_scenarios().problems}

        assert set(problems) == {"unparseable.yaml", "from-template.yaml"}
        # The YAML parser's own words, not a generic failure.
        assert "expected" in problems["unparseable.yaml"].lower()
        # The template's defect, named by field rather than by exception type.
        assert "required_roles" in problems["from-template.yaml"]

    def test_an_empty_directory_reports_nothing_wrong(self, scenarios_dir):
        response = scenarios_api.list_scenarios()
        assert response.scenarios == []
        assert response.problems == []

    def test_a_filter_does_not_turn_other_categories_into_problems(self, scenarios_dir):
        """The filtered list is narrower than the directory, so the files that
        parsed have to be recounted without the filter before the leftovers can
        be called broken."""
        (scenarios_dir / "good.yaml").write_text(GOOD_SCENARIO)
        (scenarios_dir / "unparseable.yaml").write_text("name: [unclosed\n")

        response = scenarios_api.list_scenarios(category="red-team")

        assert response.scenarios == []
        assert [p.file for p in response.problems] == ["unparseable.yaml"]

    def test_the_manifest_is_not_a_broken_scenario(self, scenarios_dir):
        """The service skips manifest.yaml by name; reporting it as unreadable
        would be a permanent false alarm on any catalog-installed directory."""
        (scenarios_dir / "manifest.yaml").write_text("items: []\n")
        assert scenarios_api.list_scenarios().problems == []

    def test_fetching_a_broken_scenario_says_so_instead_of_not_found(self, scenarios_dir):
        """404 sends an author looking for a file they can see in the browser."""
        (scenarios_dir / "from-template.yaml").write_text(APP_TEMPLATE)

        with pytest.raises(HTTPException) as e:
            scenarios_api.get_scenario("from-template")

        assert e.value.status_code == 422
        assert "from-template.yaml" in e.value.detail

    def test_a_genuinely_missing_scenario_is_still_404(self, scenarios_dir):
        with pytest.raises(HTTPException) as e:
            scenarios_api.get_scenario("no-such-thing")
        assert e.value.status_code == 404


class TestAnUnusableUploadIsRefusedAtTheDoor:
    def test_the_app_template_is_refused_by_name(self, scenarios_dir):
        upload = UploadFile(file=io.BytesIO(APP_TEMPLATE.encode()), filename="from-template.yaml")

        with pytest.raises(HTTPException) as e:
            asyncio.run(scenarios_api.upload_scenario(current_user=ADMIN, file=upload))

        assert e.value.status_code == 400
        assert "required_roles" in e.value.detail
        assert list(scenarios_dir.glob("*.yaml")) == []

    def test_a_usable_upload_is_accepted(self, scenarios_dir):
        upload = UploadFile(file=io.BytesIO(GOOD_SCENARIO.encode()), filename="good.yaml")
        detail = asyncio.run(scenarios_api.upload_scenario(current_user=ADMIN, file=upload))
        assert detail.id == "good-one"
        assert detail.required_roles == ["defender"]

    @pytest.mark.parametrize(
        "document,expected",
        [
            ("- just\n- a list\n", "top level"),
            ("name: x\nevents: not-a-list\n", "events"),
            ("name: x\nevents:\n  - a string\n", "events"),
            ("name: x\nduration_minutes: four\n", "duration_minutes"),
            ("name: 7\n", "name"),
            # A key present and empty is not an absent key: the reader's
            # data.get(field, default) hands back the null, and the response
            # model refuses it. Each of these was written to the volume and
            # then refused, leaving a file nobody asked for.
            ("name: x\ndescription:\n", "description"),
            ("name: x\nrequired_roles:\n", "required_roles"),
            ("name: x\nevents:\n", "events"),
            ("seed_id:\nname: x\n", "seed_id"),
            ("name:\n", "name"),
            # The events list is the only part of a created scenario that is
            # not typed by ScenarioUpload, so its fields are checked one by one.
            ("name: x\nevents:\n  - title: 5\n", "title"),
            ("name: x\nevents:\n  - sequence: nope\n", "sequence"),
            ("name: x\nevents:\n  - actions: not-a-list\n", "actions"),
        ],
    )
    def test_shapes_the_list_could_not_render_are_named(self, document, expected):
        import yaml

        assert expected in scenarios_api._unusable_scenario_error(yaml.safe_load(document))

    def test_a_usable_document_passes(self):
        import yaml

        assert scenarios_api._unusable_scenario_error(yaml.safe_load(GOOD_SCENARIO)) is None

    @pytest.mark.parametrize(
        "document",
        [
            "seed_id: a\nname: n\ndescription:\nevents: []\n",
            "seed_id: b\nname: n\nevents:\n",
            "seed_id: c\nname: n\nrequired_roles:\nevents: []\n",
            "seed_id: d\nname: n\nevents:\n  - title: 5\n",
            "seed_id:\nname: n\nevents: []\n",
        ],
    )
    def test_a_refused_upload_leaves_nothing_behind(self, scenarios_dir, document):
        """A refusal that still writes the file is the defect wearing a 400.

        Each of these landed on the shared volume and was then reported back as
        a failure, so the uploader was told their scenario was rejected while
        everyone else's Training Scenarios page grew a problem entry for it.
        """
        upload = UploadFile(file=io.BytesIO(document.encode()), filename="x.yaml")

        with pytest.raises(HTTPException) as e:
            asyncio.run(scenarios_api.upload_scenario(current_user=ADMIN, file=upload))

        assert e.value.status_code == 400
        assert list(scenarios_dir.glob("*.yaml")) == []


class TestCreateAndUpdateRefuseBeforeTheyWrite:
    """`events` is a list of bare dicts, so the body's own type is not enough."""

    def body(self, events):
        return scenarios_api.ScenarioUpload(
            name="A Scenario",
            description="d",
            category="blue-team",
            difficulty="beginner",
            duration_minutes=5,
            required_roles=["defender"],
            events=events,
        )

    def test_create_refuses_a_bad_inject_and_writes_nothing(self, scenarios_dir):
        with pytest.raises(HTTPException) as e:
            scenarios_api.create_scenario(
                self.body([{"title": 5}]), current_user=ADMIN, scenario_id="made-up"
            )
        assert e.value.status_code == 400
        assert "title" in e.value.detail
        assert list(scenarios_dir.glob("*.yaml")) == []

    def test_update_refuses_a_bad_inject_without_damaging_the_file(self, scenarios_dir):
        (scenarios_dir / "good.yaml").write_text(GOOD_SCENARIO)

        with pytest.raises(HTTPException) as e:
            scenarios_api.update_scenario("good", self.body([{"sequence": "nope"}]), ADMIN)

        assert e.value.status_code == 400
        assert (scenarios_dir / "good.yaml").read_text() == GOOD_SCENARIO

    def test_a_good_inject_still_saves(self, scenarios_dir):
        detail = scenarios_api.create_scenario(
            self.body([{"sequence": 1, "delay_minutes": 5, "title": "t", "target_role": "r"}]),
            current_user=ADMIN,
            scenario_id="fine",
        )
        assert detail.id == "fine"
        assert detail.events[0].delay_minutes == 5
