# backend/proving_ground/services/range_pool_service.py
"""Warm range pool service.

Maintains a small pool of pre-booted, pre-imaged DinD containers ("pool
members") so a deploy can skip the expensive cold-provisioning path (image
ensure, network setup, inner daemon boot) by claiming a ready member instead
of calling dind_service.create_range_container() from scratch.

SECURITY INVARIANT - READ BEFORE TOUCHING THIS FILE
----------------------------------------------------
Trainees get root inside range containers. A claimed container is renamed
into a live range and is destroyed on that range's teardown exactly like a
cold-provisioned one. It is NEVER returned to this pool: recycling a
container a trainee had root inside would be a cross-tenant contamination
path. There is deliberately no release() / return_to_pool() method (or
equivalent) on this class - see test_release_is_not_implemented in
tests/unit/test_range_pool_service.py, which asserts those attributes do
not exist. Refill always provisions a FRESH container via provision_member().
If a change to this file would require adding a release path, stop: that is
a deliberate, visible design decision for someone else to make, not a
refactor to slip in here.

The ONE exception (phase-3 fix wave, I3): a member that was SPOP'd off the
ready set and then found to have an image set that does not cover what the
caller needs is sadd'd straight back. That is safe because claim() has not
renamed it and it was never handed to a caller - no tenant ever touched it.
Every OTHER discard path in claim() (not running, no usable IP, inner
Docker daemon unresponsive, a lookup exception) destroys the container and
its volume instead, because by the time those checks run we may already be
past the point where re-adding would be safe to reason about uniformly.
Anything renamed or returned to a caller must be destroyed on teardown,
never sadd'd back, no exceptions.

THE POOL IS NEVER A CORRECTNESS DEPENDENCY
-------------------------------------------
claim() must never raise. Every failure - empty pool, an unhealthy member,
an image-set mismatch, Redis being down, a Docker error - is caught and
turned into None so the caller (range_deployment_service) falls back to
cold provisioning. A deploy must never fail because the pool had a problem.
"""

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import docker
import docker.errors
import redis

from proving_ground.config import get_settings
from proving_ground.database import get_session_local
from proving_ground.models import Range, RangeBlueprint, BaseImage, GoldenImage, Snapshot
from proving_ground.models.vm_enums import VMType
from proving_ground.services.blueprint_service import base_image_tag_lookup_candidates
from proving_ground.services.dind_service import get_dind_service
from proving_ground.services.docker_service import get_docker_service
from proving_ground.utils.image_ref import repo_of
from proving_ground.services.image_resolution import runtime_for_image
from proving_ground.services.registry_service import RegistryService

logger = logging.getLogger(__name__)

# Redis key holding the set of container ids that are ready to be claimed.
POOL_READY_KEY = "pg:pool:ready"

# Label carrying the JSON-encoded, sorted list of images a pool member was
# pre-imaged with. A claim compares this against required_images as a SET
# CONTAINMENT check (member's set must be a superset) - not equality. Two
# earlier fix rounds missed this because equality and containment coincide
# when there is exactly one blueprint with one VM, which is all this host
# had; a second blueprint (or a range using a subset of a blueprint's VMs)
# breaks equality silently. See C2 in the phase-3 fix-wave report.
POOL_IMAGE_SET_LABEL = "pg.pool_image_set"

# Stored runtime references carry no registry; an isolated range pulls from
# the local mirror, so it is applied at the point of use.
_REGISTRY_MIRROR = f"{RegistryService.REGISTRY_IP}:{RegistryService.REGISTRY_PORT}"

# Label carrying the synthetic pool uuid a member was created with. Its
# Docker volume name (pg-range-{short_id}-docker, same formula dind_service
# uses for a cold range) is derived from this at claim time, since the
# volume is not renamed along with the container.
POOL_ID_LABEL = "pg.pool_id"

# Short, bounded timeout for the inner-Docker-daemon health probe performed
# at claim time (I4). This is NOT the cold-provisioning startup timeout
# (config's dind_startup_timeout, default 60s) - a claimed member's daemon
# should already be up from provisioning, so anything beyond a few seconds
# means the member is unhealthy, not merely still booting.
POOL_CLAIM_PROBE_TIMEOUT_SECONDS = 3


def _encode_image_set_label(images: list[str]) -> str:
    """JSON-encode a sorted, de-duplicated image list for POOL_IMAGE_SET_LABEL."""
    return json.dumps(sorted(set(images)))


def _parse_image_set_label(value: Optional[str]) -> set[str]:
    """Inverse of _encode_image_set_label. Never raises - an unparseable or
    absent label is treated as an empty set (covers nothing)."""
    if not value:
        return set()
    try:
        parsed = json.loads(value)
        if isinstance(parsed, list):
            return set(parsed)
    except (TypeError, ValueError):
        pass
    return set()


def _volume_name_for_pool_id(pool_id: str) -> str:
    """Same derivation dind_service.create_range_container() uses."""
    short_id = str(pool_id).replace("-", "")[:8]
    return f"pg-range-{short_id}-docker"


def _parse_docker_created(created: Optional[str]) -> Optional[datetime]:
    """Parse a Docker container's Created timestamp (RFC3339 with
    nanosecond precision, e.g. '2026-08-25T12:00:00.123456789Z') into an
    aware UTC datetime. Returns None on anything unparseable rather than
    raising - a container whose age can't be determined is treated as not
    expired (see reap_expired_members)."""
    if not created:
        return None
    try:
        # datetime.fromisoformat only handles up to microsecond precision
        # and (before 3.11) doesn't accept a bare 'Z' - truncate the
        # fractional seconds and normalize the suffix ourselves.
        head = created.split(".")[0] if "." in created else created.rstrip("Z")
        return datetime.strptime(head, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _resolve_base_image_tag(
    base_images_by_tag: dict[str, BaseImage], tag: str
) -> Optional[BaseImage]:
    """Look up a BaseImage by docker_image_tag using the same candidate
    order blueprint_service.create_range_from_blueprint() tries (exact tag,
    then with a registry prefix stripped) - see
    base_image_tag_lookup_candidates()'s docstring."""
    for candidate in base_image_tag_lookup_candidates(tag):
        img = base_images_by_tag.get(candidate)
        if img:
            return img
    return None


def _image_tag_for_base_image(base_img: BaseImage, arch: Optional[str]) -> Optional[str]:
    """Resolve a BaseImage row (already selected by id, tag or name) to the
    image tag a real deploy would pull for it. Mirrors
    range_deployment_service.RangeDeploymentService._resolve_range_images()
    for both container and ISO-backed base images - the two MUST agree, see
    C2(b): ISO rows were previously skipped entirely here, which meant a
    range using ISO-backed VMs could never be covered by any pool member.
    """
    if base_img is None:
        return None

    if base_img.image_type == "container":
        image_tag = base_img.docker_image_tag or base_img.docker_image_id
        if image_tag and (
            "dockurr/windows" in image_tag.lower() or "dockur/windows" in image_tag.lower()
        ):
            if arch == "x86_64":
                image_tag = "dockurr/windows:latest"
            elif arch == "arm64":
                image_tag = "dockurr/windows-arm:latest"
        return image_tag or None

    if base_img.image_type == "iso":
        target_arch = arch or base_img.native_arch
        if base_img.vm_type == VMType.WINDOWS_VM:
            return (
                "dockurr/windows-arm:latest" if target_arch == "arm64" else "dockurr/windows:latest"
            )
        if base_img.vm_type == VMType.MACOS_VM:
            return "dockurr/macos:latest"
        # LINUX_VM and anything unrecognized default to the generic QEMU
        # image, exactly like _resolve_range_images' else branch.
        return "qemux/qemu:latest"

    return None


def _resolve_vm_base_image_tag(
    db,
    base_images: dict[str, BaseImage],
    base_images_by_tag: dict[str, BaseImage],
    *,
    base_image_id: Optional[str],
    base_image_name: Optional[str],
    base_image_tag: Optional[str],
    template_name: Optional[str],
    arch: Optional[str],
) -> Optional[str]:
    """Resolve a blueprint VM's base-image identifier fields to an image
    tag, trying each candidate IN THE SAME FALL-THROUGH ORDER
    blueprint_service.create_range_from_blueprint() uses (~lines 264-294):
    base_image_id -> base_image_name (~274) -> base_image_tag (~280) ->
    template_name (~291). That function is the authority for this order -
    it is what actually decides which BaseImage a materialized VM row
    gets, and range_deployment_service._resolve_range_images() then always
    prefers vm.base_image_id (however it was resolved) over golden_image_id
    or snapshot_id at deploy time. Do not reorder these four independently
    of blueprint_service without re-reading it first.

    Each candidate is tried only if EVERY earlier one produced NO image,
    not merely if the earlier field was absent - a base_image_id that is
    set but does not resolve to a real row (stale id, or an id from a
    blueprint imported into an environment where that row was never
    created - Issue #80) must fall through to base_image_name/tag/
    template_name exactly like an absent id would. This was the bug: the
    old code used an if/elif chain that treated "base_image_id is set" as
    "base_image_id resolved," so a stale id made every later branch -
    including base_image_tag, which extract_config_from_range() populates
    on every export specifically for this cross-environment case -
    unreachable.

    base_image_tag and template_name fall back to the raw string verbatim
    when no BaseImage row matches (over-inclusive - an image nothing will
    ever require - but never under-inclusive, which is the failure mode
    that actually breaks a claim). base_image_id and base_image_name do
    NOT fall back to a verbatim value on a miss, matching
    blueprint_service: an unresolved id/name isn't a usable image
    reference on its own, so resolution simply continues to the next
    candidate instead of inventing a value nothing would ever pull.

    Extracted as a single function (rather than four inline elif blocks)
    so this file cannot grow a second, differently-ordered copy of the
    same chain the way the base_image_tag/golden_image_id ordering had
    already drifted once before.

    C3: base_image_name and template_name are resolved with a live
    `db.query(BaseImage).filter(BaseImage.name == ...).first()` - the EXACT
    same call blueprint_service.create_range_from_blueprint() makes (~line
    274) - rather than a dict preloaded as {img.name: img for img in ...}.
    BaseImage.name is not unique (this host has 8 rows named 'vpn-kiosk');
    a dict comprehension is last-write-wins in iteration order, while
    .first() with no ORDER BY returns whatever row Postgres's query plan
    produces first for that exact query. Those two have no reason to agree,
    and measured live on this host they didn't (pool resolved
    ':pre-netextender', blueprint resolved ':latest' for the same blueprint
    VM). Issuing the identical query blueprint_service issues - same SQL,
    same session, same table - is the only way to guarantee the same row
    every time rather than merely making agreement likely; preloading with
    some ORDER BY cannot provide that guarantee because .first() on the
    OTHER side still has no ORDER BY to match against.
    """
    if base_image_id:
        image_tag = _image_tag_for_base_image(base_images.get(str(base_image_id)), arch)
        if image_tag:
            return image_tag

    if base_image_name:
        matched = db.query(BaseImage).filter(BaseImage.name == base_image_name).first()
        if matched:
            image_tag = _image_tag_for_base_image(matched, arch)
            if image_tag:
                return image_tag

    if base_image_tag:
        # Fallback identifier for cross-environment portability (Issue #80,
        # VMConfig.base_image_tag). blueprint_service.py's
        # extract_config_from_range() populates this from the source
        # BaseImage at export time, so on a blueprint whose Image Library
        # ids don't resolve locally (or were never set - the live "IL2 VPN
        # Kiosk" blueprint on this host has all three id fields None and
        # only base_image_tag populated) this is the ONLY image reference
        # left.
        matched = _resolve_base_image_tag(base_images_by_tag, base_image_tag)
        image_tag = _image_tag_for_base_image(matched, arch) if matched else base_image_tag
        if image_tag:
            return image_tag

    if template_name:
        # Deprecated predecessor of base_image_tag. catalog_service.py
        # (~lines 94-98) treats it the same way: only as a fallback when
        # base_image_tag is absent. blueprint_service.py:291 resolves it
        # via the same BaseImage.name lookup as base_image_name above (see
        # C3 note above for why this is a live query, not a dict lookup).
        matched = db.query(BaseImage).filter(BaseImage.name == template_name).first()
        image_tag = _image_tag_for_base_image(matched, arch) if matched else template_name
        if image_tag:
            return image_tag

    return None


def resolve_pool_images(db) -> list[str]:
    """Images to pre-pull into pool members: the union across all blueprints.

    Mirrors the resolution in range_deployment_service.deploy_range's Stage 3
    (unique_images dict) / _resolve_range_images() so that a pooled member
    holds the same tags a real deploy will ask for. A near-miss means
    members are provisioned and then never claimed - the pool becomes a
    silent no-op that burns RAM/disk while every deploy still runs cold.

    settings.range_pool_images, when non-empty, always wins and is returned
    verbatim without touching the database - explicit config beats inference.

    Otherwise, walks every RangeBlueprint's config["vms"] (the same VMConfig
    shape blueprint_service.py materializes into real VM rows) and resolves
    each VM's image source. The base-image identifier fields (base_image_id,
    base_image_name, base_image_tag, template_name) are resolved together as
    ONE fall-through chain, in that exact order, by
    _resolve_vm_base_image_tag() - see its docstring, which mirrors
    blueprint_service.create_range_from_blueprint() exactly (this is a
    correction: an earlier version of this function used an if/elif chain
    that both stopped dead on an unresolvable base_image_id and had no
    base_image_name branch at all). Only if none of those four produce an
    image does resolution fall through to golden_image_id, then snapshot_id
    - matching range_deployment_service._resolve_range_images(), which
    always prefers a VM's base_image_id (however it was resolved) over
    golden_image_id or snapshot_id at real deploy time:
      - base_image_id / base_image_name / base_image_tag / template_name ->
        see _resolve_vm_base_image_tag().
      - golden_image_id -> GoldenImage.docker_image_tag or docker_image_id.
      - snapshot_id -> Snapshot.docker_image_tag or docker_image_id.

    Returns a sorted, de-duplicated list so the pg.pool_image_set label is
    stable regardless of blueprint iteration order.
    """
    settings = get_settings()
    if settings.range_pool_images:
        return list(settings.range_pool_images)

    all_base_images = db.query(BaseImage).all()
    base_images = {str(img.id): img for img in all_base_images}
    base_images_by_tag = {
        img.docker_image_tag: img for img in all_base_images if img.docker_image_tag
    }
    # NOTE: deliberately no base_images_by_name dict (C3) - BaseImage.name is
    # not unique, so a preloaded {name: img} map is last-write-wins and can
    # disagree with blueprint_service's .filter(name == ...).first(). See
    # _resolve_vm_base_image_tag()'s docstring: name/template_name lookups
    # issue that same live query instead.
    golden_images = {str(img.id): img for img in db.query(GoldenImage).all()}
    snapshots = {str(s.id): s for s in db.query(Snapshot).all()}

    images: set[str] = set()

    for blueprint in db.query(RangeBlueprint).all():
        vms = (blueprint.config or {}).get("vms") or []
        for vm in vms:
            golden_image_id = vm.get("golden_image_id")
            snapshot_id = vm.get("snapshot_id")
            arch = vm.get("arch")

            image_tag = _resolve_vm_base_image_tag(
                db,
                base_images,
                base_images_by_tag,
                base_image_id=vm.get("base_image_id"),
                base_image_name=vm.get("base_image_name"),
                base_image_tag=vm.get("base_image_tag"),
                template_name=vm.get("template_name"),
                arch=arch,
            )

            if not image_tag and golden_image_id:
                golden_img = golden_images.get(str(golden_image_id))
                if golden_img:
                    # Shared with the deploy path. Reading docker_image_tag
                    # alone missed every disk-based golden image, which has
                    # none -- so its runtime never entered the pre-pull set
                    # and no member ever matched a Windows range.
                    image_tag = runtime_for_image(golden_img, arch=arch, mirror=_REGISTRY_MIRROR)

            if not image_tag and snapshot_id:
                snapshot = snapshots.get(str(snapshot_id))
                if snapshot:
                    image_tag = snapshot.docker_image_tag or snapshot.docker_image_id

            if image_tag:
                images.add(image_tag)

    # Golden images are deployed from directly, not only through blueprints --
    # capturing an image and building a range on it is the normal flow. Their
    # runtimes are few (a Windows image and a Linux one resolve to the same
    # dockur/qemu references), so adding them costs little and is the
    # difference between a warm claim and cold provisioning.
    for golden_img in golden_images.values():
        if not golden_img.disk_image_path:
            continue  # a container golden image is already covered above
        runtime = runtime_for_image(golden_img, mirror=_REGISTRY_MIRROR)
        if runtime:
            images.add(runtime)

    return sorted(images)


class RangePoolService:
    """Manages the warm pool of pre-booted DinD range containers."""

    def __init__(self):
        settings = get_settings()
        self.redis = redis.from_url(settings.redis_url)
        self.docker = docker.from_env()
        self.dind = get_dind_service()
        self.docker_service = get_docker_service()

    # -- provisioning ---------------------------------------------------

    async def provision_member(self) -> Optional[str]:
        """Boot a fresh, pre-imaged DinD pool member and mark it ready.

        Reuses dind_service.create_range_container() with a synthetic pool
        uuid as range_id (and no range_name) so naming, mounts and resource
        limits stay identical to a cold-provisioned range. The container is
        labelled pg.type=pool and carries NO pg.range_id label - it is not a
        range until claim() renames it into one.

        Returns the new container's id, or None if no images could be
        resolved (see resolve_pool_images) - refusing to provision a member
        that could never be claimed rather than silently wasting resources.
        """
        settings = get_settings()
        SessionLocal = get_session_local()
        db = SessionLocal()
        try:
            images = resolve_pool_images(db)
        finally:
            db.close()

        if not images:
            logger.warning(
                "Warm pool: no images could be resolved (range_pool_images is empty "
                "and no blueprint resolves to a container-type BaseImage/GoldenImage/"
                "Snapshot). Refusing to provision a pool member - it could never be "
                "claimed and would just waste RAM/disk. Set range_pool_images "
                "explicitly, or make sure at least one blueprint references a "
                "container image."
            )
            return None

        image_set_label = _encode_image_set_label(images)

        pool_id = str(uuid.uuid4())
        info = await self.dind.create_range_container(
            range_id=pool_id,
            range_name=None,
            memory_limit=settings.range_default_memory,
            cpu_limit=settings.range_default_cpu,
            labels={
                "pg.type": "pool",
                POOL_ID_LABEL: pool_id,
                POOL_IMAGE_SET_LABEL: image_set_label,
            },
        )
        container_id = info["container_id"]

        failed_images = []
        for image in images:
            try:
                await self.docker_service.pull_image_to_dind(
                    range_id=pool_id,
                    docker_url=info["docker_url"],
                    image=image,
                )
            except Exception as e:
                logger.warning(f"Pool member {container_id[:12]}: failed to pre-pull {image}: {e}")
                failed_images.append(image)

        if images and len(failed_images) == len(images):
            # NOTHING pulled. Marking this ready anyway would advertise
            # POOL_IMAGE_SET_LABEL for images the member does not actually
            # have - claim()'s issuperset() check would still (correctly)
            # hand it out since the label lies about what's present, and
            # Stage 3 pulls unconditionally regardless, so this isn't a
            # correctness break - but it silently turns every "warm" claim
            # of this member into full cold-provisioning cost with no
            # signal anywhere that the pool did nothing useful. Destroy it
            # exactly like an unhealthy member (I3/I4) instead of sadd'ing
            # it into POOL_READY_KEY: this container was never claimed and
            # never handed to a caller, so destroying it is safe.
            #
            # I2: use the NO-refill discard here, not _discard_unhealthy_member.
            # This runs from inside provision_member(), which itself runs from
            # inside refill_pool_task's shortfall loop. With a registry down,
            # EVERY provision_member() call in that loop hits this branch, and
            # each _discard_unhealthy_member() call used to enqueue its own
            # refill_pool_task - which finds the same shortfall (nothing
            # succeeded) and destroys+enqueues again. That never terminates:
            # it churns DinD containers/volumes on the same threads
            # worker-deploy uses for real deploys, forever, for as long as the
            # registry stays down. Retrying immediately cannot help (the
            # failure wasn't this container, it was every pull it attempted),
            # and the next teardown or API startup already enqueues a refill
            # unconditionally (see enqueue_pool_refill's call sites), so
            # nothing is lost by not also enqueueing one here.
            logger.warning(
                f"Pool member {container_id[:12]}: all {len(images)} pre-pull(s) failed "
                f"({', '.join(failed_images)}); destroying instead of marking ready - "
                "a member with none of its advertised images would silently cost full "
                "cold-provisioning time on its next claim."
            )
            try:
                container = self.docker.containers.get(container_id)
            except Exception:
                container = None
            self._discard_unhealthy_member_no_refill(container_id, container)
            return None

        if failed_images:
            logger.warning(
                f"Pool member {container_id[:12]}: {len(failed_images)}/{len(images)} "
                f"pre-pull(s) failed ({', '.join(failed_images)}); marking ready anyway - "
                "a claim needing one of the missing images will be rejected by "
                "issuperset() and fall back to cold provisioning."
            )

        self.redis.sadd(POOL_READY_KEY, container_id)
        logger.info(
            f"Pool member {container_id[:12]} provisioned and marked ready "
            f"({len(images) - len(failed_images)}/{len(images)} images)"
        )
        return container_id

    # -- reconciliation ---------------------------------------------------

    def _claimed_container_ids(self) -> Optional[set]:
        """Container ids that belong to a real range. None if unreadable."""
        SessionLocal = get_session_local()
        db = SessionLocal()
        try:
            rows = (
                db.query(Range.dind_container_id).filter(Range.dind_container_id.isnot(None)).all()
            )
            return {r[0] for r in rows if r[0]}
        except Exception as e:
            logger.warning(f"Could not read claimed container ids: {e}")
            return None
        finally:
            db.close()

    def reconcile_ready_set(self) -> int:
        """Re-adopt running pool members that Redis has forgotten.

        Which members are claimable is recorded in exactly one place: the
        POOL_READY_KEY set in Redis. Redis here has no AOF and its compose
        service has no volume, so recreating that container - which any
        `compose up` does whenever the service definition changes - empties
        the set while every member container keeps running. They stay
        healthy and hold their full memory reservation, and claim() starts
        with an spop that now returns nothing, so they can never be handed
        out again. The pool silently stops working and leaks a DinD
        container per member, with nothing anywhere reporting it. That is
        how a redeploy turned a 15s warm deploy back into a 52s cold one.

        The labels on the containers are the durable record. Rebuild from
        them.

        SECURITY: a claimed member is a live tenant's range. Re-adding one
        to the ready set would hand a running tenant's container - and
        whatever is inside it - to whoever claims next. Two independent
        conditions must BOTH hold before adopting:

          1. the container still carries its unclaimed pool name. claim()
             renames as its last step, after every health check, precisely
             so that a renamed container is provably spoken for.
          2. no Range row references the container.

        Either would very likely do on its own. A mistake here is a
        cross-tenant leak, so both are required, and a DB that cannot be
        read means adopting nothing rather than guessing.

        Returns the number of members adopted. Never raises.
        """
        try:
            containers = self.docker.containers.list(
                filters={"label": "pg.type=pool", "status": "running"}
            )
        except Exception as e:
            logger.warning(f"Pool reconciliation could not list containers: {e}")
            return 0

        if not containers:
            return 0

        try:
            known = {
                m.decode() if isinstance(m, bytes) else m
                for m in self.redis.smembers(POOL_READY_KEY)
            }
        except Exception as e:
            logger.warning(f"Pool reconciliation could not read the ready set: {e}")
            return 0

        claimed_ids = self._claimed_container_ids()
        if claimed_ids is None:
            logger.warning(
                "Pool reconciliation skipped: the range table could not be read, and "
                "adopting without it could hand a live range to another tenant."
            )
            return 0

        adopted = 0
        for container in containers:
            try:
                if container.id in known:
                    continue

                labels = getattr(container, "labels", None)
                labels = labels if isinstance(labels, dict) else {}
                pool_id = labels.get(POOL_ID_LABEL)
                if not pool_id:
                    # Pre-dates the label, or hand-made. Not ours to adopt.
                    continue

                name = (getattr(container, "name", "") or "").lstrip("/")
                if name != self.dind._get_container_name(pool_id, None):
                    # Renamed => claim() handed it out. Never re-adopt.
                    continue

                if container.id in claimed_ids:
                    continue

                self.redis.sadd(POOL_READY_KEY, container.id)
                adopted += 1
                logger.info(
                    f"Pool reconciliation adopted orphaned member {container.id[:12]} "
                    f"({name}); it was running but absent from the ready set."
                )
            except Exception as e:
                logger.warning(f"Pool reconciliation skipped a container: {e}")

        if adopted:
            logger.info(
                f"Pool reconciliation adopted {adopted} orphaned member(s) - the ready "
                "set had been lost (most likely a Redis restart)."
            )
        return adopted

    # -- claiming ---------------------------------------------------------

    def claim(self, range_id: str, range_name: str, required_images: list[str]) -> Optional[dict]:
        """Atomically claim a ready pool member for range_id, or return None.

        Every exception is caught here - the pool is an optimisation, never a
        correctness dependency. See the module docstring.

        Health checks run in this order, and the container is renamed (and
        thus irreversibly handed to the caller) only after ALL of them pass:
        running -> image set covers required_images -> has a usable mgmt IP
        -> inner Docker daemon responds. A failure at any step discards the
        member (I3): an image-set mismatch is sadd'd straight back (never
        touched), everything else destroys the container + volume and
        enqueues a refill.
        """
        try:
            while True:
                member_id = self.redis.spop(POOL_READY_KEY)
                if not member_id:
                    return None
                if isinstance(member_id, bytes):
                    member_id = member_id.decode()

                try:
                    container = self.docker.containers.get(member_id)
                except docker.errors.NotFound:
                    logger.warning(
                        f"Pool member {member_id[:12]} vanished before claim; discarding"
                    )
                    self._discard_unhealthy_member(member_id, None)
                    continue
                except Exception as e:
                    logger.warning(f"Pool member {member_id[:12]} lookup failed; discarding: {e}")
                    self._discard_unhealthy_member(member_id, None)
                    continue

                if getattr(container, "status", None) != "running":
                    logger.warning(f"Pool member {member_id[:12]} is not running; discarding")
                    self._discard_unhealthy_member(member_id, container)
                    continue

                labels = getattr(container, "labels", None)
                labels = labels if isinstance(labels, dict) else {}

                if required_images:
                    # Compared by repository, not by exact reference. The label
                    # is written from tags, so a pinned digest
                    # (dockurr/windows@sha256:...) never matched it and every
                    # Windows range fell back to cold provisioning -- losing the
                    # warm container to avoid a pull it had to do anyway. A
                    # member carrying the right repo is still worth claiming;
                    # the exact digest is pulled into it if absent.
                    have = {
                        repo_of(i) for i in _parse_image_set_label(labels.get(POOL_IMAGE_SET_LABEL))
                    }
                    want = {repo_of(i) for i in required_images}
                    if not have.issuperset(want):
                        logger.info(
                            f"Pool member {member_id[:12]} image set does not cover required images; "
                            "falling back to cold provisioning"
                        )
                        # I3 / security invariant: safe to sadd back - this
                        # member was never renamed or handed to a tenant.
                        self.redis.sadd(POOL_READY_KEY, member_id)
                        return None

                mgmt_ip = self._resolve_mgmt_ip(container)
                if not mgmt_ip:
                    logger.warning(f"Pool member {member_id[:12]} has no usable IP; discarding")
                    self._discard_unhealthy_member(member_id, container)
                    continue

                settings = get_settings()
                docker_port = settings.dind_docker_port
                docker_url = f"tcp://{mgmt_ip}:{docker_port}"

                if not self._probe_inner_docker(docker_url):
                    logger.warning(
                        f"Pool member {member_id[:12]} inner Docker daemon is not responding; discarding"
                    )
                    self._discard_unhealthy_member(member_id, container)
                    continue

                # Every health check passed - only now is it safe to hand
                # this container to a caller. Once renamed it is a live
                # range and must never be sadd'd back (security invariant).
                new_name = self.dind._get_container_name(range_id, range_name)
                container.rename(new_name)
                container.reload()

                pool_id = labels.get(POOL_ID_LABEL)
                volume_name = _volume_name_for_pool_id(pool_id) if pool_id else None

                logger.info(
                    f"Claimed pool member {member_id[:12]} for range {range_id} as '{new_name}'"
                )
                return {
                    "container_name": new_name,
                    "container_id": container.id,
                    "mgmt_ip": mgmt_ip,
                    "docker_url": docker_url,
                    "docker_port": docker_port,
                    "volume_name": volume_name,
                }
        except Exception as e:
            logger.warning(
                f"Pool claim for range {range_id} failed, falling back to cold provisioning: {e}"
            )
            return None

    def _resolve_mgmt_ip(self, container) -> Optional[str]:
        """Mirror dind_service.create_range_container()'s IP preference
        exactly (I6): prefer self.dind.ranges_network, fall back to any
        network reporting an IP (with a warning) otherwise. Unlike the cold
        path, "no usable IP" is not raised here - the caller treats it as
        an unhealthy member rather than a hard deploy failure.
        """
        networks = container.attrs.get("NetworkSettings", {}).get("Networks", {})
        ranges_network = self.dind.ranges_network
        preferred = networks.get(ranges_network)
        if isinstance(preferred, dict) and preferred.get("IPAddress"):
            return preferred["IPAddress"]
        for net_name, net_info in networks.items():
            if isinstance(net_info, dict) and net_info.get("IPAddress"):
                logger.warning(
                    f"Pool member using IP from '{net_name}' network instead of '{ranges_network}'"
                )
                return net_info["IPAddress"]
        return None

    def _probe_inner_docker(self, docker_url: str) -> bool:
        """Short, bounded, SYNCHRONOUS probe of the inner Docker daemon (I4).

        Cold provisioning waits on dind_service._wait_for_docker_ready(),
        which is async and retries once a second for up to the full startup
        timeout (config's dind_startup_timeout, default 60s). claim() is a
        synchronous method that may itself be called (see
        range_deployment_service._acquire_dind) from inside code that is
        already running an asyncio event loop - bridging to an async
        helper there via a freshly created loop's run_until_complete()
        raises "Cannot run the event loop while another loop is running"
        every single time in that situation, which would make this probe
        (and therefore the whole pool) fail permanently in production. A
        plain synchronous docker-py client with a short socket timeout
        avoids asyncio entirely and works the same regardless of what event
        loop, if any, is running on this thread.
        """
        client = None
        try:
            client = docker.DockerClient(
                base_url=docker_url, timeout=POOL_CLAIM_PROBE_TIMEOUT_SECONDS
            )
            client.ping()
            return True
        except Exception as e:
            logger.warning(f"Pool member inner Docker daemon probe failed at {docker_url}: {e}")
            return False
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    def _discard_unhealthy_member(self, member_id: str, container) -> None:
        """Destroy an orphaned, unhealthy pool member and enqueue a refill
        (I3/I4/I6). NEVER call this on a container that has been renamed or
        otherwise handed to a caller - only on members discarded before
        that point. See the module's security invariant.

        Best-effort throughout: the container may already be gone (a
        docker.errors.NotFound during lookup means there is nothing to
        remove) or otherwise unavailable, in which case this can only
        enqueue the refill.
        """
        volume_name = None
        if container is not None:
            try:
                labels = getattr(container, "labels", None)
                labels = labels if isinstance(labels, dict) else {}
                pool_id = labels.get(POOL_ID_LABEL)
                volume_name = _volume_name_for_pool_id(pool_id) if pool_id else None
            except Exception as e:
                logger.warning(
                    f"Could not read labels for unhealthy pool member {member_id[:12]}: {e}"
                )
            try:
                container.remove(force=True)
            except Exception as e:
                logger.warning(f"Failed to remove unhealthy pool member {member_id[:12]}: {e}")

        if volume_name:
            try:
                self.docker.volumes.get(volume_name).remove(force=True)
            except docker.errors.NotFound:
                pass
            except Exception as e:
                logger.warning(
                    f"Failed to remove volume {volume_name} for pool member {member_id[:12]}: {e}"
                )

        self._enqueue_refill()

    def _enqueue_refill(self) -> None:
        try:
            from proving_ground.tasks.pool import enqueue_pool_refill

            enqueue_pool_refill()
        except Exception as e:
            logger.warning(f"Could not enqueue pool refill: {e}")

    # -- maintenance --------------------------------------------------------

    def reap_expired_members(self, ttl_hours: int) -> int:
        """Destroy ready pool members older than ttl_hours (I4: this makes
        config.range_pool_member_ttl_hours a live setting instead of dead
        config read nowhere).

        A member is removed from POOL_READY_KEY BEFORE being inspected or
        destroyed, and put back immediately if it turns out not to be
        expired. This can't race a concurrent claim() into destroying a
        container that was just handed to a caller: once a member is gone
        from the ready set, claim() can never see (and therefore never
        rename) it.

        Returns the number of members destroyed. Never raises.
        """
        if ttl_hours <= 0:
            return 0

        cutoff = datetime.now(timezone.utc) - timedelta(hours=ttl_hours)
        reaped = 0

        try:
            member_ids = list(self.redis.smembers(POOL_READY_KEY))
        except Exception as e:
            logger.warning(f"Could not read pool members for TTL reap: {e}")
            return 0

        for raw_id in member_ids:
            member_id = raw_id.decode() if isinstance(raw_id, bytes) else raw_id
            try:
                removed = self.redis.srem(POOL_READY_KEY, member_id)
                if not removed:
                    # Claimed by someone else between smembers() and here.
                    continue

                container = self.docker.containers.get(member_id)
                created_at = _parse_docker_created(container.attrs.get("Created"))
                if created_at is not None and created_at < cutoff:
                    logger.info(
                        f"Pool member {member_id[:12]} exceeded TTL ({ttl_hours}h); destroying"
                    )
                    # The follow-up refill in refill_pool_task's own run
                    # will account for this, so don't also enqueue one here
                    # per reaped member.
                    self._discard_unhealthy_member_no_refill(member_id, container)
                    reaped += 1
                else:
                    # Not actually expired (or age unknown) - put it back.
                    self.redis.sadd(POOL_READY_KEY, member_id)
            except docker.errors.NotFound:
                logger.info(f"Pool member {member_id[:12]} vanished during TTL reap; already gone")
                reaped += 1
            except Exception as e:
                logger.warning(f"TTL reap failed for pool member {member_id[:12]}: {e}")
                # Don't strand it out of the ready set on an unexpected error.
                try:
                    self.redis.sadd(POOL_READY_KEY, member_id)
                except Exception:
                    pass

        return reaped

    def _discard_unhealthy_member_no_refill(self, member_id: str, container) -> None:
        """Same destruction as _discard_unhealthy_member(), without also
        enqueueing a refill. Used by:

        - reap_expired_members(), which runs as part of refill_pool_task
          itself and will already top the pool back up in the same run.
        - provision_member()'s all-pre-pulls-failed path (I2): that member
          was never claimed/usable, and it's discovered from inside
          refill_pool_task's own shortfall loop - enqueueing another refill
          from there is a self-sustaining amplifier under a sustained
          failure (e.g. registry down): every provision attempt in the
          loop fails the same way and each one used to enqueue yet another
          refill, which never terminates. Retrying immediately cannot help,
          and the next teardown or API startup already enqueues a refill
          unconditionally, so nothing is lost by not also enqueueing one
          here.
        """
        volume_name = None
        try:
            labels = getattr(container, "labels", None)
            labels = labels if isinstance(labels, dict) else {}
            pool_id = labels.get(POOL_ID_LABEL)
            volume_name = _volume_name_for_pool_id(pool_id) if pool_id else None
        except Exception as e:
            logger.warning(f"Could not read labels for expired pool member {member_id[:12]}: {e}")
        try:
            container.remove(force=True)
        except Exception as e:
            logger.warning(f"Failed to remove expired pool member {member_id[:12]}: {e}")

        if volume_name:
            try:
                self.docker.volumes.get(volume_name).remove(force=True)
            except docker.errors.NotFound:
                pass
            except Exception as e:
                logger.warning(
                    f"Failed to remove volume {volume_name} for expired pool member {member_id[:12]}: {e}"
                )

    # -- introspection ------------------------------------------------------

    def pool_depth(self) -> int:
        """Number of members currently ready to claim. 0 on any error."""
        try:
            return int(self.redis.scard(POOL_READY_KEY))
        except Exception as e:
            logger.warning(f"Could not read pool depth: {e}")
            return 0


# Singleton instance
_range_pool_service: Optional[RangePoolService] = None


def get_pool_service() -> RangePoolService:
    """Get the singleton RangePoolService instance."""
    global _range_pool_service
    if _range_pool_service is None:
        _range_pool_service = RangePoolService()
    return _range_pool_service
