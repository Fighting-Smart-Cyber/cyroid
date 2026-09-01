"""Claimed pool containers have no pg.range_id label, so lookup must not rely on it."""

from unittest.mock import MagicMock


def _svc():
    from proving_ground.services.dind_service import DinDService

    s = DinDService.__new__(DinDService)
    s.host_client = MagicMock()
    return s


def test_resolves_by_container_id_when_given():
    s = _svc()
    sentinel = MagicMock()
    s.host_client.containers.get.return_value = sentinel

    found = s._find_container_by_range_id("r1", container_id="abc123")

    assert found is sentinel
    s.host_client.containers.get.assert_called_once_with("abc123")
    s.host_client.containers.list.assert_not_called()


def test_falls_back_to_label_when_no_container_id():
    s = _svc()
    sentinel = MagicMock()
    s.host_client.containers.list.return_value = [sentinel]

    found = s._find_container_by_range_id("r1")

    assert found is sentinel
    assert "pg.range_id=r1" in str(s.host_client.containers.list.call_args)


def test_falls_back_to_label_when_container_id_is_stale():
    """A recorded id can outlive the container it named."""
    import docker.errors

    s = _svc()
    s.host_client.containers.get.side_effect = docker.errors.NotFound("gone")
    sentinel = MagicMock()
    s.host_client.containers.list.return_value = [sentinel]

    assert s._find_container_by_range_id("r1", container_id="dead") is sentinel


def test_resolves_by_name_when_no_label_or_container_id():
    """A claimed pool container has no pg.range_id label; the rename it gets on
    claim (pg-range-{name}-{short_id} or pg-range-{short_id}) is the only
    remaining signal, so lookup must fall back to a name match."""
    s = _svc()
    range_id = "rangeid1"  # no dashes -> short_id == range_id itself (8 chars)
    short_id = range_id.replace("-", "")[:8]
    container = MagicMock()
    container.name = f"pg-range-Foo-{short_id}"

    def list_side_effect(all=True, filters=None):
        if filters and "label" in filters:
            return []
        if filters and "name" in filters:
            return [container]
        return []

    s.host_client.containers.list.side_effect = list_side_effect

    found = s._find_container_by_range_id(range_id)

    assert found is container


def test_name_match_rejects_substring_collision():
    """short_id is only 8 hex chars, so a naive substring match on container
    name could hit an unrelated container. The match must be exact: name ==
    pg-range-{short_id}, or name starts with pg-range- and ends with
    -{short_id}."""
    s = _svc()
    range_id = "abc12345"
    short_id = range_id.replace("-", "")[:8]
    decoy = MagicMock()
    decoy.name = f"pg-range-{short_id}-decoy"  # contains short_id, wrong position
    real = MagicMock()
    real.name = f"pg-range-Foo-{short_id}"  # correct: ends with -{short_id}

    def list_side_effect(all=True, filters=None):
        if filters and "label" in filters:
            return []
        if filters and "name" in filters:
            return [decoy, real]  # decoy listed first
        return []

    s.host_client.containers.list.side_effect = list_side_effect

    found = s._find_container_by_range_id(range_id)

    assert found is real


def test_label_match_wins_over_name_match():
    """A cold-provisioned range carries both a label and a name; the label is
    the stronger signal and must be preferred over a name match."""
    s = _svc()
    range_id = "abc12345"
    label_container = MagicMock()
    name_container = MagicMock()

    def list_side_effect(all=True, filters=None):
        if filters and "label" in filters:
            return [label_container]
        if filters and "name" in filters:
            return [name_container]
        return []

    s.host_client.containers.list.side_effect = list_side_effect

    found = s._find_container_by_range_id(range_id)

    assert found is label_container
    # the name filter must never even be consulted once the label match wins
    name_calls = [
        c
        for c in s.host_client.containers.list.call_args_list
        if c.kwargs.get("filters", {}).get("name")
    ]
    assert name_calls == []
