"""The gateway learns what is published from Ingress objects, and refuses to guess.

A range's applications are declared as ordinary Ingress objects naming a class no controller
serves. That keeps the table declarative, garbage-collected with the range's namespace, and
answerable with `kubectl get ing -A` -- and it means the gateway needs one cluster-wide
read-only permission rather than a database credential.
"""

import uuid

import pytest

from proving_ground.gateway.table import (
    APP_LABEL,
    GATEWAY_INGRESS_CLASS,
    RANGE_LABEL,
    IngressRouteTable,
    routes_from_ingresses,
)

APPS_HOST = "apps.example.com"


def ingress(rid, app="podinfo", namespace="pg-range-x", service="podinfo", port=9898, labels=None):
    return {
        "metadata": {
            "name": f"pg-app-{app}",
            "namespace": namespace,
            "labels": {APP_LABEL: app, RANGE_LABEL: str(rid), **(labels or {})},
        },
        "spec": {
            "ingressClassName": GATEWAY_INGRESS_CLASS,
            "rules": [
                {
                    "host": f"{rid}.{APPS_HOST}",
                    "http": {
                        "paths": [
                            {
                                "path": f"/pg/apps/{app}",
                                "pathType": "Prefix",
                                "backend": {"service": {"name": service, "port": {"number": port}}},
                            }
                        ]
                    },
                }
            ],
        },
    }


@pytest.fixture
def rid():
    return uuid.uuid4()


class TestBuildingTheTable:
    def test_a_published_application_resolves(self, rid):
        table = routes_from_ingresses([ingress(rid)])
        route = table[(rid, "podinfo")]
        assert route.namespace == "pg-range-x" and route.service == "podinfo"
        assert route.port == 9898
        assert route.host == "podinfo.pg-range-x.svc.cluster.local"

    def test_an_ingress_carrying_no_range_label_is_skipped(self, rid):
        """The labels are the contract; there is no host to fall back to.

        The fallback that used to be here was unreachable: the class name and both labels ship
        together, and the listing is filtered to the `pg-gateway` class first.
        """
        item = ingress(rid)
        item["metadata"]["labels"].pop(RANGE_LABEL)
        assert routes_from_ingresses([item]) == {}

    def test_a_range_label_that_is_not_a_range_key_is_skipped(self):
        item = ingress(uuid.uuid4())
        item["metadata"]["labels"][RANGE_LABEL] = "not-a-range"
        assert routes_from_ingresses([item]) == {}

    def test_an_ingress_with_two_backends_is_skipped(self, rid):
        """Ambiguous: proxying to whichever came first is a coin toss about where data goes."""
        item = ingress(rid)
        item["spec"]["rules"][0]["http"]["paths"].append(
            {
                "path": "/pg/apps/other",
                "pathType": "Prefix",
                "backend": {"service": {"name": "other", "port": {"number": 80}}},
            }
        )
        assert routes_from_ingresses([item]) == {}

    def test_an_ingress_with_no_backend_is_skipped(self, rid):
        item = ingress(rid)
        item["spec"]["rules"][0]["http"]["paths"] = []
        assert routes_from_ingresses([item]) == {}

    def test_a_named_port_is_skipped_rather_than_guessed(self, rid):
        """`port.name` needs the Service resolved; the gateway does not read Services."""
        item = ingress(rid)
        item["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]["port"] = {
            "name": "http"
        }
        assert routes_from_ingresses([item]) == {}

    def test_two_ranges_publishing_the_same_app_name_stay_separate(self):
        a, b = uuid.uuid4(), uuid.uuid4()
        table = routes_from_ingresses(
            [ingress(a, namespace="pg-range-a"), ingress(b, namespace="pg-range-b")]
        )
        assert table[(a, "podinfo")].namespace == "pg-range-a"
        assert table[(b, "podinfo")].namespace == "pg-range-b"


class FakeKube:
    def __init__(self, listing=None, fail=False):
        self.listing = listing or []
        self.fail = fail
        self.calls = 0

    async def list_ingresses_for_class(self, ingress_class):
        self.calls += 1
        assert ingress_class == GATEWAY_INGRESS_CLASS
        if self.fail:
            raise RuntimeError("apiserver unreachable")
        return self.listing


class TestRefreshing:
    async def test_it_reads_the_cluster(self, rid):
        kube = FakeKube([ingress(rid)])
        table = IngressRouteTable(kube)
        await table.refresh()
        assert table.lookup(rid, "podinfo") is not None

    async def test_a_failed_read_keeps_the_previous_table(self, rid):
        """Every published application 404ing because the API server blipped is the worse
        outcome; briefly stale is the right failure."""
        kube = FakeKube([ingress(rid)])
        table = IngressRouteTable(kube)
        await table.refresh()

        kube.fail = True
        await table.refresh()
        assert table.lookup(rid, "podinfo") is not None

    async def test_a_removed_application_stops_resolving(self, rid):
        kube = FakeKube([ingress(rid)])
        table = IngressRouteTable(kube)
        await table.refresh()

        kube.listing = []
        await table.refresh()
        assert table.lookup(rid, "podinfo") is None


class TestTheApplicationOwnsTheTablesLifecycle:
    """A pod that is serving must have a table being kept current, and one that is shutting
    down must not leave a refresh task behind it."""

    def test_starting_the_app_refreshes_and_stopping_it_cancels(self, rid):
        import httpx
        from starlette.testclient import TestClient

        from proving_ground.gateway.app import GatewayConfig, create_app

        kube = FakeKube([ingress(rid)])
        table = IngressRouteTable(kube, interval=3600)
        app = create_app(
            GatewayConfig(apps_host=APPS_HOST, path_prefix="/pg/apps"),
            table,
            httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))),
        )

        assert kube.calls == 0, "nothing should have been read before the app started"
        with TestClient(app):
            assert kube.calls >= 1, "startup must populate the table"
            assert table.lookup(rid, "podinfo") is not None
        assert table._task is None, "shutdown must not leave the refresh task running"
