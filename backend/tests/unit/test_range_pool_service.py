"""Warm pool: claim atomicity, graceful degradation, and the security invariant."""

import uuid
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def svc():
    from proving_ground.services.range_pool_service import RangePoolService

    s = RangePoolService.__new__(RangePoolService)
    s.redis = MagicMock()
    s.docker = MagicMock()
    s.dind = MagicMock()
    # Sensible default so tests that aren't specifically about I6 don't need
    # to configure network preference too - see _healthy_container() below.
    s.dind.ranges_network = "pg-ranges"
    return s


@pytest.fixture(autouse=True)
def fake_refill_task():
    """Autouse safety net: proving_ground/tasks/__init__.py configures a
    RedisBroker against the REAL platform's Redis at import time, so an
    unpatched refill_pool_task.send() in these tests would enqueue a real
    task against the live pool queue. Every claim()/discard path that
    might enqueue a refill must go through this mock, never the wire.
    """
    with patch("proving_ground.tasks.pool.refill_pool_task") as fake_task:
        yield fake_task


@pytest.fixture(autouse=True)
def fake_inner_docker_client():
    """Default: the inner-Docker-daemon probe (I4) succeeds - claim()
    creates its own docker.DockerClient(base_url=..., timeout=...) rather
    than reusing dind_service's async _wait_for_docker_ready() (see
    range_pool_service._probe_inner_docker's docstring for why: a nested
    event loop there would raise "Cannot run the event loop while another
    loop is running" every time claim() is invoked from inside code that's
    already running one, which is exactly the real production call path).
    Tests that want to simulate an unresponsive daemon set
    `fake_inner_docker_client.return_value.ping.side_effect = ...`.
    """
    from proving_ground.services import range_pool_service as m

    with patch.object(m.docker, "DockerClient") as fake_cls:
        fake_cls.return_value = MagicMock()
        yield fake_cls


def _healthy_container(status="running", ip="172.30.1.9", network="pg-ranges", labels=None):
    """A pool member that will pass every health check in claim(): running,
    a resolvable mgmt IP on the preferred network, and (via the svc fixture)
    a successful inner-daemon probe."""
    c = MagicMock(status=status)
    c.labels = labels if labels is not None else {}
    c.attrs = {"NetworkSettings": {"Networks": {network: {"IPAddress": ip}}}}
    return c


def test_claim_returns_none_when_pool_empty(svc):
    """An empty pool must fall back to cold provisioning, not fail the deploy."""
    svc.redis.spop.return_value = None
    assert svc.claim("r1", "Range One", ["img:latest"]) is None


def test_claim_returns_none_when_redis_is_down(svc):
    """The pool is an optimisation and must never be a correctness dependency."""
    svc.redis.spop.side_effect = ConnectionError("redis unreachable")
    assert svc.claim("r1", "Range One", ["img:latest"]) is None


def test_claim_is_atomic(svc):
    """SPOP removes the member, so two concurrent claims cannot get the same one."""
    svc.redis.spop.side_effect = [b"container-a", None]
    svc.docker.containers.get.return_value = _healthy_container()

    first = svc.claim("r1", "One", [])
    second = svc.claim("r2", "Two", [])

    assert first is not None
    assert second is None


def test_claim_discards_an_unhealthy_member_and_tries_again(svc):
    svc.redis.spop.side_effect = [b"dead", b"alive", None]
    dead, alive = MagicMock(status="exited"), _healthy_container()
    svc.docker.containers.get.side_effect = lambda cid: dead if cid == "dead" else alive

    assert svc.claim("r1", "One", []) is not None


def test_release_is_not_implemented():
    """SECURITY INVARIANT: a used range container is destroyed, never reused.

    Trainees get root inside these containers; returning one to the pool is a
    cross-tenant contamination path. This test exists so that adding a release
    path is a deliberate, visible act rather than an accident.
    """
    from proving_ground.services import range_pool_service as m

    assert not hasattr(
        m.RangePoolService, "release"
    ), "a used range container must never be returned to the pool"
    assert not hasattr(
        m.RangePoolService, "return_to_pool"
    ), "a used range container must never be returned to the pool"


# =====================================================================
# C2(a): the image-set check must be a superset test, not equality.
#
# claim() previously compared a hash of the member's pre-pulled image set
# against a hash of the required set with `!=` - equality, not coverage.
# That only ever worked because this host has exactly one blueprint with
# one VM, so "the union of images across all blueprints" and "this range's
# images" happened to be identical sets. A second blueprint, or a range
# using a subset of a blueprint's VMs, breaks it silently (no exception,
# just an eternal cold-provisioning fallback) - the same class of bug that
# already survived two earlier fix rounds.
#
# Minor (b): the test that should have caught this originally asserted the
# WRONG label name ("pg.pool_images" instead of POOL_IMAGE_SET_LABEL /
# "pg.pool_image_set"), so it passed on the label being *absent*, not on a
# real mismatch. Fixed below.
# =====================================================================


def test_claim_falls_back_when_image_set_does_not_cover(svc):
    """Minor (b) fix: use the real label constant and encoding, and
    actually exercise containment (missing one required image)."""
    from proving_ground.services import range_pool_service as m

    member = MagicMock(status="running")
    member.labels = {m.POOL_IMAGE_SET_LABEL: m._encode_image_set_label(["repo/a:latest"])}
    svc.redis.spop.return_value = b"container-a"
    svc.docker.containers.get.return_value = member

    assert svc.claim("r1", "One", ["repo/a:latest", "repo/b:latest"]) is None


def test_claim_succeeds_when_member_image_set_is_a_strict_superset(svc):
    """C2(a): the decision must be set containment (>=), not equality -
    extra pre-pulled images the range doesn't need must not disqualify a
    member."""
    from proving_ground.services import range_pool_service as m

    member = _healthy_container(
        labels={
            m.POOL_IMAGE_SET_LABEL: m._encode_image_set_label(
                ["repo/a:latest", "repo/b:latest", "repo/extra-image-not-needed:latest"]
            ),
        }
    )
    svc.redis.spop.return_value = b"container-a"
    svc.docker.containers.get.return_value = member

    result = svc.claim("r1", "One", ["repo/a:latest", "repo/b:latest"])

    assert result is not None


# =====================================================================
# I3: every non-happy claim() path used to orphan a member - SPOP removes
# it from Redis first, and image mismatch / unhealthy / lookup-exception
# all abandoned the container (and its ~3.8 GB volume) with no refill.
#
# - Image mismatch: the member was never renamed or handed to a tenant, so
#   it is safe (and required) to sadd it straight back.
# - Unhealthy / exception: destroy the container + volume and enqueue a
#   refill so pool depth doesn't silently and permanently shrink.
# =====================================================================


def test_claim_returns_a_mismatched_member_to_the_pool(svc):
    from proving_ground.services import range_pool_service as m

    member = MagicMock(status="running")
    member.labels = {m.POOL_IMAGE_SET_LABEL: m._encode_image_set_label(["repo/other:latest"])}
    svc.redis.spop.return_value = b"container-a"
    svc.docker.containers.get.return_value = member

    result = svc.claim("r1", "One", ["repo/needed:latest"])

    assert result is None
    svc.redis.sadd.assert_called_once_with(m.POOL_READY_KEY, "container-a")
    member.rename.assert_not_called()
    member.remove.assert_not_called()


def test_claim_destroys_an_unhealthy_member_and_enqueues_refill(svc, fake_refill_task):
    from proving_ground.services import range_pool_service as m

    dead = MagicMock(status="exited")
    dead.labels = {m.POOL_ID_LABEL: "pool-uuid-1"}
    svc.redis.spop.side_effect = [b"dead-container", None]
    svc.docker.containers.get.return_value = dead

    result = svc.claim("r1", "One", [])

    assert result is None
    dead.remove.assert_called_once_with(force=True)
    svc.docker.volumes.get.assert_called_with(m._volume_name_for_pool_id("pool-uuid-1"))
    svc.redis.sadd.assert_not_called()
    fake_refill_task.send.assert_called_once()


def test_claim_discards_and_enqueues_refill_when_member_vanishes(svc, fake_refill_task):
    import docker.errors

    svc.redis.spop.side_effect = [b"ghost", None]
    svc.docker.containers.get.side_effect = docker.errors.NotFound("gone")

    result = svc.claim("r1", "One", [])

    assert result is None
    fake_refill_task.send.assert_called_once()


# =====================================================================
# Minor (c): the rename itself was never actually asserted - svc.dind was
# a bare MagicMock in every prior test, so _get_container_name()/rename()
# arguments were never checked. A silent failure here breaks teardown,
# isolation and VNC (the claimed container becomes unfindable).
# =====================================================================


def test_claim_renames_the_container_to_the_computed_range_name(svc):
    member = _healthy_container()
    svc.redis.spop.return_value = b"container-a"
    svc.docker.containers.get.return_value = member
    svc.dind._get_container_name.return_value = "pg-range-deploy-one-abcd1234"

    result = svc.claim("r1", "Deploy One", [])

    svc.dind._get_container_name.assert_called_once_with("r1", "Deploy One")
    member.rename.assert_called_once_with("pg-range-deploy-one-abcd1234")
    assert result["container_name"] == "pg-range-deploy-one-abcd1234"


# =====================================================================
# I4: claim() only ever checked container.status, never the INNER Docker
# daemon. A member whose inner dockerd died was claimed as healthy and
# only failed later, at network creation - after claim() already returned,
# so the cold-provisioning fallback could never trigger.
# =====================================================================


def test_claim_discards_member_whose_inner_docker_daemon_probe_fails(
    svc, fake_refill_task, fake_inner_docker_client
):
    member = _healthy_container()
    svc.redis.spop.side_effect = [b"container-a", None]
    svc.docker.containers.get.return_value = member
    fake_inner_docker_client.return_value.ping.side_effect = TimeoutError("daemon not ready")

    result = svc.claim("r1", "One", [])

    assert result is None
    member.rename.assert_not_called()
    member.remove.assert_called_once_with(force=True)
    fake_refill_task.send.assert_called_once()


def test_claim_probes_inner_docker_with_a_short_bounded_timeout(svc, fake_inner_docker_client):
    """The probe must be bounded well below the full cold-provisioning
    startup timeout (config's dind_startup_timeout, default 60s) - a
    claimed member's daemon should already be up."""
    member = _healthy_container()
    svc.redis.spop.side_effect = [b"container-a", None]
    svc.docker.containers.get.return_value = member

    svc.claim("r1", "One", [])

    assert fake_inner_docker_client.call_args is not None
    _, kwargs = fake_inner_docker_client.call_args
    assert kwargs.get("timeout", 999) < 60


# =====================================================================
# I6: claim() took the first network reporting ANY IP; cold provisioning
# prefers self.dind.ranges_network, falls back to any network (with a
# warning) only if that has none, and raises if NONE do. Members sit on
# both pg-ranges and pg-mgmt, so claim() could record a pg-mgmt IP in
# ranges.dind_docker_url - or, worse, silently succeed with mgmt_ip=None
# (-> "tcp://None:2375") when no network had reported an IP yet.
# =====================================================================


def test_claim_prefers_ranges_network_ip_like_the_cold_path(svc):
    member = MagicMock(status="running")
    member.labels = {}
    member.attrs = {
        "NetworkSettings": {
            "Networks": {
                "pg-mgmt": {"IPAddress": "172.31.0.5"},
                "pg-ranges": {"IPAddress": "172.30.1.9"},
            }
        }
    }
    svc.redis.spop.return_value = b"container-a"
    svc.docker.containers.get.return_value = member

    result = svc.claim("r1", "One", [])

    assert result is not None
    assert result["mgmt_ip"] == "172.30.1.9"


def test_claim_falls_back_to_any_network_ip_when_ranges_network_has_none(svc):
    """Mirrors the cold path's fallback-with-warning (dind_service.py):
    pg-mgmt only (no pg-ranges IP yet) must still succeed, using the
    pg-mgmt IP - exactly the choice a cold-provisioned range would make."""
    member = MagicMock(status="running")
    member.labels = {}
    member.attrs = {
        "NetworkSettings": {
            "Networks": {
                "pg-mgmt": {"IPAddress": "172.31.0.5"},
            }
        }
    }
    svc.redis.spop.return_value = b"container-a"
    svc.docker.containers.get.return_value = member

    result = svc.claim("r1", "One", [])

    assert result is not None
    assert result["mgmt_ip"] == "172.31.0.5"


def test_claim_discards_member_with_no_usable_ip_at_all(svc, fake_refill_task):
    """Only a total absence of any IP is unhealthy - the old bug silently
    returned a 'successful' claim with mgmt_ip=None (-> tcp://None:2375)."""
    member = MagicMock(status="running")
    member.labels = {}
    member.attrs = {"NetworkSettings": {"Networks": {}}}
    svc.redis.spop.side_effect = [b"container-a", None]
    svc.docker.containers.get.return_value = member

    result = svc.claim("r1", "One", [])

    assert result is None
    member.rename.assert_not_called()
    fake_refill_task.send.assert_called_once()


# =====================================================================
# Fix round 1: resolve_pool_images() must make the pool NOT a no-op.
#
# With range_pool_images = [] (the default), a pool member that pre-pulls
# nothing gets a pg.pool_image_set that can never cover a real deploy's
# required_images, so claim() would always return None and the pool would
# silently do nothing while still burning RAM/disk. These tests pin the
# resolver that fixes that, and prove end-to-end that a member it
# provisions can actually be claimed.
# =====================================================================


class _FakeQuery:
    """Just enough of a Query to drive resolve_pool_images: unfiltered .all(),
    plus .filter(Column == value).first() (C3: _resolve_vm_base_image_tag
    now issues a real .filter(BaseImage.name == ...).first() so it can't
    drift from blueprint_service's identical call - see that function's
    docstring). .first() returns rows in SEEDING ORDER, i.e. whatever order
    the row list was constructed in, exactly like an unordered `.first()`
    against a real table returns "some" row rather than a chosen one - the
    point of the tests below is that pool and blueprint_service agree on
    THAT row, not that either of them has a "correct" order to expect.
    """

    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return self._rows

    def filter(self, *criteria):
        rows = self._rows
        for expr in criteria:
            key = expr.left.key
            value = expr.right.value
            rows = [r for r in rows if getattr(r, key, None) == value]
        return _FakeQuery(rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeDB:
    """Fake Session: db.query(Model).all() returns whatever rows were seeded,
    and db.query(Model).filter(Model.col == value).first() returns the
    first seeded row matching that single equality (see _FakeQuery).
    """

    def __init__(self, blueprints=(), base_images=(), golden_images=(), snapshots=()):
        self._tables = {
            "RangeBlueprint": list(blueprints),
            "BaseImage": list(base_images),
            "GoldenImage": list(golden_images),
            "Snapshot": list(snapshots),
        }

    def query(self, model):
        return _FakeQuery(self._tables.get(model.__name__, []))


def test_resolve_pool_images_returns_settings_verbatim_without_touching_db():
    """Explicit config always wins, and short-circuits before any query."""
    from proving_ground.services import range_pool_service as m

    fake_settings = MagicMock(range_pool_images=["explicit-a:latest", "explicit-b:latest"])
    fake_db = MagicMock()

    with patch.object(m, "get_settings", return_value=fake_settings):
        result = m.resolve_pool_images(fake_db)

    assert result == ["explicit-a:latest", "explicit-b:latest"]
    fake_db.query.assert_not_called()


def test_resolve_pool_images_unions_across_blueprints():
    """Empty settings -> union of container images referenced by all blueprints,
    de-duplicated and sorted."""
    from proving_ground.services import range_pool_service as m
    from proving_ground.models import RangeBlueprint, BaseImage

    img_a = BaseImage(
        id=uuid.uuid4(), name="a", image_type="container", docker_image_tag="repo/a:latest"
    )
    img_b = BaseImage(
        id=uuid.uuid4(), name="b", image_type="container", docker_image_tag="repo/b:latest"
    )

    bp1 = RangeBlueprint(
        id=uuid.uuid4(),
        name="bp1",
        config={"vms": [{"hostname": "h1", "base_image_id": str(img_a.id)}]},
    )
    bp2 = RangeBlueprint(
        id=uuid.uuid4(),
        name="bp2",
        config={
            "vms": [
                {"hostname": "h2", "base_image_id": str(img_b.id)},
                {"hostname": "h2b", "base_image_id": str(img_a.id)},  # duplicate of bp1's image
            ]
        },
    )

    fake_db = _FakeDB(blueprints=[bp1, bp2], base_images=[img_a, img_b])
    fake_settings = MagicMock(range_pool_images=[])

    with patch.object(m, "get_settings", return_value=fake_settings):
        result = m.resolve_pool_images(fake_db)

    assert result == sorted(["repo/a:latest", "repo/b:latest"])


# =====================================================================
# C2(b): fallback resolution must AGREE with how blueprint_service.py's
# create_range_from_blueprint() actually materializes a VM, not merely
# resemble it. Three divergences, all real:
#   - ISO base images were skipped entirely instead of resolved the way
#     range_deployment_service._resolve_range_images() resolves a
#     materialized ISO VM (vm_type -> qemux/dockurr image).
#   - base_image_tag was used verbatim instead of looked up (with the same
#     registry-prefix-stripping fallback blueprint_service.py uses).
#   - template_name was used verbatim instead of looked up as a
#     BaseImage.name match.
# When a match exists, the pool must pre-pull THAT row's own image, not
# the raw blueprint-config string - otherwise the range's real required
# set (resolved from the materialized VM's base_image_id) can never be
# covered by what the pool actually pre-pulled.
# =====================================================================


def test_resolve_pool_images_resolves_iso_backed_vms_like_a_real_deploy():
    from proving_ground.services import range_pool_service as m
    from proving_ground.models import RangeBlueprint, BaseImage
    from proving_ground.models.vm_enums import VMType

    linux_iso = BaseImage(
        id=uuid.uuid4(),
        name="linux-iso",
        image_type="iso",
        vm_type=VMType.LINUX_VM,
    )
    windows_iso = BaseImage(
        id=uuid.uuid4(),
        name="win-iso",
        image_type="iso",
        vm_type=VMType.WINDOWS_VM,
        native_arch="x86_64",
    )
    bp = RangeBlueprint(
        id=uuid.uuid4(),
        name="iso-bp",
        config={
            "vms": [
                {"hostname": "h1", "base_image_id": str(linux_iso.id)},
                {"hostname": "h2", "base_image_id": str(windows_iso.id), "arch": "x86_64"},
            ]
        },
    )

    fake_db = _FakeDB(blueprints=[bp], base_images=[linux_iso, windows_iso])
    fake_settings = MagicMock(range_pool_images=[])

    with patch.object(m, "get_settings", return_value=fake_settings):
        result = m.resolve_pool_images(fake_db)

    assert result == sorted(["qemux/qemu:latest", "dockurr/windows:latest"])


def test_resolve_pool_images_still_skips_a_container_type_check_correctly():
    """Sanity check that container-type resolution is untouched by the ISO
    change above."""
    from proving_ground.services import range_pool_service as m
    from proving_ground.models import RangeBlueprint, BaseImage

    container_img = BaseImage(
        id=uuid.uuid4(), name="c", image_type="container", docker_image_tag="repo/c:latest"
    )

    bp = RangeBlueprint(
        id=uuid.uuid4(),
        name="bp",
        config={"vms": [{"hostname": "h2", "base_image_id": str(container_img.id)}]},
    )

    fake_db = _FakeDB(blueprints=[bp], base_images=[container_img])
    fake_settings = MagicMock(range_pool_images=[])

    with patch.object(m, "get_settings", return_value=fake_settings):
        result = m.resolve_pool_images(fake_db)

    assert result == ["repo/c:latest"]


def test_resolve_pool_images_resolves_base_image_tag_via_registry_stripped_lookup():
    """When base_image_tag matches an existing BaseImage (after stripping a
    registry prefix, exactly like blueprint_service.py's fallback), the
    pool must pre-pull THAT row's own docker_image_tag - not the raw,
    possibly registry-qualified, config string."""
    from proving_ground.services import range_pool_service as m
    from proving_ground.models import RangeBlueprint, BaseImage

    img = BaseImage(
        id=uuid.uuid4(),
        name="kali",
        image_type="container",
        docker_image_tag="proving_ground/kali:latest",
    )
    bp = RangeBlueprint(
        id=uuid.uuid4(),
        name="registry-qualified-tag",
        config={
            "vms": [
                {
                    "hostname": "h1",
                    "base_image_tag": "127.0.0.1:5000/proving_ground/kali:latest",
                }
            ]
        },
    )

    fake_db = _FakeDB(blueprints=[bp], base_images=[img])
    fake_settings = MagicMock(range_pool_images=[])

    with patch.object(m, "get_settings", return_value=fake_settings):
        result = m.resolve_pool_images(fake_db)

    assert result == ["proving_ground/kali:latest"]


def test_resolve_pool_images_resolves_template_name_via_base_image_name_lookup():
    """template_name must resolve via BaseImage.name, like
    blueprint_service.py:273, not be used verbatim as an image tag."""
    from proving_ground.services import range_pool_service as m
    from proving_ground.models import RangeBlueprint, BaseImage

    img = BaseImage(
        id=uuid.uuid4(),
        name="legacy-kali-template",
        image_type="container",
        docker_image_tag="repo/legacy-kali:2021",
    )
    bp = RangeBlueprint(
        id=uuid.uuid4(),
        name="template-resolves",
        config={"vms": [{"hostname": "h1", "template_name": "legacy-kali-template"}]},
    )

    fake_db = _FakeDB(blueprints=[bp], base_images=[img])
    fake_settings = MagicMock(range_pool_images=[])

    with patch.object(m, "get_settings", return_value=fake_settings):
        result = m.resolve_pool_images(fake_db)

    assert result == ["repo/legacy-kali:2021"]


def test_resolve_pool_images_matches_blueprint_service_selection_for_a_duplicate_name():
    """C3: BaseImage.name is not unique - this host has 8 rows named
    'vpn-kiosk'. blueprint_service.create_range_from_blueprint() (~line 274)
    resolves a name via `.filter(BaseImage.name == ...).first()` - FIRST
    wins, in whatever order the query returns. The pool previously preloaded
    {img.name: img for img in all_base_images} - a dict comprehension, so
    LAST wins in iteration order - and the two picked different rows:
    measured live, blueprint resolved ':latest' while the pool resolved
    ':pre-netextender' for the identical blueprint. issuperset() then never
    holds and the pool silently never gets claimed for that image.

    Seeded so the two selection strategies would historically disagree:
    dict-comprehension last-wins picks img_current (seeded second),
    .first() picks img_old (seeded first). The pool must now agree with
    the .first()-based selection, because it issues that exact same call.
    """
    from proving_ground.services import range_pool_service as m
    from proving_ground.models import RangeBlueprint, BaseImage

    img_old = BaseImage(
        id=uuid.uuid4(),
        name="vpn-kiosk",
        image_type="container",
        docker_image_tag="proving-ground/vpn-kiosk:pre-netextender",
    )
    img_current = BaseImage(
        id=uuid.uuid4(),
        name="vpn-kiosk",
        image_type="container",
        docker_image_tag="proving-ground/vpn-kiosk:latest",
    )

    bp = RangeBlueprint(
        id=uuid.uuid4(),
        name="vpn-kiosk-range",
        config={"vms": [{"hostname": "h1", "base_image_name": "vpn-kiosk"}]},
    )

    fake_db = _FakeDB(blueprints=[bp], base_images=[img_old, img_current])
    fake_settings = MagicMock(range_pool_images=[])

    with patch.object(m, "get_settings", return_value=fake_settings):
        pool_result = m.resolve_pool_images(fake_db)

    # The exact call blueprint_service.create_range_from_blueprint() makes
    # to resolve base_image_name (~line 274).
    blueprint_selected = fake_db.query(BaseImage).filter(BaseImage.name == "vpn-kiosk").first()

    assert pool_result == [blueprint_selected.docker_image_tag]
    assert pool_result == ["proving-ground/vpn-kiosk:pre-netextender"]


@pytest.mark.asyncio
async def test_provision_member_refuses_when_resolved_image_set_is_empty(svc):
    """An empty resolved set must be a loud no-op, not a wasted container."""
    from proving_ground.services import range_pool_service as m

    svc.docker_service = MagicMock()

    with (
        patch.object(m, "resolve_pool_images", return_value=[]),
        patch.object(m, "get_session_local") as mock_gsl,
    ):
        mock_gsl.return_value = MagicMock()
        result = await svc.provision_member()

    assert result is None
    svc.dind.create_range_container.assert_not_called()


@pytest.mark.asyncio
async def test_provisioned_member_can_be_claimed_for_matching_images(svc):
    """End-to-end: a member provisioned for an image set is claimable by a
    deploy requiring that exact set. Proves provision and claim agree on
    the same encoded image set instead of silently missing each other."""
    from proving_ground.services import range_pool_service as m

    images = ["repo/kali:latest", "repo/windows-dc:2022"]
    captured = {}

    async def fake_create_range_container(**kwargs):
        captured["labels"] = kwargs["labels"]
        return {
            "container_id": "member-123",
            "container_name": "pg-range-abcd1234",
            "mgmt_ip": "172.30.1.9",
            "docker_url": "tcp://172.30.1.9:2375",
            "docker_port": 2375,
            "volume_name": "pg-range-abcd1234-docker",
        }

    async def fake_pull(**kwargs):
        return {"success": True}

    svc.dind.create_range_container = fake_create_range_container
    svc.docker_service = MagicMock()
    svc.docker_service.pull_image_to_dind = fake_pull

    with (
        patch.object(m, "resolve_pool_images", return_value=images),
        patch.object(m, "get_session_local") as mock_gsl,
    ):
        mock_gsl.return_value = MagicMock()
        container_id = await svc.provision_member()

    assert container_id == "member-123"
    assert "pg.range_id" not in captured["labels"]
    assert captured["labels"]["pg.type"] == "pool"

    # Now simulate the live container Docker would report back at claim time.
    member = MagicMock(status="running")
    member.labels = captured["labels"]
    member.attrs = {"NetworkSettings": {"Networks": {"pg-ranges": {"IPAddress": "172.30.1.9"}}}}
    member.id = "member-123"
    svc.redis.spop.return_value = b"member-123"
    svc.docker.containers.get.return_value = member

    result = svc.claim("r1", "Deploy One", images)

    assert result is not None
    assert result["container_id"] == "member-123"


# =====================================================================
# Fix round 2: base_image_tag / template_name fallback fields, no match.
#
# blueprint_service.py's export path (extract_config_from_range, ~lines
# 50-92) populates base_image_tag from the exported VM's BaseImage at
# export time; on any blueprint where the id no longer resolves locally
# (imported across environments, or - as found live on this host for the
# "IL2 VPN Kiosk" blueprint - the id fields are simply None) base_image_tag
# is the ONLY image reference left. When NO BaseImage row matches it
# either (the scenarios below), the pool falls back to the raw string
# verbatim - over-inclusive (an unclaimed image sitting in the pool) but
# never under-inclusive, which is what actually breaks claims.
# =====================================================================


def test_resolve_pool_images_uses_base_image_tag_verbatim_with_no_db_lookup():
    from proving_ground.services import range_pool_service as m
    from proving_ground.models import RangeBlueprint

    bp = RangeBlueprint(
        id=uuid.uuid4(),
        name="tag-only",
        config={"vms": [{"hostname": "h1", "base_image_tag": "proving-ground/vpn-kiosk:latest"}]},
    )

    # No BaseImage/GoldenImage/Snapshot rows seeded at all - a DB lookup for
    # the tag finds nothing, so the raw tag must be used verbatim.
    fake_db = _FakeDB(blueprints=[bp])
    fake_settings = MagicMock(range_pool_images=[])

    with patch.object(m, "get_settings", return_value=fake_settings):
        result = m.resolve_pool_images(fake_db)

    assert result == ["proving-ground/vpn-kiosk:latest"]


def test_resolve_pool_images_falls_back_to_template_name():
    from proving_ground.services import range_pool_service as m
    from proving_ground.models import RangeBlueprint

    bp = RangeBlueprint(
        id=uuid.uuid4(),
        name="template-only",
        config={"vms": [{"hostname": "h1", "template_name": "legacy-kali-template"}]},
    )

    fake_db = _FakeDB(blueprints=[bp])
    fake_settings = MagicMock(range_pool_images=[])

    with patch.object(m, "get_settings", return_value=fake_settings):
        result = m.resolve_pool_images(fake_db)

    assert result == ["legacy-kali-template"]


def test_resolve_pool_images_prefers_base_image_tag_over_template_name():
    from proving_ground.services import range_pool_service as m
    from proving_ground.models import RangeBlueprint

    bp = RangeBlueprint(
        id=uuid.uuid4(),
        name="both-fallbacks",
        config={
            "vms": [
                {
                    "hostname": "h1",
                    "base_image_tag": "proving-ground/current:latest",
                    "template_name": "deprecated-template",
                }
            ]
        },
    )

    fake_db = _FakeDB(blueprints=[bp])
    fake_settings = MagicMock(range_pool_images=[])

    with patch.object(m, "get_settings", return_value=fake_settings):
        result = m.resolve_pool_images(fake_db)

    assert result == ["proving-ground/current:latest"]


def test_resolve_pool_images_prefers_base_image_id_over_base_image_tag():
    """One image per VM: an Image Library id always wins over the tag fallback,
    even when both are present (e.g. a blueprint exported before Issue #80 cleared
    the id side of a later re-point)."""
    from proving_ground.services import range_pool_service as m
    from proving_ground.models import RangeBlueprint, BaseImage

    img = BaseImage(
        id=uuid.uuid4(), name="a", image_type="container", docker_image_tag="repo/a:latest"
    )
    bp = RangeBlueprint(
        id=uuid.uuid4(),
        name="id-and-tag",
        config={
            "vms": [
                {
                    "hostname": "h1",
                    "base_image_id": str(img.id),
                    "base_image_tag": "repo/stale-tag:latest",
                }
            ]
        },
    )

    fake_db = _FakeDB(blueprints=[bp], base_images=[img])
    fake_settings = MagicMock(range_pool_images=[])

    with patch.object(m, "get_settings", return_value=fake_settings):
        result = m.resolve_pool_images(fake_db)

    assert result == ["repo/a:latest"]


def test_resolve_pool_images_mixes_id_based_and_tag_based_vms():
    from proving_ground.services import range_pool_service as m
    from proving_ground.models import RangeBlueprint, BaseImage

    img = BaseImage(
        id=uuid.uuid4(), name="a", image_type="container", docker_image_tag="repo/a:latest"
    )
    bp1 = RangeBlueprint(
        id=uuid.uuid4(),
        name="id-based",
        config={"vms": [{"hostname": "h1", "base_image_id": str(img.id)}]},
    )
    bp2 = RangeBlueprint(
        id=uuid.uuid4(),
        name="tag-based",
        config={"vms": [{"hostname": "h2", "base_image_tag": "proving-ground/vpn-kiosk:latest"}]},
    )

    fake_db = _FakeDB(blueprints=[bp1, bp2], base_images=[img])
    fake_settings = MagicMock(range_pool_images=[])

    with patch.object(m, "get_settings", return_value=fake_settings):
        result = m.resolve_pool_images(fake_db)

    assert result == sorted(["repo/a:latest", "proving-ground/vpn-kiosk:latest"])


# =====================================================================
# Fix round 3: the base_image_id branch must FALL THROUGH, and there was
# no base_image_name branch at all.
#
# blueprint_service.py's create_range_from_blueprint() (~lines 264-294) is
# the authority here and FALLS THROUGH: base_image_id -> base_image_name
# (~274) -> base_image_tag (~280) -> template_name (~291). This module's
# resolver was an if/elif chain instead: a base_image_id that is SET but
# does not resolve to a real BaseImage row (stale id, or an id from a
# blueprint imported into an environment where that row was never created)
# took the base_image_id branch, produced nothing, and every later branch
# - including base_image_tag, which extract_config_from_range() populates
# on every export specifically for this cross-environment case (Issue #80)
# - was unreachable. That silently starves the pool of an image a real
# deploy will still ask for: members get provisioned with a smaller image
# set, issuperset() rejects every claim needing the missing image, and the
# pool becomes a silent no-op that burns RAM/disk while every deploy runs
# cold. See resolver-fix-report.md for the full writeup.
# =====================================================================


def test_resolve_pool_images_falls_through_unresolvable_base_image_id_to_base_image_tag():
    """The regression this fix closes: base_image_id is set but does not
    resolve (no matching BaseImage row), yet a valid base_image_tag is
    present. The old elif chain yielded nothing for this VM; it must now
    yield the tag."""
    from proving_ground.services import range_pool_service as m
    from proving_ground.models import RangeBlueprint

    stale_id = str(uuid.uuid4())
    bp = RangeBlueprint(
        id=uuid.uuid4(),
        name="stale-id-with-tag",
        config={
            "vms": [
                {
                    "hostname": "h1",
                    "base_image_id": stale_id,
                    "base_image_tag": "repo/kali:latest",
                }
            ]
        },
    )

    # No BaseImage rows seeded at all - stale_id resolves to nothing.
    fake_db = _FakeDB(blueprints=[bp])
    fake_settings = MagicMock(range_pool_images=[])

    with patch.object(m, "get_settings", return_value=fake_settings):
        result = m.resolve_pool_images(fake_db)

    assert result == ["repo/kali:latest"]


def test_resolve_pool_images_resolves_via_base_image_name():
    """A VM with only base_image_name set (the Issue #80 cross-environment
    export field, mirroring blueprint_service.py:274's fallback) resolves
    via a BaseImage.name lookup - there was previously no branch for this
    field at all."""
    from proving_ground.services import range_pool_service as m
    from proving_ground.models import RangeBlueprint, BaseImage

    img = BaseImage(
        id=uuid.uuid4(),
        name="kali-image",
        image_type="container",
        docker_image_tag="repo/kali:latest",
    )
    bp = RangeBlueprint(
        id=uuid.uuid4(),
        name="name-only",
        config={"vms": [{"hostname": "h1", "base_image_name": "kali-image"}]},
    )

    fake_db = _FakeDB(blueprints=[bp], base_images=[img])
    fake_settings = MagicMock(range_pool_images=[])

    with patch.object(m, "get_settings", return_value=fake_settings):
        result = m.resolve_pool_images(fake_db)

    assert result == ["repo/kali:latest"]


def test_resolve_pool_images_full_precedence_id_over_name_over_tag_over_template():
    """Precedence must match blueprint_service.py exactly: id -> name ->
    tag -> template_name - not an invented order. One image per VM even
    when multiple fields are simultaneously set (a real export populates
    id, name AND tag together), and no duplicates across VMs."""
    from proving_ground.services import range_pool_service as m
    from proving_ground.models import RangeBlueprint, BaseImage

    img_id = BaseImage(
        id=uuid.uuid4(),
        name="by-id",
        image_type="container",
        docker_image_tag="repo/by-id:latest",
    )
    img_name = BaseImage(
        id=uuid.uuid4(),
        name="by-name",
        image_type="container",
        docker_image_tag="repo/by-name:latest",
    )

    bp = RangeBlueprint(
        id=uuid.uuid4(),
        name="full-precedence",
        config={
            "vms": [
                {
                    # id wins even though name/tag/template_name are ALSO set -
                    # exactly what a real export looks like.
                    "hostname": "h1",
                    "base_image_id": str(img_id.id),
                    "base_image_name": "by-name",
                    "base_image_tag": "repo/stale-tag:latest",
                    "template_name": "stale-template",
                },
                {
                    # no (resolvable) id - name wins over tag and template_name.
                    "hostname": "h2",
                    "base_image_name": "by-name",
                    "base_image_tag": "repo/stale-tag-2:latest",
                    "template_name": "stale-template-2",
                },
                {
                    # no id/name - tag wins over template_name.
                    "hostname": "h3",
                    "base_image_tag": "repo/by-tag:latest",
                    "template_name": "stale-template-3",
                },
            ]
        },
    )

    fake_db = _FakeDB(blueprints=[bp], base_images=[img_id, img_name])
    fake_settings = MagicMock(range_pool_images=[])

    with patch.object(m, "get_settings", return_value=fake_settings):
        result = m.resolve_pool_images(fake_db)

    assert result == sorted(["repo/by-id:latest", "repo/by-name:latest", "repo/by-tag:latest"])


def test_resolve_pool_images_nothing_resolves_contributes_nothing_and_does_not_raise():
    """A VM whose base_image_id doesn't resolve and has no name/tag/
    template_name/golden_image_id/snapshot_id fallback contributes nothing
    - not an exception, not a spurious verbatim image."""
    from proving_ground.services import range_pool_service as m
    from proving_ground.models import RangeBlueprint

    bp = RangeBlueprint(
        id=uuid.uuid4(),
        name="nothing-resolves",
        config={"vms": [{"hostname": "h1", "base_image_id": str(uuid.uuid4())}]},
    )

    fake_db = _FakeDB(blueprints=[bp])
    fake_settings = MagicMock(range_pool_images=[])

    with patch.object(m, "get_settings", return_value=fake_settings):
        result = m.resolve_pool_images(fake_db)

    assert result == []


# =====================================================================
# I4 (second half): range_pool_member_ttl_hours was defined in config.py
# and read nowhere. reap_expired_members() is the reaper that makes it a
# live setting instead of dead config.
# =====================================================================


def test_reap_expired_members_destroys_only_members_older_than_ttl(svc, fake_refill_task):
    from datetime import datetime, timedelta, timezone

    old_created = (datetime.now(timezone.utc) - timedelta(hours=48)).strftime(
        "%Y-%m-%dT%H:%M:%S.000000000Z"
    )
    fresh_created = (datetime.now(timezone.utc) - timedelta(hours=1)).strftime(
        "%Y-%m-%dT%H:%M:%S.000000000Z"
    )

    old_container = MagicMock(status="running")
    old_container.attrs = {"Created": old_created}
    old_container.labels = {}
    fresh_container = MagicMock(status="running")
    fresh_container.attrs = {"Created": fresh_created}
    fresh_container.labels = {}

    svc.redis.smembers.return_value = [b"old-member", b"fresh-member"]
    svc.redis.srem.return_value = 1

    def get_container(cid):
        return old_container if cid == "old-member" else fresh_container

    svc.docker.containers.get.side_effect = get_container

    reaped = svc.reap_expired_members(ttl_hours=24)

    assert reaped == 1
    old_container.remove.assert_called_once_with(force=True)
    fresh_container.remove.assert_not_called()
    # The fresh member must be put back - it was removed from the ready set
    # only to be inspected.
    svc.redis.sadd.assert_called_once_with("pg:pool:ready", "fresh-member")


def test_reap_expired_members_is_a_noop_when_ttl_is_not_positive(svc):
    svc.redis.smembers.return_value = [b"member-1"]
    reaped = svc.reap_expired_members(ttl_hours=0)
    assert reaped == 0
    svc.redis.srem.assert_not_called()


# =====================================================================
# Minor: a member must not be marked READY when every pre-pull failed.
#
# provision_member()'s pre-pull loop swallowed each failure and then
# sadd'd the member into POOL_READY_KEY unconditionally. A member with
# zero images actually present still advertises pg.pool_image_set for the
# full resolved set, so claim()'s issuperset() check happily hands it out
# - Stage 3 then pulls every image cold anyway, but the "warm" signal was
# a lie and nothing logged it as anything other than a per-image warning.
# If NOTHING pulled, the member must be destroyed exactly like an
# unhealthy member (never sadd'd), not silently marked ready.
#
# I2: that destroy must NOT also enqueue a refill - see the docstring on
# test_provision_member_destroys_member_when_every_prepull_fails below.
# =====================================================================


@pytest.mark.asyncio
async def test_provision_member_destroys_member_when_every_prepull_fails(svc, fake_refill_task):
    """Every pre-pull fails -> the member must be destroyed and never
    marked ready, not silently sadd'd with an image set it doesn't have.

    I2: this must NOT enqueue a refill. This path runs from inside
    refill_pool_task's own shortfall loop - with a registry down, every
    provision_member() call in that loop hits this exact branch, and
    enqueueing a refill from here used to make each one spawn another,
    forever (net churn on the DinD containers/volumes AND the threads
    worker-deploy shares with real deploys). That member was never
    claimed/usable, so retrying immediately cannot help, and the next
    teardown or API startup already enqueues a refill unconditionally.
    """
    from proving_ground.services import range_pool_service as m

    images = ["repo/kali:latest", "repo/windows-dc:2022"]

    async def fake_create_range_container(**kwargs):
        return {
            "container_id": "member-fail",
            "container_name": "pg-range-fail1234",
            "mgmt_ip": "172.30.1.9",
            "docker_url": "tcp://172.30.1.9:2375",
            "docker_port": 2375,
            "volume_name": "pg-range-fail1234-docker",
        }

    async def fake_pull_always_fails(**kwargs):
        raise RuntimeError("registry unreachable")

    svc.dind.create_range_container = fake_create_range_container
    svc.docker_service = MagicMock()
    svc.docker_service.pull_image_to_dind = fake_pull_always_fails

    member = MagicMock()
    member.labels = {"pg.pool_id": "pool-fail-id"}
    svc.docker.containers.get.return_value = member

    with (
        patch.object(m, "resolve_pool_images", return_value=images),
        patch.object(m, "get_session_local") as mock_gsl,
    ):
        mock_gsl.return_value = MagicMock()
        result = await svc.provision_member()

    assert result is None
    svc.redis.sadd.assert_not_called()
    member.remove.assert_called_once_with(force=True)
    fake_refill_task.send.assert_not_called()


@pytest.mark.asyncio
async def test_provision_member_marks_ready_when_some_prepulls_fail(svc, fake_refill_task):
    """A PARTIAL failure is still genuinely useful - the member is marked
    ready (issuperset() will reject any claim that needs the missing
    image), it just isn't destroyed the way a total failure is."""
    from proving_ground.services import range_pool_service as m

    images = ["repo/kali:latest", "repo/windows-dc:2022"]

    async def fake_create_range_container(**kwargs):
        return {
            "container_id": "member-partial",
            "container_name": "pg-range-partial12",
            "mgmt_ip": "172.30.1.9",
            "docker_url": "tcp://172.30.1.9:2375",
            "docker_port": 2375,
            "volume_name": "pg-range-partial12-docker",
        }

    async def fake_pull(**kwargs):
        if kwargs.get("image") == "repo/windows-dc:2022":
            raise RuntimeError("registry unreachable")
        return {"success": True}

    svc.dind.create_range_container = fake_create_range_container
    svc.docker_service = MagicMock()
    svc.docker_service.pull_image_to_dind = fake_pull

    with (
        patch.object(m, "resolve_pool_images", return_value=images),
        patch.object(m, "get_session_local") as mock_gsl,
    ):
        mock_gsl.return_value = MagicMock()
        result = await svc.provision_member()

    assert result == "member-partial"
    svc.redis.sadd.assert_called_once_with(m.POOL_READY_KEY, "member-partial")
    fake_refill_task.send.assert_not_called()
