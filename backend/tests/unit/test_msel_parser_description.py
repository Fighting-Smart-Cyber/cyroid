# backend/tests/unit/test_msel_parser_description.py
"""An inject's description survives every Actions heading the product documents.

Only "**Actions:**" was recognised. The import dialog's own example writes "Actions:" without the
bold, so an author following the in-product instructions got injects with empty bodies and no
indication that anything had been dropped.
"""

from proving_ground.services.msel_parser import MSELParser


def _first(content: str) -> dict:
    injects = MSELParser().parse(content)
    assert injects, "expected the section to parse into one inject"
    return injects[0]


def test_bold_actions_heading_bounds_the_description():
    inject = _first("""# MSEL

## T+0:00 - Convoy Departs
The convoy leaves the staging area.

**Actions:**
- Run command on WS-01: whoami
""")
    assert inject["description"] == "The convoy leaves the staging area."


def test_plain_actions_heading_bounds_the_description():
    inject = _first("""# MSEL

## T+0:00 - Convoy Departs
The convoy leaves the staging area.

Actions:
- Run command on WS-01: whoami
""")
    assert inject["description"] == "The convoy leaves the staging area."


def test_description_stops_at_the_first_action_when_there_is_no_heading():
    inject = _first("""# MSEL

## T+0:00 - Convoy Departs
The convoy leaves the staging area.

- Run command on WS-01: whoami
""")
    assert inject["description"] == "The convoy leaves the staging area."
    assert len(inject["actions"]) == 1


def test_a_section_with_no_actions_keeps_its_whole_body():
    inject = _first("""# MSEL

## T+0:15 - Situation Report
No system action. The instructor briefs the team.
""")
    assert inject["description"] == "No system action. The instructor briefs the team."
    assert inject["actions"] == []


def test_a_section_with_no_body_has_an_empty_description():
    inject = _first("""# MSEL

## T+0:00 - Exercise Start
**Actions:**
- Run command on WS-01: date
""")
    assert inject["description"] == ""
