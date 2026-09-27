"""Range endpoints must check who is asking.

CLAUDE.md carried this as a known live defect: `GET`, `PUT`, `DELETE`,
`/deploy`, `/start`, `/stop` and `/teardown` took `current_user` — so they
required a login — and then never compared it to anything. Any authenticated
user could tear down any range by naming its id, on a deployment reachable
from the internet.

The fix needed two checks, not one. `check_resource_access` grants access to a
resource with no tags, because "no tags = public" is a reasonable answer to
"may I look at this". It is not a reasonable answer to "may I delete this":
under it one engineer could destroy another engineer's untagged range, and a
tag shared for viewing would also hand over the power to tear it down.

So control is owner-or-admin, and visibility stays as it was.
"""

import re
import pathlib
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException

from proving_ground.api.deps import check_resource_control
from proving_ground.api.snapshots import check_snapshot_control
from proving_ground.models.range import Range
from proving_ground.models.vm import VM

RANGES = pathlib.Path(__file__).resolve().parents[2] / "proving_ground" / "api" / "ranges.py"


def user(*, admin=False, uid=None):
    return SimpleNamespace(id=uid or uuid4(), is_admin=admin)


class TestControlIsOwnerOrAdmin:
    def test_the_owner_may_control_their_range(self):
        u = user()
        assert check_resource_control("range", uuid4(), u, None, u.id) is True

    def test_an_admin_may_control_any_range(self):
        assert check_resource_control("range", uuid4(), user(admin=True), None, uuid4()) is True

    def test_a_stranger_may_not(self):
        """The defect itself: an authenticated non-owner could delete anything."""
        with pytest.raises(HTTPException) as e:
            check_resource_control("range", uuid4(), user(), None, uuid4())
        assert e.value.status_code == 403

    def test_an_unowned_resource_is_not_therefore_everyones(self):
        """owner_id=None must not fall through to 'allowed'.

        A range whose creator was deleted has no owner. Treating that as public
        would make every orphaned range destroyable by anyone.
        """
        with pytest.raises(HTTPException) as e:
            check_resource_control("range", uuid4(), user(), None, None)
        assert e.value.status_code == 403

    def test_it_never_returns_false(self):
        """A caller who ignores the return value must still be protected, so
        denial is an exception rather than a value to forget to check."""
        src = pathlib.Path(
            pathlib.Path(__file__).resolve().parents[2] / "proving_ground" / "api" / "deps.py"
        ).read_text()
        body = src[src.index("def check_resource_control") : src.index("# Type aliases")]
        assert "return False" not in body
        assert "raise HTTPException" in body


class TestControlIsStricterThanAccess:
    def test_tags_do_not_confer_control(self):
        """The distinction this function exists for.

        check_resource_access allows an untagged resource through, and allows
        anyone holding a matching tag. check_resource_control consults neither:
        it never reads tags at all.
        """
        src = pathlib.Path(
            pathlib.Path(__file__).resolve().parents[2] / "proving_ground" / "api" / "deps.py"
        ).read_text()
        body = src[src.index("def check_resource_control") : src.index("# Type aliases")]
        assert "ResourceTag" not in body, "control is consulting tags; tags are visibility"
        assert "has_any_tag" not in body
        assert (
            "get_student_accessible_range_ids" not in body
        ), "an assignment is permission to use a lab, not to delete it"


def _endpoints():
    """(line, verb, path, guards) for every /{range_id} route in ranges.py."""
    lines = RANGES.read_text().splitlines()
    out = []
    route = None
    start = 0
    for i, line in enumerate(lines):
        m = re.match(r'@router\.(get|put|post|delete)\("([^"]+)"', line.strip())
        if not m:
            continue
        if route and "{range_id}" in route[1]:
            body = "\n".join(lines[start:i])
            out.append((start + 1, route[0].upper(), route[1], body))
        route, start = m.groups(), i
    if route and "{range_id}" in route[1]:
        out.append((start + 1, route[0].upper(), route[1], "\n".join(lines[start:])))
    return out


class TestEveryRangeEndpointIsGuarded:
    """The regression that matters is a NEW endpoint added without a check.

    Asserted over the file rather than per-endpoint, so adding one and
    forgetting fails here instead of shipping.
    """

    def test_the_audit_finds_endpoints_at_all(self):
        assert len(_endpoints()) > 20, "the parser found nothing; this proves nothing"

    def test_no_range_endpoint_is_unguarded(self):
        unguarded = [
            f"{verb} {path} (line {ln})"
            for ln, verb, path, body in _endpoints()
            if "check_resource_" not in body
            and "accessible_ids" not in body
            and "is_owner" not in body
        ]
        assert (
            not unguarded
        ), "These range endpoints accept any authenticated user:\n  " + "\n  ".join(unguarded)

    def test_mutating_endpoints_require_control_not_merely_access(self):
        """A read check on a destructive endpoint is the original bug wearing a
        seatbelt: it passes for any user when the range has no tags."""
        weak = [
            f"{verb} {path} (line {ln})"
            for ln, verb, path, body in _endpoints()
            if verb in {"PUT", "DELETE"}
            and "check_resource_control" not in body
            and "is_owner" not in body
        ]
        assert (
            not weak
        ), "These endpoints change a range but only check read access:\n  " + "\n  ".join(weak)

    def test_the_destructive_endpoints_named_in_claude_md_are_covered(self):
        """Named explicitly so the specific reported defect cannot regress."""
        want = {
            ("PUT", "/{range_id}"),
            ("DELETE", "/{range_id}"),
            ("POST", "/{range_id}/deploy"),
            ("POST", "/{range_id}/start"),
            ("POST", "/{range_id}/stop"),
            ("POST", "/{range_id}/teardown"),
        }
        found = {
            (verb, path) for _, verb, path, body in _endpoints() if "check_resource_control" in body
        }
        assert want <= found, f"not controlled: {sorted(want - found)}"

    def test_read_endpoints_are_not_accidentally_locked_to_owners(self):
        """Over-tightening breaks a student opening the lab assigned to them,
        which is a real regression and a quiet one — it looks like a bug in the
        console, not in authorization."""
        overtight = [
            f"GET {path} (line {ln})"
            for ln, verb, path, body in _endpoints()
            if verb == "GET" and "check_resource_control" in body
        ]
        assert not overtight, "These read endpoints demand ownership:\n  " + "\n  ".join(overtight)


# --- child resources -------------------------------------------------------

API = pathlib.Path(__file__).resolve().parents[2] / "proving_ground" / "api"

# Endpoints that legitimately have no range to check against, with the reason.
# Listed explicitly so "unguarded" always means "someone forgot", never "this
# one is fine, trust me".
EXEMPT = {
    ("networks.py", "GET", ""): "collection; guarded by range_id parameter check",
    ("vms.py", "GET", "/allowed-devices"): "static list of device types, no range involved",
    # Not unguarded — guarded differently, and it has to be. This IS the
    # authorization endpoint: Traefik's forwardAuth calls it for the iframe's
    # request, which has no session to check. Its credential is the console
    # ticket cookie, and test_console_authorization.py asserts it refuses
    # without one, refuses a ticket for another VM, and refuses an access token
    # presented in its place.
    ("vms.py", "GET", "/console-authz"): "forwardAuth target; the ticket cookie is the credential",
    ("vms.py", "POST", "/{vm_id}/provision"): "delegates to _start_or_provision_vm, which checks",
    ("ranges.py", "GET", ""): "listing is filtered by visibility, not gated",
    ("ranges.py", "POST", ""): "creating your own range",
    ("ranges.py", "GET", "/export/jobs/{job_id}"): "deprecated; keyed by unguessable job id",
    ("ranges.py", "POST", "/import/validate"): "validates an uploaded file, touches no range",
    ("ranges.py", "POST", "/import/execute"): "creates a new range owned by the caller",
    ("ranges.py", "POST", "/import/load-images"): "loads images into a range being imported",
    ("ranges.py", "POST", "/import"): "creates a new range owned by the caller",
    # The image library is read-by-anyone, changed-by-admins. Whoever builds a
    # range has to see what there is to pick from; the entries themselves are
    # install-wide shared state with no owner, so only an administrator can
    # speak for one. Every read below is that rule, not an oversight.
    ("images.py", "GET", "/library"): "reading the library; picking an image needs seeing it",
    ("images.py", "GET", "/library/stats"): "counts of library rows; names no resource",
    ("images.py", "GET", "/base"): "reading the library",
    ("images.py", "GET", "/base/{image_id}"): "reading one library entry",
    ("images.py", "GET", "/golden"): "reading the library",
    ("images.py", "GET", "/golden/{image_id}"): "reading one library entry",
    ("images.py", "GET", "/snapshots"): "reading the library's global snapshots",
    ("snapshots.py", "GET", ""): "listing snapshot metadata; the library is a read surface",
    ("snapshots.py", "GET", "/{snapshot_id}"): "reading one snapshot's metadata",
}

# Routes this audit finds unguarded that are not this file's to fix. Kept apart
# from EXEMPT deliberately: an exemption says there is nothing here to check,
# these say there is and nobody does. An entry is a defect with a name, not a
# dispensation — delete it the moment the route grows a check.
KNOWN_UNGUARDED = {
    ("ranges.py", "POST", "/{range_id}/clone"): (
        "clones any range by id into a new one owned by the caller, so it copies "
        "the contents of a range the caller may not be allowed to see"
    ),
}

GUARDS = (
    "check_artifact_access",
    "check_artifact_control",
    "may_browse_artifacts",
    "check_resource_access",
    "check_resource_control",
    "check_range_access",
    "check_range_control",
    "check_snapshot_control",
    "can_access_vm_console",
    "check_console_access",
    "accessible_ids",
    "is_owner",
    "filter_by_visibility",
    # A dependency rather than a call: `current_user: AdminUser` is the check,
    # and it runs before the handler body does.
    "AdminUser",
    # An owner comparison written out in the handler instead of through the
    # helper. Still a check, just not the one everything else uses. Spelled
    # against the loaded row, because `Range.created_by != current_user.id` is
    # a query filter narrowing a listing, and counting that as a guard is how
    # a route with no check at all would read as guarded.
    "range_obj.created_by != current_user.id",
)

_DECORATOR = re.compile(r"@router\.(get|put|post|patch|delete)\(")
_FIRST_STRING = re.compile(r'"([^"]*)"')


def _route_path(src, i):
    """The path argument of the decorator starting on line i.

    Decorators are written across several lines as often as not — POST
    /snapshots and POST /images/golden/import both are — and the first version
    of this parser required the path on the decorator's own line, so it never
    saw them. A route this audit cannot see is a route it cannot report, which
    is the same silence the audit exists to break.
    """
    m = _FIRST_STRING.search(src[i].split("(", 1)[1])
    if m:
        return m.group(1)
    for j in range(i + 1, min(i + 8, len(src))):
        if src[j].lstrip().startswith(("def ", "async def ", "@")):
            break
        m = _FIRST_STRING.search(src[j])
        if m:
            return m.group(1)
    return None


def _routes(filename):
    src = (API / filename).read_text().splitlines()
    out, route, start = [], None, 0
    for i, line in enumerate(src):
        m = _DECORATOR.match(line.strip())
        if not m:
            continue
        path = _route_path(src, i)
        if path is None:
            continue
        if route:
            out.append((route[0], route[1], "\n".join(src[start:i])))
        route, start = (m.group(1).upper(), path), i
    if route:
        out.append((route[0], route[1], "\n".join(src[start:])))
    return out


class TestChildResourcesInheritTheRangesAuthorization:
    """networks.py had no 403 anywhere in it, across ten endpoints.

    A network or a VM has no owner of its own: it belongs to a range, and
    deleting someone else's network is destroying their range a piece at a
    time. So both answer to the range's rule.
    """

    @pytest.mark.parametrize(
        "filename",
        ["networks.py", "vms.py", "ranges.py", "images.py", "snapshots.py", "artifacts.py"],
    )
    def test_every_endpoint_is_guarded_or_explicitly_exempt(self, filename):
        routes = _routes(filename)
        assert routes, f"{filename}: parsed no routes, so this proves nothing"
        missing = [
            f"{verb} {path or '(collection)'}"
            for verb, path, body in routes
            if not any(g in body for g in GUARDS)
            and (filename, verb, path) not in EXEMPT
            and (filename, verb, path) not in KNOWN_UNGUARDED
        ]
        assert not missing, f"{filename} has unguarded endpoints:\n  " + "\n  ".join(missing)

    def test_the_parser_sees_patch_and_multi_line_decorators(self):
        """Both shapes were invisible to the earlier parser, and both hid live
        routes — including the two this file was extended to cover."""
        images = {(verb, path) for verb, path, _ in _routes("images.py")}
        assert ("PATCH", "/base/{image_id}") in images
        assert ("POST", "/golden/import") in images
        assert ("POST", "") in {(verb, path) for verb, path, _ in _routes("snapshots.py")}

    @pytest.mark.parametrize("filename", ["networks.py", "vms.py"])
    def test_mutating_child_endpoints_require_control(self, filename):
        weak = [
            f"{verb} {path}"
            for verb, path, body in _routes(filename)
            if verb in {"PUT", "DELETE"}
            and "check_range_control" not in body
            and (filename, verb, path) not in EXEMPT
        ]
        assert not weak, f"{filename}: change a resource on a read check only:\n  " + "\n  ".join(
            weak
        )

    def test_the_exempt_list_has_not_gone_stale(self):
        """An exemption for an endpoint that no longer exists hides the next
        one that needs looking at."""
        live = {
            (f, verb, path)
            for f in (
                "networks.py",
                "vms.py",
                "ranges.py",
                "images.py",
                "snapshots.py",
                "artifacts.py",
            )
            for verb, path, _ in _routes(f)
        }
        stale = sorted((set(EXEMPT) | set(KNOWN_UNGUARDED)) - live)
        assert not stale, f"exemptions for endpoints that no longer exist: {stale}"


class TestTheGuardsAreActuallyReached:
    """A check that sits after a return is not a check.

    Both of these shipped past the audit above, which asserted only that the
    guard APPEARED in the function body. It appeared; it was unreachable:

        return network

        check_range_access(network.range_id, current_user, db)

    GET /networks/{network_id} and GET /vms/{vm_id} were wide open while the
    test reported them guarded. Presence is not enforcement, and a test that
    confuses the two is worse than none because it stops anyone looking.

    Found by a security review, not by this file. Hence this class.
    """

    @pytest.mark.parametrize(
        "filename",
        ["networks.py", "vms.py", "ranges.py", "images.py", "snapshots.py", "artifacts.py"],
    )
    def test_no_authorization_check_sits_after_a_return(self, filename):
        src = (API / filename).read_text().splitlines()
        dead = []
        for i, line in enumerate(src):
            if not re.match(r"\s+check_(range|resource|snapshot)_(access|control)\(", line):
                continue
            start = next(j for j in range(i, -1, -1) if re.match(r"^(async )?def \w+", src[j]))
            # A return at function-body indentation before the check means every
            # path to it has already left.
            if any(re.match(r"    return\b", src[j]) for j in range(start + 1, i)):
                dead.append(f"{filename}:{i + 1} — {src[start].strip()[:50]}")
        assert not dead, (
            "These authorization checks are unreachable; the endpoint returns "
            "before them:\n  " + "\n  ".join(dead)
        )

    def test_this_check_would_notice(self):
        """The detector itself, against a known-dead sample."""
        sample = [
            "def handler(vm_id, db, current_user):",
            "    vm = fetch()",
            "    return vm",
            "",
            "    check_range_access(vm.range_id, current_user, db)",
        ]
        i = 4
        start = 0
        assert any(re.match(r"    return\b", sample[j]) for j in range(start + 1, i))


# --- the image library and the snapshots that feed it ----------------------


class TestTheImageLibraryAndItsSnapshots:
    """Twenty routes across images.py and snapshots.py, none of them authorised.

    Every one stopped at CurrentUser, which is a login and not a permission. A
    student could delete the base image every range in the install was built
    on, or restore an instructor's running VM to an older disk underneath them.
    Both pages sit behind a role-gated route in the SPA, which is the
    guarded-looking-and-open shape this file already exists because of.

    The two files answer to different rules because they have different owners.
    A library entry has none — one row, shared install-wide — so changing it is
    admin-only. A snapshot reaches an owner through its VM's range, so the rule
    there is the range's, and an engineer keeps control of their own machines.
    """

    MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

    def test_every_library_mutation_is_admin_only(self):
        weak = [
            f"{verb} {path}"
            for verb, path, body in _routes("images.py")
            if verb in self.MUTATING and "AdminUser" not in body
        ]
        assert (
            not weak
        ), "These change the shared image library on a login alone:\n  " + "\n  ".join(weak)

    def test_the_admin_dependency_is_actually_wired(self):
        """The check above reads source text; this one reads what FastAPI
        resolved. `AdminUser` mentioned in a docstring, or annotated on a
        parameter the handler never receives, would satisfy the first and
        enforce nothing."""
        from proving_ground.api.images import router

        def resolved(dependant):
            for sub in dependant.dependencies:
                yield getattr(sub.call, "__qualname__", "")
                yield from resolved(sub)

        unwired = []
        for route in router.routes:
            if not (set(route.methods) & self.MUTATING):
                continue
            if "require_admin.<locals>.checker" not in set(resolved(route.dependant)):
                unwired.append(f"{sorted(route.methods)} {route.path}")
        assert not unwired, "No admin dependency resolved for:\n  " + "\n  ".join(unwired)

    def test_library_reads_stay_open(self):
        """Over-tightening is a real regression and a quiet one: demand admin
        on the reads and every engineer's range builder loses its image
        pickers, which looks like a broken page rather than a policy."""
        overtight = [
            f"GET {path}"
            for verb, path, body in _routes("images.py")
            if verb == "GET" and "AdminUser" in body
        ]
        assert not overtight, "These reads demand admin:\n  " + "\n  ".join(overtight)

    def test_every_snapshot_mutation_requires_control_of_the_range(self):
        weak = [
            f"{verb} {path or '(collection)'}"
            for verb, path, body in _routes("snapshots.py")
            if verb in self.MUTATING
            and "check_range_control" not in body
            and "check_snapshot_control" not in body
        ]
        assert not weak, "These change a VM on a login alone:\n  " + "\n  ".join(weak)

    def test_snapshots_are_not_locked_to_admins(self):
        """An engineer snapshotting a VM in their own range is the ordinary
        case, and the only button in the product that calls this."""
        assert "AdminUser" not in (API / "snapshots.py").read_text()

    def test_the_routes_named_in_the_report_are_covered(self):
        """Named explicitly so the specific reported defect cannot regress."""
        want = {
            ("images.py", "POST", "/sync-from-cache"),
            ("images.py", "DELETE", "/base/{image_id}"),
            ("images.py", "DELETE", "/golden/{image_id}"),
            ("images.py", "POST", "/golden/import"),
            ("snapshots.py", "POST", ""),
            ("snapshots.py", "POST", "/{snapshot_id}/restore"),
            ("snapshots.py", "DELETE", "/{snapshot_id}"),
        }
        guarded = {
            (f, verb, path)
            for f in ("images.py", "snapshots.py")
            for verb, path, body in _routes(f)
            if any(g in body for g in GUARDS)
        }
        assert want <= guarded, f"not guarded: {sorted(want - guarded)}"


class _Query:
    def __init__(self, row):
        self._row = row

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return self._row


class _Session:
    """Answers db.query(Model).filter(...).first() from a model -> row map."""

    def __init__(self, rows):
        self._rows = rows

    def query(self, model):
        return _Query(self._rows.get(model))


class TestSnapshotControlResolvesThroughTheVM:
    """A snapshot has no owner column; it reaches one through vm -> range."""

    def _snapshot_of(self, owner_id):
        rng = SimpleNamespace(id=uuid4(), created_by=owner_id)
        vm = SimpleNamespace(id=uuid4(), range_id=rng.id)
        snapshot = SimpleNamespace(id=uuid4(), vm_id=vm.id)
        return snapshot, _Session({VM: vm, Range: rng})

    def test_the_owner_of_the_range_may_change_its_snapshot(self):
        owner = user()
        snapshot, db = self._snapshot_of(owner.id)
        check_snapshot_control(snapshot, owner, db)

    def test_a_stranger_may_not(self):
        snapshot, db = self._snapshot_of(uuid4())
        with pytest.raises(HTTPException) as e:
            check_snapshot_control(snapshot, user(), db)
        assert e.value.status_code == 403

    def test_an_orphaned_snapshot_is_not_therefore_everyones(self):
        """vm_id is SET NULL when the VM is deleted. Falling through to
        'allowed' there would leave every snapshot of a torn-down range
        deletable by anyone — the defect again, one indirection along."""
        snapshot = SimpleNamespace(id=uuid4(), vm_id=None)
        with pytest.raises(HTTPException) as e:
            check_snapshot_control(snapshot, user(), _Session({}))
        assert e.value.status_code == 403

    def test_an_admin_may_still_clear_an_orphaned_snapshot(self):
        snapshot = SimpleNamespace(id=uuid4(), vm_id=None)
        check_snapshot_control(snapshot, user(admin=True), _Session({}))

    def test_a_snapshot_whose_vm_row_is_gone_is_treated_as_orphaned(self):
        snapshot = SimpleNamespace(id=uuid4(), vm_id=uuid4())
        with pytest.raises(HTTPException) as e:
            check_snapshot_control(snapshot, user(), _Session({}))
        assert e.value.status_code == 403


def _account(db, role):
    from proving_ground.models.user import User, UserAttribute, UserRole

    account = User(
        username=f"{role}-{uuid4().hex[:8]}",
        email=f"{role}-{uuid4().hex[:8]}@x.invalid",
        hashed_password="x",
        role=UserRole(role) if role in {r.value for r in UserRole} else UserRole.ENGINEER,
        is_active=True,
        is_approved=True,
    )
    db.add(account)
    db.commit()
    db.add(UserAttribute(user_id=account.id, attribute_type="role", attribute_value=role))
    db.commit()
    db.refresh(account)
    return account


class TestTheRoutesThemselvesRefuseAStudent:
    """The last word belongs to a request, not to a grep.

    Both of this file's earlier defects shipped past an audit that asserted the
    guard appeared in the source. It appeared. So these drive the real routers
    with a real token and read the status code.
    """

    @pytest.fixture
    def app(self, db_session):
        from fastapi import FastAPI

        from proving_ground.api.images import router as images_router
        from proving_ground.api.snapshots import router as snapshots_router
        from proving_ground.database import get_db

        api = FastAPI()
        api.include_router(images_router, prefix="/api/v1")
        api.include_router(snapshots_router, prefix="/api/v1")
        api.dependency_overrides[get_db] = lambda: db_session
        return api

    def _client(self, app, account):
        from fastapi.testclient import TestClient

        from proving_ground.utils.security import create_access_token

        client = TestClient(app)
        client.headers["Authorization"] = f"Bearer {create_access_token(account.id)}"
        return client

    def _someone_elses_snapshot(self, db_session):
        from proving_ground.models.range import Range, RangeStatus
        from proving_ground.models.snapshot import Snapshot

        owner = _account(db_session, "engineer")
        rng = Range(name="theirs", status=RangeStatus.DRAFT, created_by=owner.id)
        db_session.add(rng)
        db_session.flush()
        vm = VM(
            range_id=rng.id,
            network_id=uuid4(),
            hostname="dc01",
            ip_address="10.0.0.5",
            cpu=2,
            ram_mb=2048,
            disk_gb=20,
            container_id="deadbeef",
        )
        db_session.add(vm)
        db_session.flush()
        snapshot = Snapshot(vm_id=vm.id, name="before-the-exercise")
        db_session.add(snapshot)
        db_session.commit()
        return owner, vm, snapshot

    def test_a_student_cannot_change_the_image_library(self, app, db_session):
        client = self._client(app, _account(db_session, "student"))
        assert client.post("/api/v1/images/sync-from-cache").status_code == 403
        assert client.delete(f"/api/v1/images/base/{uuid4()}").status_code == 403
        assert client.delete(f"/api/v1/images/golden/{uuid4()}").status_code == 403
        assert client.patch(f"/api/v1/images/base/{uuid4()}", json={}).status_code == 403

    def test_a_student_cannot_import_an_image(self, app, db_session):
        """The import is also the path into the OVA extractor, so this route
        being open was the reach as well as the write."""
        client = self._client(app, _account(db_session, "student"))
        response = client.post(
            "/api/v1/images/golden/import",
            files={"file": ("x.ova", b"not really a tar", "application/octet-stream")},
            data={"name": "x", "os_type": "linux", "vm_type": "linux_vm"},
        )
        assert response.status_code == 403

    def test_an_admin_still_imports(self, app, db_session, monkeypatch):
        """The refusal has to leave the route working for whoever it is for.

        This one is worth a request of its own because the import spells its
        dependencies `AdminUser = None` and `DBSession = None`: FastAPI
        resolves both and the default never arrives, but if that ever stopped
        being true the handler would read `.id` off None inside its blanket
        `except Exception` and answer 500 'Failed to import image' -- a broken
        server where the only real change was meant to be a refusal.
        """
        import proving_ground.services.image_import_service as import_service
        from proving_ground.models.golden_image import GoldenImage

        seen = {}

        class _Stub:
            """Stands in for the service, which needs qemu-img and /data."""

            async def import_vm_image(self, **kwargs):
                seen.update(kwargs)
                golden = GoldenImage(
                    name=kwargs["name"],
                    source="import",
                    os_type=kwargs["os_type"],
                    vm_type=kwargs["vm_type"],
                    created_by=kwargs["user_id"],
                )
                kwargs["db"].add(golden)
                kwargs["db"].commit()
                kwargs["db"].refresh(golden)
                return golden

        monkeypatch.setattr(import_service, "ImageImportService", _Stub)
        admin = _account(db_session, "admin")

        response = self._client(app, admin).post(
            "/api/v1/images/golden/import",
            files={"file": ("x.ova", b"tar", "application/octet-stream")},
            data={"name": "x", "os_type": "linux", "vm_type": "linux_vm"},
        )

        assert response.status_code == 201, response.text
        assert seen["user_id"] == admin.id, "the handler received no user, only an admin check"

    def test_a_student_can_still_read_the_library(self, app, db_session):
        client = self._client(app, _account(db_session, "student"))
        assert client.get("/api/v1/images/base").status_code == 200
        assert client.get("/api/v1/images/golden").status_code == 200

    def test_a_student_cannot_restore_or_delete_another_users_snapshot(self, app, db_session):
        _, vm, snapshot = self._someone_elses_snapshot(db_session)
        client = self._client(app, _account(db_session, "student"))

        assert client.post(f"/api/v1/snapshots/{snapshot.id}/restore").status_code == 403
        assert client.delete(f"/api/v1/snapshots/{snapshot.id}").status_code == 403
        assert (
            client.post("/api/v1/snapshots", json={"vm_id": str(vm.id), "name": "mine"}).status_code
            == 403
        )

    def test_an_engineer_cannot_touch_a_range_that_is_not_theirs(self, app, db_session):
        """Not a student problem. Engineers hold each other's ranges apart by
        the same rule."""
        _, _, snapshot = self._someone_elses_snapshot(db_session)
        client = self._client(app, _account(db_session, "engineer"))
        assert client.post(f"/api/v1/snapshots/{snapshot.id}/restore").status_code == 403

    def test_the_owner_of_the_range_still_deletes_their_own_snapshot(self, app, db_session):
        """Locking these to admins would be the other failure: the one button
        in the product that takes a snapshot is on an engineer's own range."""
        owner, _, snapshot = self._someone_elses_snapshot(db_session)
        client = self._client(app, owner)
        assert client.delete(f"/api/v1/snapshots/{snapshot.id}").status_code == 204
