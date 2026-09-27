# backend/tests/unit/test_redis_state_is_disposable.py
"""Nothing PROVING GROUND keeps in Redis may be authoritative (PG-39).

ADR-0015 decides that Redis is PG's own broker, run without persistence and
without an eviction policy, because everything in it is either retriable,
TTL'd, or rebuildable from the database. That decision is only safe while it
stays true, and it has already been broken once by accident: the warm pool's
ready set was the single record of which containers were claimable, so
recreating the Redis container emptied it, left every member running and
unclaimable, and silently turned a 15-second warm deploy back into a
52-second cold one. `reconcile_ready_set()` exists because of that.

So this is a guard, not a unit test. A module that starts writing to Redis
fails it until somebody writes down, here, why losing that data is survivable.
A decision recorded only in an ADR decays; this one has to be re-argued to be
broken.
"""

import ast
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2] / "proving_ground"

# Methods that are distinctly Redis. `set`, `get` and `delete` are deliberately
# absent: they collide with dict and ORM calls and would make this test noisy
# enough that somebody would weaken it.
REDIS_WRITES = {
    "setex",
    "setnx",
    "hset",
    "sadd",
    "srem",
    "spop",
    "lpush",
    "rpush",
    "zadd",
    "expire",
    "publish",
    "flushdb",
    "flushall",
}

# Every module allowed to put something in Redis, and why losing it is
# survivable. Add an entry only with a real answer to that question.
DECLARED = {
    "api/ranges.py": (
        "export_job:* job status, 24h TTL. Losing it orphans a file on disk "
        "that the user can export again; nothing is unrecoverable."
    ),
    "tasks/jobs.py": (
        "import/install job status, 1h TTL. Losing it means a progress modal "
        "cannot find its job; the work itself is a Dramatiq message."
    ),
    "tasks/blueprint_export.py": ("export job status, TTL'd. Same shape as api/ranges.py."),
    "services/event_broadcaster.py": (
        "pub/sub fan-out to websockets. Ephemeral by construction; the events "
        "themselves are rows in the event table."
    ),
    "services/event_service.py": ("pub/sub fan-out, as above. The database is the record."),
    "services/range_pool_service.py": (
        "pg:pool:ready, the set of claimable warm pool members. Derived state: "
        "reconcile_ready_set() rebuilds it from container labels cross-checked "
        "against the Range table, and is called on every refill."
    ),
}


def modules_touching_redis():
    """Modules that both know about Redis and call a distinctly-Redis writer."""
    found = {}
    for path in sorted(ROOT.rglob("*.py")):
        source = path.read_text(encoding="utf-8", errors="ignore")
        if "redis" not in source.lower():
            continue
        try:
            tree = ast.parse(source)
        except SyntaxError:  # pragma: no cover - a broken file fails elsewhere
            continue
        writes = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in REDIS_WRITES
        }
        if writes:
            found[str(path.relative_to(ROOT))] = sorted(writes)
    return found


def test_every_module_writing_to_redis_has_declared_why_losing_it_is_safe():
    touching = modules_touching_redis()
    undeclared = sorted(set(touching) - set(DECLARED))
    assert not undeclared, (
        "These modules write to Redis without saying why the data is disposable.\n"
        "ADR-0015 lets PG run Redis with no persistence and no eviction policy "
        "precisely because nothing in it is authoritative. If this module's data "
        "IS authoritative, Redis is the wrong home for it. If it is not, add an "
        "entry to DECLARED saying so:\n  "
        + "\n  ".join(f"{m} calls {touching[m]}" for m in undeclared)
    )


def test_the_declaration_list_has_not_gone_stale():
    """A module that stopped using Redis should stop being listed."""
    touching = modules_touching_redis()
    stale = sorted(set(DECLARED) - set(touching))
    assert not stale, (
        "These modules no longer write to Redis; drop them from DECLARED so the "
        "list keeps meaning something:\n  " + "\n  ".join(stale)
    )


def test_job_status_always_carries_a_ttl():
    """A JobStore without a TTL would accumulate forever and survive restarts."""
    from proving_ground.tasks.jobs import DEFAULT_TTL, JobStore

    assert DEFAULT_TTL > 0
    assert JobStore("x:").ttl == DEFAULT_TTL
    assert JobStore("x:", ttl=60).ttl == 60

    written = {}

    class FakeRedis:
        def setex(self, key, ttl, value):
            written[key] = ttl

    store = JobStore("guard:", ttl=123)
    store._redis = lambda: FakeRedis()
    store.update("job", "running", "step")
    assert written == {"guard:job": 123}, "update() must always go through setex"


def test_the_pool_ready_set_is_reconcilable():
    """The one piece of Redis state that is not TTL'd must be rebuildable."""
    from proving_ground.services.range_pool_service import RangePoolService

    assert hasattr(RangePoolService, "reconcile_ready_set"), (
        "pg:pool:ready has no TTL, so reconcile_ready_set() is the only thing "
        "that makes losing Redis survivable. It may not be removed."
    )


BROKER_DIR = pathlib.Path(__file__).resolve().parents[3] / "deploy/helm/proving-ground/templates"


def load_docs(name):
    """Parse a manifest, so assertions are about the spec and not about comments.

    Helm expressions are blanked first. `image: {{ .Values.redis.image }}` is not valid YAML --
    a brace opens a flow mapping -- and redis.yaml stopped being the chart's one untemplated
    manifest when its image became overridable, which it had to for an air-gapped or
    registry-restricted cluster to deploy at all.
    """
    import re

    import yaml

    text = re.sub(r"\{\{-?.*?-?\}\}", "__helm__", (BROKER_DIR / name).read_text(), flags=re.S)
    return [d for d in yaml.safe_load_all(text) if d]


def chart_values():
    import yaml

    return yaml.safe_load((BROKER_DIR.parent / "values.yaml").read_text())


def redis_conf():
    for doc in load_docs("redis.yaml"):
        if doc.get("kind") == "ConfigMap":
            return doc["data"]["redis.conf"]
    raise AssertionError("redis.yaml has no ConfigMap")


def redis_container():
    for doc in load_docs("redis.yaml"):
        if doc.get("kind") == "Deployment":
            return doc["spec"]["template"]["spec"]["containers"][0]
    raise AssertionError("redis.yaml has no Deployment")


@pytest.mark.parametrize(
    "setting,expected",
    [("save", '""'), ("appendonly", "no"), ("maxmemory-policy", "noeviction")],
)
def test_the_broker_config_enforces_the_decision(setting, expected):
    """The ADR's claims must be in the config, not only in the prose."""
    directives = {
        line.split(maxsplit=1)[0]: line.split(maxsplit=1)[1].strip()
        for line in redis_conf().splitlines()
        if line.strip() and not line.strip().startswith("#") and " " in line.strip()
    }
    assert directives.get(setting) == expected, (
        f"redis.conf no longer sets '{setting} {expected}'. ADR-0015 rests on it: "
        "persistence would make restart behaviour differ between profiles, and an "
        "eviction policy would silently drop queued Dramatiq messages."
    )


def test_the_broker_image_is_digest_pinned():
    """Read from values.yaml: the manifest now references it rather than carrying the literal."""
    image = chart_values()["redis"]["image"]
    assert "@sha256:" in image, (
        f"{image} is not digest-pinned. ADR-0007: an air-gapped mirror and a "
        "connected cluster must run the same bytes."
    )


def test_the_broker_has_nowhere_durable_to_write():
    """emptyDir, so 'never persist' survives someone re-enabling appendonly."""
    for doc in load_docs("redis.yaml"):
        if doc.get("kind") != "Deployment":
            continue
        volumes = {v["name"]: v for v in doc["spec"]["template"]["spec"]["volumes"]}
        assert "emptyDir" in volumes["data"], "the data volume must not be persistent"


def test_a_second_broker_is_never_started_alongside_the_first():
    """Two Redis pods are two brokers; workers on the old one starve."""
    for doc in load_docs("redis.yaml"):
        if doc.get("kind") == "Deployment":
            assert doc["spec"]["strategy"]["type"] == "Recreate"


def test_the_broker_is_not_reachable_cluster_wide():
    policies = [d for d in load_docs("redis-networkpolicy.yaml") if d["kind"] == "NetworkPolicy"]
    assert policies, "the broker has no NetworkPolicy"

    for policy in policies:
        assert policy["spec"]["policyTypes"] == ["Ingress"]
        for rule in policy["spec"]["ingress"]:
            for source in rule["from"]:
                assert "namespaceSelector" not in source, (
                    "a namespaceSelector would admit other namespaces to an "
                    "unauthenticated broker"
                )
                assert source["podSelector"]["matchLabels"] == {
                    "app.kubernetes.io/part-of": "proving-ground"
                }
