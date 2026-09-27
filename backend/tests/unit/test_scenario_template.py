"""The app's own "Training Scenario" template has to produce a usable scenario.

The template in `CreateFileModal.tsx` is the only example of a scenario most
users will ever see, so it is the documentation as well as the starting point.
It used to emit fields the parser does not read -- `duration_hours`,
`time_offset`, and `required_roles` as a list of mappings -- so a file created
from the product's own template was refused by the product's own API, and one
such file was enough to blank the Training Scenarios page and the range
builder's scenario picker for everyone.

Reading the template out of the component is the point: a test with its own
copy of the YAML passes happily while the dialog writes something else.
"""

import asyncio
import io
import re
from pathlib import Path

import pytest
import yaml
from fastapi import UploadFile

from proving_ground.api import scenarios as scenarios_api
from proving_ground.services import scenario_filesystem as fs

# The upload route never reads the caller; the role check is the dependency, and these tests are
# about the document rather than about who sent it.
AUTHOR = object()

MODAL = (
    Path(__file__).resolve().parents[3]
    / "frontend"
    / "src"
    / "components"
    / "files"
    / "CreateFileModal.tsx"
)


def template_text() -> str:
    """The template string the dialog writes, taken from the dialog."""
    if not MODAL.exists():
        pytest.skip(f"{MODAL} is not in this checkout")
    source = MODAL.read_text()
    match = re.search(r"const SCENARIO_TEMPLATE = `(.*?)`;", source, re.DOTALL)
    assert match, "CreateFileModal.tsx no longer declares SCENARIO_TEMPLATE as one literal"
    body = match.group(1)
    # A JavaScript template literal can escape characters; this one must not, or what the
    # browser writes to disk is not what this test parses.
    assert "\\" not in body, "the scenario template must contain no escape sequences"
    return body


@pytest.fixture
def scenarios_dir(tmp_path, monkeypatch):
    """Point the scenario service at an empty directory and drop its cache."""
    monkeypatch.setattr(fs, "SCENARIOS_DIR", tmp_path)
    fs.refresh_cache()
    yield tmp_path
    fs.refresh_cache()


class TestTheTemplateIsAUsableScenario:
    def test_uploading_it_is_accepted(self, scenarios_dir):
        """The upload route refuses a document the list could not render, by name."""
        upload = UploadFile(file=io.BytesIO(template_text().encode()), filename="scenario.yaml")

        detail = asyncio.run(scenarios_api.upload_scenario(current_user=AUTHOR, file=upload))

        assert detail.id == "new-scenario"
        assert detail.required_roles == ["defender", "target"]

    def test_it_declares_roles_as_names_not_mappings(self):
        """The defect itself: `required_roles` was a list of {role, description}."""
        data = yaml.safe_load(template_text())
        assert data["required_roles"]
        assert all(isinstance(role, str) for role in data["required_roles"])

    def test_every_event_addresses_a_declared_role(self):
        data = yaml.safe_load(template_text())
        declared = set(data["required_roles"])
        assert {event["target_role"] for event in data["events"]} <= declared

    def test_a_file_created_from_it_appears_in_the_list(self, scenarios_dir):
        (scenarios_dir / "scenario.yaml").write_text(template_text())

        response = scenarios_api.list_scenarios()

        assert response.problems == []
        assert [s.id for s in response.scenarios] == ["new-scenario"]

    def test_the_listed_scenario_carries_what_the_page_draws(self, scenarios_dir):
        (scenarios_dir / "scenario.yaml").write_text(template_text())

        listed = scenarios_api.list_scenarios().scenarios[0]

        assert listed.category in {"red-team", "blue-team", "insider-threat"}
        assert listed.difficulty in {"beginner", "intermediate", "advanced"}
        assert listed.duration_minutes > 0
        assert listed.event_count == 3

    def test_its_events_survive_the_detail_response(self, scenarios_dir):
        (scenarios_dir / "scenario.yaml").write_text(template_text())

        detail = scenarios_api.get_scenario("new-scenario")

        sequences = [e.sequence for e in detail.events]
        assert sequences == sorted(sequences)
        assert all(e.title for e in detail.events)
        # Zero is a meaningful offset -- the first inject fires at T+0 -- so the field has to be
        # present rather than defaulted away by the parser.
        assert detail.events[0].delay_minutes == 0
