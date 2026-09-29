# backend/proving_ground/services/scenario_filesystem.py
"""
Filesystem-based scenario service.

Scenarios are YAML files stored in data/scenarios/. No database required.

The API runs four uvicorn workers over one shared data volume, so this module
exists four times over and each copy reads files the other three wrote. Parsing
is memoised on the bytes that were parsed rather than on the file's timestamp,
which is what keeps the four copies from answering the same question
differently. See `_ScenarioMemo`.
"""

import hashlib
import logging
import os
import stat
import threading
import uuid
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Dict, Any, Tuple
from dataclasses import dataclass, field

import yaml

logger = logging.getLogger(__name__)

# Default scenarios directory - can be overridden via environment
SCENARIOS_DIR = Path(os.environ.get("SCENARIOS_DIR", "/data/scenarios"))


@dataclass
class ScenarioEvent:
    """A single event within a scenario."""

    sequence: int
    delay_minutes: int
    title: str
    target_role: str
    description: Optional[str] = None
    actions: List[dict] = field(default_factory=list)


@dataclass
class Scenario:
    """A training scenario loaded from YAML."""

    id: str  # filename without extension (e.g., "ransomware-attack")
    name: str
    description: str
    category: str
    difficulty: str
    duration_minutes: int
    required_roles: List[str]
    events: List[ScenarioEvent]
    file_path: str
    modified_at: datetime

    @property
    def event_count(self) -> int:
        return len(self.events)


# How many parsed scenarios one worker keeps. The memo exists to avoid re-parsing
# YAML, not to hold the directory, and a bound is what stops a worker that has
# been up for weeks accumulating an entry for every version of every file it has
# ever read. The previous cache had no bound and no eviction: it dropped a key
# only when that same file was asked for again, and a file the catalog has since
# uninstalled is never asked for again.
_MEMO_MAX_ENTRIES = 512

# The key is (digest of the bytes, mtime in nanoseconds) -- every input a parse
# is derived from.
_MemoKey = Tuple[str, int]


class _ScenarioMemo:
    """Parsed scenarios, keyed by everything the parse was derived from.

    The previous cache decided an entry was still good by comparing the file's
    current mtime to the mtime it had recorded. That is a claim about the
    filesystem rather than about the file, and it fails in two ways that four
    workers over one volume will reach. On a volume whose timestamps are coarse,
    a rewrite inside a single tick carries the mtime it already had, so the stale
    parse matches and is served indefinitely. On the NFS-backed ReadWriteMany
    volume the chart's values file calls for past one node, another client's
    `stat` may be answered from its attribute cache for as long as the mount
    allows, so a worker is told the file has not changed when it has. In both
    cases a worker serves a scenario that is not the one on disk, and which of
    the four answered the request decides what the user sees.

    Keying on the bytes removes the question. The caller has already opened and
    read the file -- which is also the operation NFS revalidates, where a bare
    `stat` is not -- so the key is the digest of what it read together with the
    mtime taken off that same open handle. Two workers that read identical bytes
    therefore hold identical scenarios, and no worker can serve one it did not
    read during this request. The memo is safe to be per-process precisely
    because nothing in its key comes from the process: it is a memo on a pure
    function of file content, not a record of what this worker believes.

    The digest is an identity check, not a security boundary. It answers "are
    these the bytes I parsed last time", and the bytes in hand are the ones the
    caller is about to act on either way.
    """

    def __init__(self) -> None:
        self._entries: "OrderedDict[str, Tuple[_MemoKey, Scenario]]" = OrderedDict()
        # FastAPI runs this module's callers from the anyio threadpool -- every
        # scenario route is a sync `def` -- so several threads per worker touch
        # this dict at once. The previous cache looked a key up and then deleted
        # it in a second statement, which raises KeyError and 500s the request
        # when another thread deletes it in between. The lock covers the dict
        # only; reading and parsing happen outside it, so a slow parse never
        # holds up the other threads.
        self._lock = threading.Lock()

    def get(self, file_path: Path, key: _MemoKey) -> Optional[Scenario]:
        """Return the scenario parsed from exactly these bytes, if it is held."""
        path_key = str(file_path)
        with self._lock:
            entry = self._entries.get(path_key)
            if entry is None or entry[0] != key:
                return None
            self._entries.move_to_end(path_key)
            return entry[1]

    def put(self, file_path: Path, key: _MemoKey, scenario: Scenario) -> None:
        """Hold a parse against the bytes and mtime it came from."""
        path_key = str(file_path)
        with self._lock:
            self._entries[path_key] = (key, scenario)
            self._entries.move_to_end(path_key)
            while len(self._entries) > _MEMO_MAX_ENTRIES:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        """Drop everything held. Never required for correctness -- see refresh_cache."""
        with self._lock:
            self._entries.clear()


_memo = _ScenarioMemo()


def _read_scenario_file(file_path: Path) -> Optional[Tuple[bytes, os.stat_result]]:
    """Read a scenario file, and the metadata belonging to the bytes that were read.

    One open, with the stat taken from that open file descriptor. A separate
    `Path.stat()` describes whatever is at the path at the moment it runs, which
    on a volume four workers write to need not be the file whose bytes are in
    hand: the memo would then key one version's parse against another version's
    mtime, and `modified_at` would name a write the caller never saw.
    """
    try:
        with open(file_path, "rb") as handle:
            raw = handle.read()
            stat_result = os.fstat(handle.fileno())
    except OSError as e:
        logger.error(f"Error reading scenario file {file_path}: {e}")
        return None
    return raw, stat_result


def _parse_scenario_yaml(file_path: Path, raw: bytes, modified_at: datetime) -> Optional[Scenario]:
    """Parse a scenario YAML document into a Scenario.

    The bytes and the timestamp are passed in rather than read here so that the
    document parsed is the same document the memo keyed, with no second trip to
    a filesystem another worker may have written to in between.
    """
    try:
        data = yaml.safe_load(raw)

        if not data:
            logger.warning(f"Empty scenario file: {file_path}")
            return None

        # Parse events
        events = []
        for event_data in data.get("events", []):
            events.append(
                ScenarioEvent(
                    sequence=event_data.get("sequence", 0),
                    delay_minutes=event_data.get("delay_minutes", 0),
                    title=event_data.get("title", ""),
                    description=event_data.get("description"),
                    target_role=event_data.get("target_role", ""),
                    actions=event_data.get("actions", []),
                )
            )

        # Use seed_id if present, otherwise use filename
        scenario_id = data.get("seed_id", file_path.stem)

        scenario = Scenario(
            id=scenario_id,
            name=data.get("name", scenario_id),
            description=data.get("description", ""),
            category=data.get("category", "red-team"),
            difficulty=data.get("difficulty", "intermediate"),
            duration_minutes=data.get("duration_minutes", 60),
            required_roles=data.get("required_roles", []),
            events=events,
            file_path=str(file_path),
            modified_at=modified_at,
        )

        return scenario

    except yaml.YAMLError as e:
        logger.error(f"Invalid YAML in scenario file {file_path}: {e}")
        return None
    except Exception as e:
        logger.error(f"Error parsing scenario file {file_path}: {e}")
        return None


def _load_scenario(file_path: Path) -> Optional[Scenario]:
    """Read and parse one scenario file, reusing an identical earlier parse.

    Every read of a scenario goes through here, so every read revalidates
    against the bytes currently on the volume. That is what makes the four API
    workers agree: none of them can answer from a parse whose source it has not
    just re-read, whatever the volume's timestamps say.

    The parse is the expensive half by three orders of magnitude -- reading and
    digesting fifty scenario files costs about a millisecond, parsing them costs
    several hundred -- so the memo buys back nearly all of the cost of never
    trusting a cached copy.
    """
    read = _read_scenario_file(file_path)
    if read is None:
        return None
    raw, stat_result = read

    key: _MemoKey = (hashlib.sha256(raw).hexdigest(), stat_result.st_mtime_ns)
    cached = _memo.get(file_path, key)
    if cached is not None:
        return cached

    scenario = _parse_scenario_yaml(file_path, raw, datetime.fromtimestamp(stat_result.st_mtime))
    if scenario is not None:
        _memo.put(file_path, key, scenario)
    return scenario


def get_scenarios_dir() -> Path:
    """Get the scenarios directory path."""
    return SCENARIOS_DIR


def list_scenarios(
    category: Optional[str] = None,
    difficulty: Optional[str] = None,
) -> List[Scenario]:
    """
    List all scenarios from the filesystem.

    Args:
        category: Filter by category (red-team, blue-team, insider-threat)
        difficulty: Filter by difficulty (beginner, intermediate, advanced)

    Returns:
        List of Scenario objects
    """
    scenarios = []
    scenarios_dir = get_scenarios_dir()

    if not scenarios_dir.exists():
        logger.warning(f"Scenarios directory not found: {scenarios_dir}")
        return []

    for file_path in sorted(scenarios_dir.glob("*.yaml")):
        if file_path.name == "manifest.yaml":
            continue  # Skip manifest file if present

        scenario = _load_scenario(file_path)
        if scenario:
            # Apply filters
            if category and scenario.category != category:
                continue
            if difficulty and scenario.difficulty != difficulty:
                continue
            scenarios.append(scenario)

    return scenarios


def get_scenario(scenario_id: str) -> Optional[Scenario]:
    """
    Get a specific scenario by ID.

    Args:
        scenario_id: The scenario ID (filename without extension or seed_id)

    Returns:
        Scenario object or None if not found
    """
    scenarios_dir = get_scenarios_dir()

    # Try direct filename match first
    file_path = scenarios_dir / f"{scenario_id}.yaml"
    if file_path.exists():
        return _load_scenario(file_path)

    # Fall back to scanning all files for matching seed_id
    for file_path in scenarios_dir.glob("*.yaml"):
        if file_path.name == "manifest.yaml":
            continue

        scenario = _load_scenario(file_path)
        if scenario and scenario.id == scenario_id:
            return scenario

    return None


def _write_scenario_file(file_path: Path, data: Dict[str, Any]) -> None:
    """Write a scenario document so that no reader can ever see half of it.

    Opening the destination and writing into it truncates the file first, and on
    a volume four API workers and two worker pods share, another process listing
    scenarios during that window reads a partial document. It becomes a problem
    entry attributed to a file its author did not break, and a document truncated
    on a mapping boundary can parse cleanly into a scenario that is missing its
    injects -- which is worse, because nothing reports it.

    Writing a temporary file and renaming it means a reader sees either the old
    document or the new one. The fsync is what makes that true across a node
    losing power rather than only across a concurrent read: the rename can
    otherwise reach the disk before the bytes do, leaving a scenario file that
    exists and is empty, on a volume the chart states has no backup.

    The temporary name is dotted and does not end in `.yaml`, so neither the
    scenario glob nor the file browser picks it up in the moment it exists.

    The dump is the safe one because the load is: `yaml.dump` serialises a value
    it has no standard tag for as `!!python/object:...`, which `yaml.safe_load`
    then refuses, so the writer could put a document on the volume that the
    reader -- every reader, on every worker -- reports as a broken scenario.
    `safe_dump` raises instead, and a refusal that leaves the previous document
    in place is the outcome to want.
    """
    temp_path = file_path.with_name(f".{file_path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with open(temp_path, "w") as handle:
            yaml.safe_dump(
                data, handle, default_flow_style=False, allow_unicode=True, sort_keys=False
            )
            handle.flush()
            os.fsync(handle.fileno())
        # A rename carries the temporary file's permissions onto the destination,
        # so replacing a file re-creates it at whatever this process's umask
        # allows. Writing in place kept the mode the file already had, and a
        # scenario installed from the catalog keeps the mode `shutil.copy2`
        # copied from the catalog's own file -- so without this, saving through
        # the UI silently widens a file somebody narrowed, on a volume every API
        # and worker pod mounts. The stat fails on a create, which is the case
        # where there is no earlier mode to keep. Neither call is worth failing a
        # save over: the document is already written, and refusing here would
        # leave the author unable to save at all over a permission bit.
        try:
            os.chmod(temp_path, stat.S_IMODE(os.stat(file_path).st_mode))
        except OSError:
            pass
        os.replace(temp_path, file_path)
    except BaseException:
        # A failed write must not leave the temporary file behind: it is invisible
        # to the list, so nothing would ever report it and nothing would clean it up.
        try:
            temp_path.unlink()
        except OSError:
            pass
        raise


def save_scenario(
    scenario_id: str,
    data: Dict[str, Any],
    overwrite: bool = False,
) -> Scenario:
    """
    Save a scenario to the filesystem.

    Args:
        scenario_id: The scenario ID (will be used as filename)
        data: The scenario data (dict matching YAML structure)
        overwrite: Whether to overwrite existing file

    Returns:
        The saved Scenario object

    Raises:
        FileExistsError: If file exists and overwrite=False
        ValueError: If scenario data is invalid
    """
    scenarios_dir = get_scenarios_dir()
    scenarios_dir.mkdir(parents=True, exist_ok=True)

    file_path = scenarios_dir / f"{scenario_id}.yaml"

    # Two authors creating the same id at the same instant still race here, as
    # they did before: this asks whether the file exists and writes afterwards.
    # The window is unchanged and the outcome is last-writer-wins on a complete
    # document, which is why it is left alone rather than papered over -- closing
    # it needs an exclusive create, and that is a separate change with its own
    # behaviour to argue about.
    if file_path.exists() and not overwrite:
        raise FileExistsError(f"Scenario '{scenario_id}' already exists")

    # Validate required fields
    required_fields = [
        "name",
        "description",
        "category",
        "difficulty",
        "duration_minutes",
        "required_roles",
        "events",
    ]
    for required in required_fields:
        if required not in data:
            raise ValueError(f"Missing required field: {required}")

    # Add seed_id if not present
    if "seed_id" not in data:
        data["seed_id"] = scenario_id

    _write_scenario_file(file_path, data)

    # Read the file back rather than returning the document that was written.
    # The memo keys on content, so this parses fresh without anything having to
    # remember to invalidate it -- and the caller is handed the scenario as every
    # other worker will read it, not as this one meant it.
    scenario = _load_scenario(file_path)

    if scenario:
        logger.info(f"Saved scenario: {scenario_id}")
        return scenario
    else:
        raise ValueError("Failed to parse saved scenario")


def delete_scenario(scenario_id: str) -> bool:
    """
    Delete a scenario from the filesystem.

    Args:
        scenario_id: The scenario ID

    Returns:
        True if deleted, False if not found
    """
    scenarios_dir = get_scenarios_dir()
    file_path = scenarios_dir / f"{scenario_id}.yaml"

    if not file_path.exists():
        # Try to find by seed_id
        for fp in scenarios_dir.glob("*.yaml"):
            scenario = _load_scenario(fp)
            if scenario and scenario.id == scenario_id:
                file_path = fp
                break
        else:
            return False

    try:
        file_path.unlink()
        logger.info(f"Deleted scenario: {scenario_id}")
        return True
    except OSError as e:
        logger.error(f"Failed to delete scenario {scenario_id}: {e}")
        return False


def refresh_cache():
    """Drop this worker's parsed scenarios.

    Nothing needs this to see a change. Every read re-opens the file and keys its
    parse on the bytes it just read, so a scenario written by another worker, by
    a worker pod installing a catalog item, or by hand on the volume is picked up
    by the next request that asks for it.

    That is worth stating because the endpoint in front of this reaches exactly
    one of the four API workers -- whichever one answered the request -- and
    always did. It was never able to keep the promise its name makes. It is
    harmless now rather than misleading: this only frees memory, and the
    correctness it used to be needed for no longer depends on anyone calling it.
    """
    _memo.clear()
    logger.info("Scenario parse memo dropped for this worker")


def scenario_to_dict(scenario: Scenario, include_events: bool = False) -> Dict[str, Any]:
    """Convert a Scenario to a dictionary for API response.

    The lists are copied rather than handed out. The Scenario passed in is the
    memo's own object, so a caller that appended to `required_roles` or to an
    inject's `actions` would be editing the parsed copy in place -- and because
    the memo is keyed on the bytes of the file, re-reading the file cannot undo
    it. That is this module's four-worker defect arriving by a different door:
    one worker would answer with a scenario no file on the volume describes,
    for as long as that worker lives, and which worker took the request would
    again decide what the user saw.
    """
    result = {
        "id": scenario.id,
        "name": scenario.name,
        "description": scenario.description,
        "category": scenario.category,
        "difficulty": scenario.difficulty,
        "duration_minutes": scenario.duration_minutes,
        "event_count": scenario.event_count,
        "required_roles": list(scenario.required_roles),
        "modified_at": scenario.modified_at.isoformat(),
    }

    if include_events:
        result["events"] = [
            {
                "sequence": e.sequence,
                "delay_minutes": e.delay_minutes,
                "title": e.title,
                "description": e.description,
                "target_role": e.target_role,
                "actions": list(e.actions),
            }
            for e in scenario.events
        ]

    return result
