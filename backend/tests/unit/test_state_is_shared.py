"""What one API worker knows about scenarios, the other three must know too.

The chart serves the API with `uvicorn --workers 4` over one shared data
volume, so `services/scenario_filesystem` exists four times and each copy reads
files the other three wrote. A request reaches exactly one of them, and nothing
routes a user back to the same one twice.

The cache that used to sit in that module decided a parsed copy was still good
by comparing the file's mtime against the mtime it had recorded. That test is a
claim about the filesystem rather than about the file. It holds on a local disk
with nanosecond timestamps and fails on the two volumes this product actually
runs on: a volume whose timestamps are coarse gives a rewrite the mtime it
already had, and the NFS-backed ReadWriteMany volume the chart's values file
calls for past one node can answer another client's `stat` from an attribute
cache. In both cases a worker serves a scenario that is not the one on disk,
and which worker answered decides what the user sees -- a scenario edited and
then re-opened comes back either old or new depending on nothing the user can
see or influence.

`POST /scenarios/refresh` could not rescue it. That request also reaches one
worker of four, so it cleared a quarter of the install and reported success for
all of it.

The fix is that no read answers from a copy whose source it has not just
re-read: the parse is memoised on the digest of the bytes it was parsed from,
together with the mtime off the same open handle. These tests hold that
property, including the two cases the mtime test got wrong. Per-process is then
safe by construction -- nothing in the key comes from the process, so two
workers that read the same bytes hold the same scenario, and neither can hold
one it did not read.
"""

import os
import stat
import threading

import pytest
import yaml

from proving_ground.services import scenario_filesystem as fs


def scenario_document(name: str, injects: int = 2, category: str = "red-team") -> dict:
    """A scenario document the service parses and the API would accept."""
    return {
        "name": name,
        "description": f"{name} description",
        "category": category,
        "difficulty": "intermediate",
        "duration_minutes": 60,
        "required_roles": ["operator"],
        "events": [
            {
                "sequence": i,
                "delay_minutes": i * 5,
                "title": f"{name} inject {i}",
                "description": "narrative",
                "target_role": "operator",
                "actions": [],
            }
            for i in range(injects)
        ],
    }


def write_file(directory, scenario_id: str, document: dict) -> "os.PathLike":
    """Write a scenario straight onto the volume, as another worker would."""
    path = directory / f"{scenario_id}.yaml"
    path.write_text(yaml.dump(document, default_flow_style=False, sort_keys=False))
    return path


@pytest.fixture
def scenarios_dir(tmp_path, monkeypatch):
    """Point the scenario service at an empty directory with nothing memoised."""
    monkeypatch.setattr(fs, "SCENARIOS_DIR", tmp_path)
    fs.refresh_cache()
    yield tmp_path
    fs.refresh_cache()


class TestAWriteIsVisibleToTheNextRead:
    """The user-visible promise: what is on the volume is what comes back."""

    def test_a_scenario_written_after_a_read_is_visible_to_the_next_read(self, scenarios_dir):
        assert fs.list_scenarios() == []

        write_file(scenarios_dir, "late-arrival", scenario_document("Late Arrival"))

        found = fs.list_scenarios()
        assert [s.id for s in found] == ["late-arrival"]
        assert fs.get_scenario("late-arrival").name == "Late Arrival"

    def test_a_rewrite_is_visible_to_the_next_read(self, scenarios_dir):
        write_file(scenarios_dir, "edited", scenario_document("First Name"))
        assert fs.get_scenario("edited").name == "First Name"

        write_file(scenarios_dir, "edited", scenario_document("Second Name"))

        assert fs.get_scenario("edited").name == "Second Name"

    def test_a_rewrite_that_keeps_the_files_timestamp_is_still_visible(self, scenarios_dir):
        """The failure the mtime test could not see, and the reason for the digest.

        A volume with coarse timestamps gives a rewrite inside one tick the mtime
        it already had. Setting the timestamp back is how that is reproduced here
        without needing such a volume, and it is also what a restore, an rsync
        that preserves times, or a catalog install writing a fixed mtime does.

        The mtime cache compared equal, decided the file had not changed and
        served the previous parse -- for the life of the worker, since nothing
        else would ever invalidate it. The next request landing on a different
        worker would show the new document, so the same scenario read twice gave
        two different answers.
        """
        path = write_file(scenarios_dir, "same-clock", scenario_document("Before", injects=2))
        assert fs.get_scenario("same-clock").name == "Before"
        frozen = os.stat(path)

        write_file(scenarios_dir, "same-clock", scenario_document("After", injects=7))
        os.utime(path, ns=(frozen.st_atime_ns, frozen.st_mtime_ns))
        assert os.stat(path).st_mtime_ns == frozen.st_mtime_ns, "the timestamp must be unchanged"

        served = fs.get_scenario("same-clock")
        assert served.name == "After"
        assert served.event_count == 7

    def test_a_deleted_scenario_stops_being_returned(self, scenarios_dir):
        path = write_file(scenarios_dir, "removed", scenario_document("Removed"))
        assert fs.get_scenario("removed") is not None

        path.unlink()

        assert fs.get_scenario("removed") is None
        assert fs.list_scenarios() == []

    def test_a_filtered_list_sees_a_write_too(self, scenarios_dir):
        """The range builder's scenario picker filters, so it must not be a stale path."""
        write_file(scenarios_dir, "blue", scenario_document("Blue", category="blue-team"))
        assert [s.id for s in fs.list_scenarios(category="blue-team")] == ["blue"]

        write_file(scenarios_dir, "blue", scenario_document("Blue", category="red-team"))

        assert fs.list_scenarios(category="blue-team") == []
        assert [s.id for s in fs.list_scenarios(category="red-team")] == ["blue"]


class TestNothingIsHeldThatWasNotJustRead:
    """Why the memo is safe to be per-process: its key comes only from the file."""

    def test_identical_bytes_are_parsed_once_and_changed_bytes_are_parsed_again(
        self, scenarios_dir, monkeypatch
    ):
        parses = []
        real_parse = fs._parse_scenario_yaml

        def counting_parse(file_path, raw, modified_at):
            parses.append(str(file_path))
            return real_parse(file_path, raw, modified_at)

        monkeypatch.setattr(fs, "_parse_scenario_yaml", counting_parse)

        write_file(scenarios_dir, "counted", scenario_document("Counted"))

        fs.list_scenarios()
        fs.list_scenarios()
        fs.list_scenarios()
        assert len(parses) == 1, "repeat reads of identical bytes must reuse the parse"

        write_file(scenarios_dir, "counted", scenario_document("Counted Again"))
        fs.list_scenarios()
        assert len(parses) == 2, "changed bytes must be parsed again"

    def test_a_touch_alone_is_reflected_in_modified_at(self, scenarios_dir):
        """The key carries the mtime as well as the digest, so the value cannot drift.

        `modified_at` is derived from the mtime, so if the key ignored it two
        workers could report different modification times for byte-identical
        files. Everything the parse is built from is in the key.
        """
        path = write_file(scenarios_dir, "touched", scenario_document("Touched"))
        before = fs.get_scenario("touched").modified_at

        bumped = os.stat(path).st_mtime_ns + 5_000_000_000
        os.utime(path, ns=(bumped, bumped))

        assert fs.get_scenario("touched").modified_at > before

    def test_the_memo_is_bounded(self, scenarios_dir, monkeypatch):
        """A worker that has been up for weeks must not hold every file it ever read.

        The previous cache dropped a key only when that same file was asked for
        again, so an entry for a scenario the catalog has since uninstalled was
        never reached and never freed.
        """
        monkeypatch.setattr(fs, "_MEMO_MAX_ENTRIES", 4)

        for i in range(20):
            write_file(scenarios_dir, f"bulk-{i:02d}", scenario_document(f"Bulk {i}"))

        assert len(fs.list_scenarios()) == 20
        assert len(fs._memo._entries) <= 4

        # Eviction must cost correctness nothing: every scenario is still served.
        assert len(fs.list_scenarios()) == 20


class TestConcurrentReadersDoNotFail:
    """Several threads per worker touch this, because every scenario route is a sync def."""

    def test_reading_while_a_file_changes_raises_nothing(self, scenarios_dir):
        """FastAPI runs sync routes on the anyio threadpool, so this is the live shape.

        The previous cache looked a key up and then deleted it in a second
        statement. Two threads that both found the mtime changed both ran the
        delete, and the second raised KeyError out of the cache and into the
        route -- a 500 on the Training Scenarios page, under exactly the
        condition the page is most likely to be open: somebody editing a
        scenario while others are reading it.
        """
        write_file(scenarios_dir, "contended", scenario_document("Contended"))

        failures = []
        stop = threading.Event()

        def reader():
            try:
                while not stop.is_set():
                    fs.list_scenarios()
                    fs.get_scenario("contended")
            except Exception as exc:  # noqa: BLE001 - the assertion is that there are none
                failures.append(exc)

        def writer():
            try:
                for i in range(60):
                    write_file(scenarios_dir, "contended", scenario_document(f"Contended {i}"))
            finally:
                stop.set()

        threads = [threading.Thread(target=reader) for _ in range(4)]
        threads.append(threading.Thread(target=writer))
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)

        assert failures == []
        assert not any(thread.is_alive() for thread in threads)


class TestAWriteIsNeverHalfVisible:
    """A reader on another worker must see the old document or the new one, never part of one."""

    def test_a_save_leaves_no_temporary_file_behind(self, scenarios_dir):
        fs.save_scenario("clean", scenario_document("Clean"))

        assert [p.name for p in scenarios_dir.iterdir()] == ["clean.yaml"]

    def test_a_failed_write_leaves_the_previous_document_intact(self, scenarios_dir):
        """A refusal that destroys the file it refused to replace is the worst outcome."""
        fs.save_scenario("survivor", scenario_document("Survivor"))

        unwritable = scenario_document("Doomed")
        unwritable["events"] = [object()]

        # The safe dumper refuses a value it has no standard tag for. The plain
        # dumper would have written `!!python/object:...`, which the reader then
        # refuses -- turning a document this worker accepted into a broken
        # scenario for all four of them.
        with pytest.raises(yaml.YAMLError):
            fs.save_scenario("survivor", unwritable, overwrite=True)

        assert fs.get_scenario("survivor").name == "Survivor"
        assert [p.name for p in scenarios_dir.iterdir()] == ["survivor.yaml"]

    def test_a_save_replaces_rather_than_truncating(self, scenarios_dir, monkeypatch):
        """The destination is never opened for writing, so it is never momentarily empty."""
        real_open = open
        opened_for_write = []

        def watching_open(file, mode="r", *args, **kwargs):
            if "w" in mode or "a" in mode or "+" in mode:
                opened_for_write.append(str(file))
            return real_open(file, mode, *args, **kwargs)

        monkeypatch.setattr("builtins.open", watching_open)
        fs.save_scenario("atomic", scenario_document("Atomic"))
        monkeypatch.undo()

        assert opened_for_write, "the save must have written something"
        assert not any(
            path.endswith("atomic.yaml") for path in opened_for_write
        ), "the live scenario file must be replaced, not opened and truncated in place"

    def test_the_temporary_file_is_invisible_to_a_concurrent_list(self, scenarios_dir):
        """A partly written file must not surface as somebody else's broken scenario."""
        fs.save_scenario("first", scenario_document("First"))
        listed_during_write = []

        real_replace = os.replace

        def replace_after_listing(src, dst, *args, **kwargs):
            # At this instant the temporary file exists and the destination does
            # not yet carry the new bytes. This is the whole window another
            # worker could observe.
            listed_during_write.append(sorted(p.name for p in scenarios_dir.glob("*.yaml")))
            return real_replace(src, dst, *args, **kwargs)

        os.replace = replace_after_listing
        try:
            fs.save_scenario("second", scenario_document("Second"))
        finally:
            os.replace = real_replace

        assert listed_during_write == [["first.yaml"]], (
            "the scenario glob must not pick up the file being written, "
            f"but saw {listed_during_write}"
        )

    def test_a_save_keeps_the_permissions_the_file_already_had(self, scenarios_dir):
        """Replacing a file must not re-create it at this process's umask.

        A rename carries the temporary file's own mode onto the destination,
        where writing in place kept whatever the file had. A scenario installed
        from the catalog keeps the mode `shutil.copy2` copied from the catalog's
        file, so without this an author saving through the UI silently widens a
        file somebody narrowed -- on a volume every API and worker pod mounts.
        """
        fs.save_scenario("narrow", scenario_document("Narrow"))
        path = scenarios_dir / "narrow.yaml"
        os.chmod(path, 0o600)

        fs.save_scenario("narrow", scenario_document("Narrow Again"), overwrite=True)

        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
        assert fs.get_scenario("narrow").name == "Narrow Again"


class TestNothingHandedOutPointsIntoTheMemo:
    """A caller must not be able to edit the parsed copy the next request will be served.

    The memo holds one parsed Scenario per file and hands the same object to
    every request in this worker. Its key is the bytes of the file, so an edit
    made to that object in memory is not something re-reading the file can
    undo: the digest still matches and the edited copy is served again. That is
    the same four-worker defect the memo was built to prevent, reached from the
    other side -- one worker answering with a scenario no file on the volume
    describes, for as long as that worker lives.

    Nothing does this today. These tests are what keeps that true, because the
    failure is silent and survives a restart of only three workers in four.
    """

    def test_editing_the_response_dict_does_not_edit_the_parsed_scenario(self, scenarios_dir):
        write_file(scenarios_dir, "shared", scenario_document("Shared"))

        first = fs.scenario_to_dict(fs.get_scenario("shared"), include_events=True)
        first["required_roles"].append("smuggled")
        first["events"][0]["actions"].append({"smuggled": True})

        second = fs.scenario_to_dict(fs.get_scenario("shared"), include_events=True)
        assert second["required_roles"] == ["operator"]
        assert second["events"][0]["actions"] == []

    def test_the_file_on_disk_is_what_is_served_after_such_an_edit(self, scenarios_dir):
        """The check that matters: the answer still matches the volume, not memory."""
        write_file(scenarios_dir, "shared", scenario_document("Shared"))

        fs.scenario_to_dict(fs.get_scenario("shared"), include_events=True)[
            "required_roles"
        ].append("smuggled")

        assert fs.get_scenario("shared").required_roles == ["operator"]


class TestRefreshIsNoLongerLoadBearing:
    """The endpoint reaches one worker of four and always did."""

    def test_a_change_is_seen_without_calling_refresh(self, scenarios_dir):
        write_file(scenarios_dir, "no-refresh", scenario_document("Original"))
        assert fs.get_scenario("no-refresh").name == "Original"

        write_file(scenarios_dir, "no-refresh", scenario_document("Replaced"))

        # No refresh_cache() call anywhere in this test.
        assert fs.get_scenario("no-refresh").name == "Replaced"

    def test_refresh_frees_the_memo_without_changing_any_answer(self, scenarios_dir):
        write_file(scenarios_dir, "kept", scenario_document("Kept"))
        before = fs.list_scenarios()
        assert fs._memo._entries

        fs.refresh_cache()

        assert not fs._memo._entries
        after = fs.list_scenarios()
        assert [s.id for s in after] == [s.id for s in before]
        assert [s.name for s in after] == [s.name for s in before]
