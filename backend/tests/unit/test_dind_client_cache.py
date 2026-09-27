"""A range's cached Docker client must follow its DinD, not outlive it.

Observed live on 2026-09-09. A range redeployed after teardown failed every
time with:

    HTTPConnectionPool(host='172.30.1.9', port=2375): Max retries exceeded
    with url: /v1.43/networks/create ... [Errno 113] No route to host

while the deploy log, three lines earlier, said:

    DinD container at 172.30.1.6, waiting for Docker daemon...
    Docker daemon ready at tcp://172.30.1.6:2375

The platform created the new DinD correctly, waited for it correctly, and then
talked to the previous one. get_range_client cached on range_id alone and read
docker_url only on a miss, so every redeploy of a range got the client built
for whichever DinD it had the first time.

It survived teardown because teardown runs in the API process and deploys run
in the worker: close_range_client cleared the API's cache and never touched
the worker's. Restarting the worker "fixed" it, which is what made it look
intermittent rather than deterministic.
"""

from unittest.mock import MagicMock, patch

import pytest

from proving_ground.services.dind_service import DinDService


@pytest.fixture
def svc():
    with patch("proving_ground.services.dind_service.docker.from_env"):
        return DinDService()


def _clients(svc, monkeypatch):
    """Record every DockerClient built, in order, with its base_url."""
    built = []

    def fake_client(base_url, **kw):
        c = MagicMock(name=f"client:{base_url}")
        c.base_url = base_url
        built.append(c)
        return c

    monkeypatch.setattr("proving_ground.services.dind_service.docker.DockerClient", fake_client)
    return built


class TestTheClientFollowsTheAddress:
    def test_the_same_address_is_served_from_cache(self):
        """The cache still has to work; this is the behaviour being preserved."""
        with (
            patch("proving_ground.services.dind_service.docker.from_env"),
            patch("proving_ground.services.dind_service.docker.DockerClient") as dc,
        ):
            s = DinDService()
            a = s.get_range_client("r1", "tcp://172.30.1.6:2375")
            b = s.get_range_client("r1", "tcp://172.30.1.6:2375")
        assert a is b
        assert dc.call_count == 1, "a repeat call rebuilt the client"

    def test_a_new_address_for_the_same_range_returns_a_new_client(self, svc, monkeypatch):
        """The defect. Same range id, redeployed onto a different DinD."""
        built = _clients(svc, monkeypatch)
        old = svc.get_range_client("r1", "tcp://172.30.1.9:2375")
        new = svc.get_range_client("r1", "tcp://172.30.1.6:2375")
        assert new is not old, "redeploy was handed the previous DinD's client"
        assert new.base_url == "tcp://172.30.1.6:2375"
        assert [c.base_url for c in built] == [
            "tcp://172.30.1.9:2375",
            "tcp://172.30.1.6:2375",
        ]

    def test_the_superseded_client_is_closed(self, svc, monkeypatch):
        """Otherwise a redeploying host leaks a connection pool per deploy."""
        built = _clients(svc, monkeypatch)
        svc.get_range_client("r1", "tcp://172.30.1.9:2375")
        svc.get_range_client("r1", "tcp://172.30.1.6:2375")
        built[0].close.assert_called_once()

    def test_two_ranges_do_not_share_a_client(self, svc, monkeypatch):
        built = _clients(svc, monkeypatch)
        a = svc.get_range_client("r1", "tcp://172.30.1.6:2375")
        b = svc.get_range_client("r2", "tcp://172.30.1.7:2375")
        assert a is not b
        assert len(built) == 2

    def test_a_range_returning_to_a_previous_address_still_gets_a_live_client(
        self, svc, monkeypatch
    ):
        """IPs are recycled. Coming back to .9 must not resurrect the closed
        client that was built for the old .9."""
        built = _clients(svc, monkeypatch)
        first = svc.get_range_client("r1", "tcp://172.30.1.9:2375")
        svc.get_range_client("r1", "tcp://172.30.1.6:2375")
        again = svc.get_range_client("r1", "tcp://172.30.1.9:2375")
        assert again is not first
        assert len(built) == 3


class TestCloseForgetsTheAddressToo:
    def test_close_then_reopen_builds_a_client_for_the_new_address(self, svc, monkeypatch):
        """If close_range_client dropped the client but kept the recorded url,
        the next call would match that url, miss the client, and rebuild --
        harmless -- but a later call to the OLD url would compare equal and
        return the wrong client. Forgetting both keeps the pair consistent."""
        built = _clients(svc, monkeypatch)
        svc.get_range_client("r1", "tcp://172.30.1.9:2375")
        svc.close_range_client("r1")
        assert "r1" not in svc._range_client_urls
        svc.get_range_client("r1", "tcp://172.30.1.6:2375")
        assert built[-1].base_url == "tcp://172.30.1.6:2375"

    def test_close_all_forgets_every_address(self, svc, monkeypatch):
        _clients(svc, monkeypatch)
        svc.get_range_client("r1", "tcp://172.30.1.6:2375")
        svc.get_range_client("r2", "tcp://172.30.1.7:2375")
        svc.close_all_range_clients()
        assert svc._range_clients == {}
        assert svc._range_client_urls == {}


class TestTheCachesCannotDriftApart:
    def test_every_cached_client_has_a_recorded_url(self, svc, monkeypatch):
        """The two dicts are written together and must stay in step; a client
        with no recorded url would compare unequal forever and rebuild on
        every single call."""
        _clients(svc, monkeypatch)
        for rid, url in (("r1", "tcp://a:2375"), ("r2", "tcp://b:2375"), ("r1", "tcp://c:2375")):
            svc.get_range_client(rid, url)
        assert set(svc._range_clients) == set(svc._range_client_urls)
