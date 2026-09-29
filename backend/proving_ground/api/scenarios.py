# backend/proving_ground/api/scenarios.py
"""
Scenarios API endpoints for training scenarios.

Scenarios are read directly from YAML files in data/scenarios/.
No database required - files are immediately visible when added.
"""

import re
from pathlib import Path
from typing import Annotated, Any, List, Optional

import yaml
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel, ValidationError

from proving_ground.api.deps import get_current_user, require_any_role
from proving_ground.models.user import User
from proving_ground.services.scenario_filesystem import (
    Scenario,
    list_scenarios as fs_list_scenarios,
    get_scenario as fs_get_scenario,
    save_scenario as fs_save_scenario,
    delete_scenario as fs_delete_scenario,
    refresh_cache,
    scenario_to_dict,
    get_scenarios_dir,
)

router = APIRouter(prefix="/scenarios", tags=["scenarios"])

# Scenarios are files on the data volume every API and worker pod mounts, owned
# by nobody: there is no owner row for check_resource_control to compare a
# caller against, and no range whose visibility check_resource_access could
# apply. Authoring a scenario is a content-development act, so the role is the
# check, and it is the same pair the Training Scenarios page is gated to.
#
# Reads stay authenticated-only on purpose. A scenario definition is training
# design -- name, category, difficulty, the inject timeline -- and the range
# builder's scenario picker shows it to every evaluator and engineer who can
# open a range. Nothing in it is a credential, so a login is the right bar.
ScenarioAuthor = Annotated[User, Depends(require_any_role("admin", "engineer"))]

# A scenario id becomes a filename under the scenarios directory, so anything
# that could name a file elsewhere is refused: a separator, a leading dot (which
# covers "." and ".." and stops an id writing a file the list never shows), and
# the control characters a filename has no business carrying.
_UNSAFE_IN_ID = re.compile(r"[/\\\x00-\x1f\x7f]")
_MAX_SCENARIO_ID = 128


def _validated_scenario_id(scenario_id: str) -> str:
    """Refuse a scenario id that would escape the scenarios directory.

    The id arrives from three places and is interpolated straight into a path.
    Two of them never pass through the router: the `scenario_id` query
    parameter on create, which wrote "../../escaped.yaml" outside the
    directory, and the seed_id inside an uploaded document, which the uploader
    chooses. The path parameter is the mildest -- a separator splits into
    segments the route cannot match -- but a bare ".." still reaches the
    handler, and one rule applied to all three is cheaper to keep true than
    three arguments about which source is safe.

    The rule is containment rather than a slug on purpose. Ids come from the
    seed_id inside files the catalog installs, and a stricter shape would refuse
    to open or delete existing content that has never been a danger -- turning a
    traversal fix into a second defect.
    """
    # The type is checked here and not only at the call sites: one of the three
    # sources is a YAML field, where `seed_id: 7` is an int, and len() on it
    # would be a bare 500 rather than a refusal.
    if not isinstance(scenario_id, str):
        raise HTTPException(status_code=400, detail="Scenario id must be text")
    if not scenario_id or len(scenario_id) > _MAX_SCENARIO_ID:
        raise HTTPException(
            status_code=400,
            detail=f"Scenario id must be 1-{_MAX_SCENARIO_ID} characters",
        )
    if scenario_id.startswith(".") or _UNSAFE_IN_ID.search(scenario_id):
        raise HTTPException(
            status_code=400,
            detail="Scenario id must name a single file: no path separators, and no leading '.'",
        )
    return scenario_id


# Response models
class ScenarioEventResponse(BaseModel):
    sequence: int
    delay_minutes: int
    title: str
    description: Optional[str] = None
    target_role: str
    actions: List[dict] = []


class ScenarioListItem(BaseModel):
    id: str
    name: str
    description: str
    category: str
    difficulty: str
    duration_minutes: int
    event_count: int
    required_roles: List[str]
    modified_at: str


class ScenarioDetail(ScenarioListItem):
    events: List[ScenarioEventResponse]


class ScenarioUpload(BaseModel):
    name: str
    description: str
    category: str
    difficulty: str
    duration_minutes: int
    required_roles: List[str]
    events: List[dict]


class ScenarioProblem(BaseModel):
    """One file in the scenarios directory that could not be read as a scenario."""

    file: str
    error: str


class ScenariosListResponse(BaseModel):
    scenarios: List[ScenarioListItem]
    total: int
    problems: List[ScenarioProblem] = []
    # `scenarios_dir` used to carry the API pod's absolute path here, to every authenticated
    # caller. It named nothing a user can act on -- the path is inside a container -- and it
    # described the deployment's filesystem layout to anyone who asked. The file browser works
    # in relative paths, so nothing needed it.


def _validation_message(exc: ValidationError) -> str:
    """Turn a pydantic error into one line naming the fields that are wrong."""
    parts = []
    for err in exc.errors()[:3]:
        location = ".".join(str(p) for p in err["loc"]) or "(document)"
        parts.append(f"{location}: {err['msg']}")
    return "; ".join(parts)


def _parse_failure(file_path: Path) -> str:
    """Say why the scenario service could not read this file.

    The service logs the reason and returns None, so by the time the API has a
    list the bad file is simply absent. Re-reading it here is what lets the
    response name it and quote the parser instead of leaving a user to guess
    which of their files is the broken one.
    """
    try:
        with open(file_path, "r") as f:
            data = yaml.safe_load(f)
    except yaml.YAMLError as e:
        return " ".join(str(e).split())
    except UnicodeDecodeError:
        # Opening in text mode decodes, and a file that is not UTF-8 raises here
        # rather than from the parser. Uncaught it escaped this helper into the
        # list route, so one file saved in another encoding -- which the scenario
        # service itself simply skips -- turned the whole Training Scenarios page
        # into a 500 for everyone.
        return "the file is not valid UTF-8 text"
    except OSError as e:
        return f"cannot be read: {e.strerror}"

    if data is None:
        return "the file is empty"
    if not isinstance(data, dict):
        return f"the top level is a {type(data).__name__}, not a mapping of scenario fields"

    events = data.get("events")
    if events is not None and not isinstance(events, list):
        return f"'events' is a {type(events).__name__}, not a list"
    if isinstance(events, list) and any(not isinstance(e, dict) for e in events):
        return "every entry under 'events' must be a mapping"

    return "the file parses as YAML but could not be read as a scenario"


def _inject_error(event: dict) -> Optional[str]:
    """Check one inject against the model the detail response is built from.

    The reader supplies 0 for a missing sequence or delay and "" for a missing
    title or role, so an absent key is never what breaks the response -- a
    present key of the wrong type is. Validating through the response model
    itself, with the reader's own defaults, keeps this check from drifting away
    from the thing it is predicting.
    """
    try:
        ScenarioEventResponse(
            sequence=event.get("sequence", 0),
            delay_minutes=event.get("delay_minutes", 0),
            title=event.get("title", ""),
            description=event.get("description"),
            target_role=event.get("target_role", ""),
            actions=event.get("actions", []),
        )
    except ValidationError as e:
        return _validation_message(e)
    return None


def _unusable_scenario_error(data: Any) -> Optional[str]:
    """Reject a document whose shape the list or detail response would choke on.

    These are exactly the fields whose type the response models fix. Checking
    them before the file is written means a bad write is refused with a reason
    the author can act on, rather than landing on the shared volume and turning
    into a problem entry for everyone else -- a refusal that leaves the file
    behind is the worst of both.

    Presence is tested with `in`, not against None, because the reader reads
    every field as `data.get(field, default)`: a key that is present and empty
    yields the null rather than the default, so "description:" with nothing
    after it is not an absent field but a None the response model refuses.
    """
    if not isinstance(data, dict):
        return f"the top level is a {type(data).__name__}, not a mapping of scenario fields"

    for field in ("seed_id", "name", "description", "category", "difficulty"):
        if field in data and not isinstance(data[field], str):
            return f"'{field}' must be text, not a {type(data[field]).__name__}"

    if "duration_minutes" in data:
        duration = data["duration_minutes"]
        if isinstance(duration, bool) or not isinstance(duration, int):
            return (
                "'duration_minutes' must be a whole number of minutes, "
                f"not a {type(duration).__name__}"
            )

    if "required_roles" in data:
        roles = data["required_roles"]
        if not isinstance(roles, list):
            return f"'required_roles' must be a list of role names, not a {type(roles).__name__}"
        if any(not isinstance(r, str) for r in roles):
            return "'required_roles' must be a list of role names, not a list of mappings"

    if "events" in data:
        events = data["events"]
        if not isinstance(events, list):
            return f"'events' must be a list, not a {type(events).__name__}"
        if any(not isinstance(e, dict) for e in events):
            return "every entry under 'events' must be a mapping"
        for index, event in enumerate(events):
            error = _inject_error(event)
            if error:
                return f"inject {index + 1}: {error}"

    return None


def _detail_response(scenario: Scenario) -> ScenarioDetail:
    """Build the detail response, or refuse with the field that is wrong.

    A scenario the service parsed can still carry values the response model
    will not accept -- required_roles as a list of mappings is the one the
    product's own file template produces. Without this the caller gets a bare
    500 with the reason only in the API log.
    """
    try:
        return ScenarioDetail(**scenario_to_dict(scenario, include_events=True))
    except ValidationError as e:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Scenario '{Path(scenario.file_path).name}' is not usable: "
                f"{_validation_message(e)}"
            ),
        ) from e


@router.get("")
def list_scenarios(
    category: Optional[str] = None,
    difficulty: Optional[str] = None,
    current_user: User = Depends(get_current_user),
) -> ScenariosListResponse:
    """
    List all available training scenarios.

    Scenarios are read directly from YAML files in the scenarios directory.
    No restart required - new files are immediately visible.

    Args:
        category: Filter by category (red-team, blue-team, insider-threat)
        difficulty: Filter by difficulty (beginner, intermediate, advanced)

    Returns:
        List of scenarios without event details, plus any files that could not
        be read as one.
    """
    scenarios = fs_list_scenarios(category=category, difficulty=difficulty)

    # One unusable file used to take the whole page down: the list was built in
    # a comprehension, so the first scenario whose fields did not match the
    # response model raised and the caller got a 500 with no idea which file was
    # at fault. Anyone who can write to the scenarios directory could therefore
    # blank the Training Scenarios page and the range builder's scenario picker
    # for everyone. A bad file is now one named entry beside the good ones.
    items: List[ScenarioListItem] = []
    problems: List[ScenarioProblem] = []
    readable = set()

    for scenario in scenarios:
        name = Path(scenario.file_path).name
        readable.add(name)
        try:
            items.append(ScenarioListItem(**scenario_to_dict(scenario, include_events=False)))
        except ValidationError as e:
            problems.append(ScenarioProblem(file=name, error=_validation_message(e)))

    problems.extend(_unreadable_files(readable, filtered=bool(category or difficulty)))

    return ScenariosListResponse(
        scenarios=items,
        total=len(items),
        problems=problems,
    )


def _unreadable_files(readable: set, filtered: bool) -> List[ScenarioProblem]:
    """Name the YAML files in the scenarios directory that never became scenarios.

    The service drops an unparseable file silently, so it is absent from the
    list rather than reported. Everything on disk that did not come back is
    therefore a file somebody meant as a scenario and cannot see.

    When a category or difficulty filter is in play the caller's own list is
    narrower than the directory, so the set of files that parsed is recomputed
    without the filters -- otherwise every scenario of another category would be
    reported as broken.
    """
    if filtered:
        readable = {Path(s.file_path).name for s in fs_list_scenarios()}

    scenarios_dir = get_scenarios_dir()
    if not scenarios_dir.exists():
        return []

    problems = []
    for file_path in sorted(scenarios_dir.glob("*.yaml")):
        if file_path.name == "manifest.yaml" or file_path.name in readable:
            continue
        problems.append(ScenarioProblem(file=file_path.name, error=_parse_failure(file_path)))
    return problems


@router.get("/{scenario_id}")
def get_scenario(
    scenario_id: str,
    current_user: User = Depends(get_current_user),
) -> ScenarioDetail:
    """
    Get a scenario with full event details.

    Args:
        scenario_id: ID of the scenario (filename without extension or seed_id)

    Returns:
        Full scenario details including all events.
    """
    scenario_id = _validated_scenario_id(scenario_id)
    scenario = fs_get_scenario(scenario_id)
    if not scenario:
        # A file that exists but will not parse is not a missing scenario, and
        # saying "not found" about a file the user can see in the browser sends
        # them looking for the wrong problem.
        file_path = get_scenarios_dir() / f"{scenario_id}.yaml"
        if file_path.exists():
            raise HTTPException(
                status_code=422,
                detail=f"Scenario '{file_path.name}' could not be read: {_parse_failure(file_path)}",
            )
        raise HTTPException(status_code=404, detail="Scenario not found")

    return _detail_response(scenario)


@router.post("")
def create_scenario(
    scenario: ScenarioUpload,
    current_user: ScenarioAuthor,
    scenario_id: Optional[str] = None,
) -> ScenarioDetail:
    """
    Create a new scenario by saving a YAML file.

    Args:
        scenario: Scenario data
        scenario_id: Optional ID (defaults to slugified name)

    Returns:
        The created scenario.
    """
    # Generate ID from name if not provided
    if not scenario_id:
        scenario_id = scenario.name.lower().replace(" ", "-").replace("_", "-")
        # Remove non-alphanumeric characters except hyphens
        scenario_id = "".join(c for c in scenario_id if c.isalnum() or c == "-")

    # A name of nothing but punctuation slugifies to an empty id, which would
    # write ".yaml" -- a hidden file the list never shows.
    scenario_id = _validated_scenario_id(scenario_id)

    data = {
        "name": scenario.name,
        "description": scenario.description,
        "category": scenario.category,
        "difficulty": scenario.difficulty,
        "duration_minutes": scenario.duration_minutes,
        "required_roles": scenario.required_roles,
        "events": scenario.events,
    }

    # Every field but `events` is typed by ScenarioUpload; `events` is a list of
    # bare dicts, so an inject whose title is a number reaches the file. Refusing
    # before the write is what stops a rejected create from leaving a scenario
    # behind that lists as healthy and 422s when anyone opens it.
    shape_error = _unusable_scenario_error(data)
    if shape_error:
        raise HTTPException(status_code=400, detail=f"Not a usable scenario: {shape_error}")

    try:
        saved = fs_save_scenario(scenario_id, data, overwrite=False)
    except FileExistsError as exc:
        raise HTTPException(
            status_code=409, detail=f"Scenario '{scenario_id}' already exists. Use PUT to update."
        ) from exc
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return _detail_response(saved)


@router.put("/{scenario_id}")
def update_scenario(
    scenario_id: str,
    scenario: ScenarioUpload,
    current_user: ScenarioAuthor,
) -> ScenarioDetail:
    """
    Update an existing scenario.

    Args:
        scenario_id: ID of the scenario to update
        scenario: Updated scenario data

    Returns:
        The updated scenario.
    """
    scenario_id = _validated_scenario_id(scenario_id)

    # A file that exists but will not parse is precisely the one an author needs
    # to overwrite, and the service reports it as absent. Refusing with 404 would
    # leave the API able to report a broken scenario and unable to repair it.
    existing = fs_get_scenario(scenario_id)
    if not existing and not (get_scenarios_dir() / f"{scenario_id}.yaml").exists():
        raise HTTPException(status_code=404, detail="Scenario not found")

    data = {
        "name": scenario.name,
        "description": scenario.description,
        "category": scenario.category,
        "difficulty": scenario.difficulty,
        "duration_minutes": scenario.duration_minutes,
        "required_roles": scenario.required_roles,
        "events": scenario.events,
    }

    # An update is the repair path for a file that is already broken, so it is
    # the last place that should be allowed to write a new broken one.
    shape_error = _unusable_scenario_error(data)
    if shape_error:
        raise HTTPException(status_code=400, detail=f"Not a usable scenario: {shape_error}")

    try:
        saved = fs_save_scenario(scenario_id, data, overwrite=True)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return _detail_response(saved)


@router.delete("/{scenario_id}")
def delete_scenario(
    scenario_id: str,
    current_user: ScenarioAuthor,
):
    """
    Delete a scenario.

    Args:
        scenario_id: ID of the scenario to delete

    Returns:
        Success message.
    """
    scenario_id = _validated_scenario_id(scenario_id)

    if not fs_delete_scenario(scenario_id):
        raise HTTPException(status_code=404, detail="Scenario not found")

    return {"message": f"Scenario '{scenario_id}' deleted"}


@router.post("/upload")
async def upload_scenario(
    current_user: ScenarioAuthor,
    file: UploadFile = File(...),
    overwrite: bool = Form(default=False),
) -> ScenarioDetail:
    """
    Upload a scenario YAML file.

    Args:
        file: YAML file to upload
        overwrite: Whether to overwrite if exists

    Returns:
        The uploaded scenario.
    """
    if not file.filename or not file.filename.endswith((".yaml", ".yml")):
        raise HTTPException(status_code=400, detail="File must be a YAML file (.yaml or .yml)")

    try:
        content = await file.read()
        data = yaml.safe_load(content.decode("utf-8"))
    except yaml.YAMLError as e:
        raise HTTPException(status_code=400, detail=f"Invalid YAML: {e}") from e
    except UnicodeDecodeError as exc:
        raise HTTPException(status_code=400, detail="File must be UTF-8 encoded") from exc

    if not data:
        raise HTTPException(status_code=400, detail="Empty YAML file")

    # Refuse here rather than after the write. The file lands on the volume every
    # range reads from, so a shape the list endpoint cannot render would become
    # everyone else's problem entry, reported against a filename they did not
    # choose.
    shape_error = _unusable_scenario_error(data)
    if shape_error:
        raise HTTPException(status_code=400, detail=f"Not a usable scenario: {shape_error}")

    # Extract scenario ID from filename or seed_id. Both are attacker-chosen --
    # the seed_id is a field inside the uploaded document, and a filename in a
    # multipart part is whatever the client sent, ".." included.
    scenario_id = data.get("seed_id") or Path(file.filename).name.rsplit(".", 1)[0]
    scenario_id = _validated_scenario_id(scenario_id)

    try:
        saved = fs_save_scenario(scenario_id, data, overwrite=overwrite)
    except FileExistsError as exc:
        raise HTTPException(
            status_code=409,
            detail=f"Scenario '{scenario_id}' already exists. Set overwrite=true to replace.",
        ) from exc
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return _detail_response(saved)


@router.post("/refresh")
def refresh_scenarios(
    current_user: ScenarioAuthor,
):
    """
    Re-read the scenarios directory and report what is in it.

    This used to be the way to make a change on the volume visible, and it could
    not be: the API serves from four worker processes, a request reaches exactly
    one of them, and each held its own parsed copies. Pressing it cleared a
    quarter of the install and reported success for all of it, so the next list
    request had three chances in four of looking unchanged -- which reads as the
    button not working rather than as the button not being able to work.

    The scenario service no longer holds a copy it has not re-read, so there is
    nothing left to go stale and nothing here that has to reach the other three
    workers. What the route still does honestly is answer what is on the volume
    now, which is what anyone pressing it actually wanted to know.

    Returns:
        The scenarios currently readable from the scenarios directory.
    """
    refresh_cache()
    scenarios = fs_list_scenarios()

    return {
        "message": f"Scenarios re-read from disk: {len(scenarios)} found",
        "total": len(scenarios),
        "scenarios": [s.id for s in scenarios],
    }
