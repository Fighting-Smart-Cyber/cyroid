"""Infrastructure networks must never be deleted as if they were range networks.

cleanup_all_proving_ground_resources() is reachable from the admin API and removes
every matching network. Before this guard, pg-mgmt and pg-ranges were not excluded,
so a cleanup run took the platform's own networking down.
"""

from unittest.mock import MagicMock


def test_helper_includes_every_configured_infrastructure_network(monkeypatch):
    monkeypatch.delenv("PROVING_GROUND_MGMT_NETWORK", raising=False)
    monkeypatch.delenv("PROVING_GROUND_RANGES_NETWORK", raising=False)
    from proving_ground.config import infrastructure_network_names

    names = infrastructure_network_names()
    assert "pg-mgmt" in names, "the management network must be protected"
    assert "pg-ranges" in names, "the ranges network must be protected"
    assert "pg-management" in names
    assert "proving_ground_default" in names


def _net(name, labels):
    n = MagicMock()
    n.name = name
    n.id = f"id-{name}"
    n.attrs = {"Labels": labels, "IPAM": {"Config": [{"Subnet": "10.0.0.0/24"}]}}
    return n


def _service_with(networks):
    from proving_ground.services.docker_service import DockerService

    svc = DockerService.__new__(DockerService)
    svc.client = MagicMock()
    svc.client.containers.list.return_value = []
    svc.client.networks.list.return_value = networks
    svc.disconnect_traefik_from_network = MagicMock()
    svc.teardown_network_isolation = MagicMock()
    deleted = []
    svc.delete_network = MagicMock(side_effect=lambda nid: deleted.append(nid) or True)
    return svc, deleted


def test_cleanup_spares_infrastructure_networks():
    """The regression this test exists to prevent."""
    svc, deleted = _service_with(
        [
            _net("pg-mgmt", {}),
            _net("pg-ranges", {}),
            _net("pg-egress-abc12345", {"pg.range_id": "abc12345"}),
        ]
    )
    svc.cleanup_all_proving_ground_resources()

    assert "id-pg-mgmt" not in deleted, "cleanup deleted the management network"
    assert "id-pg-ranges" not in deleted, "cleanup deleted the ranges network"
    assert "id-pg-egress-abc12345" in deleted, "cleanup must still remove real range networks"


def test_cleanup_ignores_unrelated_pg_prefixed_networks():
    """The 'pg-' prefix is only 3 chars and can collide on a shared host."""
    svc, deleted = _service_with(
        [
            _net("pgadmin-internal", {}),
            _net("pg-egress-abc12345", {"pg.range_id": "abc12345"}),
        ]
    )
    svc.cleanup_all_proving_ground_resources()

    assert "id-pgadmin-internal" not in deleted, "cleanup deleted an unrelated network"
    assert "id-pg-egress-abc12345" in deleted


def test_networks_removed_count_excludes_already_gone_networks():
    """A network that vanished mid-cleanup must not be counted as removed."""
    svc, deleted = _service_with(
        [
            _net("pg-egress-abc12345", {"pg.range_id": "abc12345"}),
            _net("pg-egress-xyz67890", {"pg.range_id": "xyz67890"}),
        ]
    )

    # Override delete_network: return True for first, False for second (NotFound race)
    call_count = [0]

    def mock_delete(nid):
        call_count[0] += 1
        deleted.append(nid)
        return call_count[0] == 1  # True for first call, False for second

    svc.delete_network = MagicMock(side_effect=mock_delete)

    results = svc.cleanup_all_proving_ground_resources()

    assert (
        results["networks_removed"] == 1
    ), f"Expected 1 network removed, got {results['networks_removed']}"
