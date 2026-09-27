"""What a Kubernetes install says when it is asked for the Docker product.

Two failures with one shape. Around thirty routes build a Docker client the
moment they are called and answered a bare `Internal Server Error` on a host
with no socket, never naming the reason. POST /networks and POST /vms did the
opposite and succeeded, writing a row that exists nowhere on the cluster, which
the range page then counts.

Both are covered here because the bar is the same for both: a refusal that names
the substrate and says what to do instead, rather than a 500 or a phantom row.
"""

import uuid

import pytest
from docker.errors import APIError, DockerException
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from proving_ground import main
from proving_ground.api import deps
from proving_ground.api import kubernetes_ranges
from proving_ground.api import networks as networks_api
from proving_ground.api import vms as vms_api
from proving_ground.database import get_db
from proving_ground.models.base_image import BaseImage
from proving_ground.models.network import Network
from proving_ground.models.range import Range, RangeStatus
from proving_ground.models.user import User, UserAttribute, UserRole
from proving_ground.models.vm import VM
from proving_ground.schemas.network import NetworkCreate
from proving_ground.schemas.vm import NetworkInterfaceCreate, VMCreate
from proving_ground.services import docker_service
from proving_ground.services.docker_service import DOCKER_SDK_ERROR, DockerService


@pytest.fixture
def on_kubernetes(monkeypatch):
    monkeypatch.setattr(kubernetes_ranges, "is_kubernetes", lambda: True)


@pytest.fixture
def on_docker(monkeypatch):
    monkeypatch.setattr(kubernetes_ranges, "is_kubernetes", lambda: False)


@pytest.fixture
def probe():
    """A throwaway app carrying only the handler, so the routes can misbehave.

    The handler is the one main registers, not a copy; that it is registered on
    the real app is asserted separately.
    """
    app = FastAPI()
    app.add_exception_handler(DOCKER_SDK_ERROR, main.docker_unavailable)

    @app.get("/probe/no-socket")
    def no_socket():
        raise DockerException("Error while fetching server API version: FileNotFoundError(2)")

    @app.get("/probe/daemon-said-no")
    def daemon_said_no():
        raise APIError("500 Server Error for http+docker://localhost/v1.47/containers/json")

    def a_client() -> None:
        raise DockerException("Error while fetching server API version: FileNotFoundError(2)")

    @app.get("/probe/from-a-dependency")
    def from_a_dependency(_=Depends(a_client)):
        return {}

    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
def admin(db_session):
    tag = uuid.uuid4().hex[:8]
    user = User(
        username=f"admin-{tag}",
        email=f"admin-{tag}@x.invalid",
        hashed_password="x",
        role=UserRole.ADMIN,
        is_active=True,
        is_approved=True,
    )
    db_session.add(user)
    db_session.commit()
    # `User.is_admin` reads the role attributes, not the `role` column, so a
    # user built from the column alone is refused by every AdminUser route.
    db_session.add(UserAttribute(user_id=user.id, attribute_type="role", attribute_value="admin"))
    db_session.commit()
    db_session.refresh(user)
    return user


@pytest.fixture
def running_range(db_session, admin):
    rng = Range(name="probe", status=RangeStatus.RUNNING, created_by=admin.id)
    db_session.add(rng)
    db_session.commit()
    return rng


@pytest.fixture
def base_image(db_session):
    image = BaseImage(
        name="probe-image",
        image_type="container",
        docker_image_tag="probe/image:latest",
        os_type="linux",
        vm_type="container",
    )
    db_session.add(image)
    db_session.commit()
    return image


class TestADockerFailureIsAnswered:
    def test_the_handler_is_registered_on_the_app(self):
        """Without this the probe app below proves nothing about the product."""
        assert DOCKER_SDK_ERROR in main.app.exception_handlers

    def test_on_kubernetes_it_is_a_501_naming_the_substrate(self, probe, on_kubernetes):
        resp = probe.get("/probe/no-socket")
        assert resp.status_code == 501
        detail = resp.json()["detail"]
        assert "Kubernetes substrate" in detail
        assert "GET /probe/no-socket" in detail
        assert "Internal Server Error" not in resp.text

    def test_a_daemon_side_error_is_answered_too(self, probe, on_kubernetes):
        """APIError descends from DockerException, so one handler covers the tree."""
        resp = probe.get("/probe/daemon-said-no")
        assert resp.status_code == 501
        assert "Kubernetes substrate" in resp.json()["detail"]

    def test_a_client_built_in_a_dependency_is_answered_too(self, probe, on_kubernetes):
        """Several routes take the client as a Depends, so the failure precedes the body."""
        resp = probe.get("/probe/from-a-dependency")
        assert resp.status_code == 501
        assert "Kubernetes substrate" in resp.json()["detail"]

    def test_on_docker_it_does_not_claim_the_wrong_substrate(self, probe, on_docker):
        """A Docker host that loses its socket must not be told it is a cluster."""
        resp = probe.get("/probe/no-socket")
        assert resp.status_code == 503
        detail = resp.json()["detail"]
        assert "Kubernetes" not in detail
        assert "Docker daemon" in detail

    def test_the_route_is_logged_so_a_real_docker_host_stays_debuggable(
        self, probe, on_kubernetes, caplog
    ):
        with caplog.at_level("WARNING", logger="proving_ground.main"):
            probe.get("/probe/no-socket")
        assert any("GET /probe/no-socket" in r.getMessage() for r in caplog.records)

    def test_a_daemon_that_does_not_ping_lands_in_the_same_handler(self):
        """It raised a RuntimeError, which no handler saw and the browser got as a 500."""
        service = DockerService.__new__(DockerService)

        class Unresponsive:
            def ping(self):
                raise OSError("connection refused")

        service.client = Unresponsive()
        with pytest.raises(DOCKER_SDK_ERROR):
            service._verify_connection()


class TestARealRouteOnTheRealApp:
    """The probe app proves the handler; this proves a product route reaches it.

    Nothing between a route and the handler is ours -- the dependency solver,
    the router, Starlette's exception middleware -- so the claim that the defect
    is gone has to be made through all of it, against the failure a cluster
    actually produces: `from_env()` raising because there is no socket to open.
    `/admin/docker-status` is one of the routes the audit reproduced returning a
    bare `Internal Server Error`.
    """

    @pytest.fixture
    def host_with_no_docker(self, monkeypatch):
        def refuse(*_args, **_kwargs):
            raise DockerException(
                "Error while fetching server API version: "
                "FileNotFoundError(2, 'No such file or directory')"
            )

        # The service is a module-level singleton, so one an earlier test built
        # would hide the constructor this is here to drive.
        monkeypatch.setattr(docker_service, "_docker_service", None)
        monkeypatch.setattr(docker_service.docker, "from_env", refuse)

    @pytest.fixture
    def api(self, db_session, admin):
        main.app.dependency_overrides[deps.get_current_user] = lambda: admin
        main.app.dependency_overrides[get_db] = lambda: db_session
        yield TestClient(main.app, raise_server_exceptions=False)
        main.app.dependency_overrides.clear()

    def test_a_docker_route_answers_501_and_names_the_substrate(
        self, api, host_with_no_docker, on_kubernetes
    ):
        resp = api.get("/api/v1/admin/docker-status")
        assert resp.status_code == 501
        assert resp.text != "Internal Server Error"
        detail = resp.json()["detail"]
        assert "Kubernetes substrate" in detail
        assert "GET /api/v1/admin/docker-status" in detail

    def test_on_docker_it_reports_the_failure_and_keeps_the_traceback(
        self, api, host_with_no_docker, on_docker, caplog
    ):
        with caplog.at_level("ERROR", logger="proving_ground.main"):
            resp = api.get("/api/v1/admin/docker-status")
        assert resp.status_code == 503
        detail = resp.json()["detail"]
        assert "Kubernetes" not in detail
        assert "Error while fetching server API version" in detail
        # Starlette logged the traceback while nothing handled the exception;
        # intercepting it must not cost a Docker host that record.
        logged = [r for r in caplog.records if r.name == "proving_ground.main"]
        assert logged and all(r.exc_info for r in logged)


class TestComposingARangeOnKubernetes:
    def test_add_network_is_refused_and_writes_nothing(
        self, db_session, admin, running_range, on_kubernetes
    ):
        body = NetworkCreate(range_id=running_range.id, name="lan", subnet="172.16.1.0/24")
        with pytest.raises(HTTPException) as exc:
            networks_api.create_network(body, db_session, admin)
        assert exc.value.status_code == 409
        assert "blueprint" in exc.value.detail
        assert db_session.query(Network).filter(Network.range_id == running_range.id).count() == 0

    def test_add_vm_is_refused_and_writes_nothing(
        self, db_session, admin, running_range, base_image, on_kubernetes
    ):
        # A network row cannot be made through the API any more, so this one is
        # planted directly -- the refusal must hold for a range that already has
        # the rows an install upgraded from the DinD path would carry.
        network = Network(
            range_id=running_range.id,
            name="lan",
            subnet="172.16.1.0/24",
            gateway="172.16.1.1",
        )
        db_session.add(network)
        db_session.commit()
        body = VMCreate(
            range_id=running_range.id,
            hostname="web",
            base_image_id=base_image.id,
            networks=[NetworkInterfaceCreate(network_id=network.id, ip_address="172.16.1.10")],
        )
        with pytest.raises(HTTPException) as exc:
            vms_api.create_vm(body, db_session, admin)
        assert exc.value.status_code == 409
        assert "blueprint" in exc.value.detail
        assert db_session.query(VM).filter(VM.range_id == running_range.id).count() == 0


class TestComposingARangeOnDocker:
    def test_add_network_still_works(self, db_session, admin, running_range, on_docker):
        body = NetworkCreate(range_id=running_range.id, name="lan", subnet="172.16.1.0/24")
        created = networks_api.create_network(body, db_session, admin)
        assert created.subnet == "172.16.1.0/24"
        assert db_session.query(Network).filter(Network.range_id == running_range.id).count() == 1

    def test_add_vm_still_works(self, db_session, admin, running_range, base_image, on_docker):
        network = networks_api.create_network(
            NetworkCreate(range_id=running_range.id, name="lan", subnet="172.16.1.0/24"),
            db_session,
            admin,
        )
        body = VMCreate(
            range_id=running_range.id,
            hostname="web",
            base_image_id=base_image.id,
            networks=[NetworkInterfaceCreate(network_id=network.id, ip_address="172.16.1.10")],
        )
        created = vms_api.create_vm(body, db_session, admin)
        assert created["hostname"] == "web"
        assert db_session.query(VM).filter(VM.range_id == running_range.id).count() == 1
