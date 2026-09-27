# backend/tests/unit/test_every_actor_is_registered.py
"""Every Dramatiq actor the app enqueues must be registered with the worker.

The worker runs `dramatiq proving_ground.tasks`, so an actor is only known to
it if importing that package imports the module defining it. Miss the import
and nothing fails loudly: the producer enqueues happily, the broker accepts the
message, and the worker logs `ActorNotFound` and moves it to the dead-letter
queue. The user watches a progress modal that never advances.

That shipped once — `install_catalog_item_async` (PG-149) was defined, tested,
merged and deployed without ever being imported into the package, and 121 tests
passed because they all call the task's body directly rather than crossing the
broker. This is the test that would have caught it.
"""

import ast
import importlib
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2] / "proving_ground"


def enqueued_actor_names():
    """Every `<name>.send(...)` call site in the codebase."""
    found = {}
    for path in sorted(ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except SyntaxError:  # pragma: no cover
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "send"
                and isinstance(node.func.value, ast.Name)
            ):
                name = node.func.value.id
                # Actors are module-level callables named like tasks, not locals
                # such as `response.send()`; require the _task/_async convention
                # the codebase already follows.
                if name.endswith(("_task", "_async")):
                    found.setdefault(name, set()).add(str(path.relative_to(ROOT)))
    return found


def test_every_enqueued_actor_is_importable_from_the_tasks_package():
    tasks = importlib.import_module("proving_ground.tasks")
    enqueued = enqueued_actor_names()
    assert enqueued, "found no .send() call sites — the scan is broken, not the code"

    missing = {name: sorted(where) for name, where in enqueued.items() if not hasattr(tasks, name)}
    assert not missing, (
        "These actors are enqueued but are not registered on "
        "proving_ground.tasks, so the worker will log ActorNotFound and "
        "dead-letter every message:\n  "
        + "\n  ".join(f"{n} — enqueued from {w}" for n, w in missing.items())
        + "\n\nAdd `from .<module> import <actor>` to proving_ground/tasks/__init__.py."
    )


def test_the_registered_actors_are_actually_dramatiq_actors():
    """A plain function exported under the same name would pass the check above."""
    import dramatiq

    tasks = importlib.import_module("proving_ground.tasks")
    for name in enqueued_actor_names():
        actor = getattr(tasks, name, None)
        if actor is None:
            continue
        assert isinstance(actor, dramatiq.Actor), (
            f"proving_ground.tasks.{name} is {type(actor).__name__}, not a "
            "dramatiq.Actor — .send() on it will not reach the worker."
        )
