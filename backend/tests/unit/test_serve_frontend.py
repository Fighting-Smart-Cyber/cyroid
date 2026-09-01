"""The public surface serves a built frontend, and both redeploy paths agree.

The internet-facing host was running a Vite dev server: a development tool that
resolves and transforms modules on request. It refused all 27 path-traversal
probes aimed at it, but "the dev server's file access held up this time" is not
the property to depend on, so the frontend is served as a static build behind
nginx instead.

The failure this guards is quieter than a broken build. The overlay chain was
written out twice -- once in scripts/pg-update.sh, once in the update
container's script -- so adding the serve overlay to one leaves the other
putting the dev server back on the public interface at the next update, with
nothing failing and nobody told.
"""

import pathlib
import re

import pytest
import yaml

# scripts/ and docker-compose*.yml sit at the repo root and are not mounted into
# the api container (only backend/ is), so these run on the host and in CI.
REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
COMPOSE_SH = REPO_ROOT / "scripts" / "compose.sh"
SERVE_YML = REPO_ROOT / "docker-compose.serve.yml"
PG_UPDATE = REPO_ROOT / "scripts" / "pg-update.sh"

needs_repo = pytest.mark.skipif(not COMPOSE_SH.exists(), reason="repo root not present")


@needs_repo
class TestTheOverlayChainHasOneDefinition:
    def test_pg_update_does_not_spell_out_its_own_chain(self):
        body = PG_UPDATE.read_text()
        assert "docker-compose.dev.yml" not in body, (
            "pg-update.sh names the overlay files itself, so it will drift from "
            "the update container's copy again."
        )
        assert "compose.sh" in body

    def test_the_update_container_does_not_spell_out_its_own_chain(self):
        """The in-UI update and the shell script must deploy the same stack."""
        from proving_ground.api import admin as admin_api
        import inspect

        src = inspect.getsource(admin_api.start_platform_update)
        assert "docker-compose.dev.yml" not in src, (
            "The update container builds its own overlay chain; an operator "
            "pressing Update in the UI gets a different stack than pg-update.sh."
        )
        assert "compose.sh" in src


@needs_repo
class TestTheChainIsRunnableWhereItRuns:
    def test_it_is_posix_sh_not_bash(self):
        """The update container is docker:27-cli, which has no bash."""
        first = COMPOSE_SH.read_text().splitlines()[0]
        assert first == "#!/bin/sh", f"shebang is {first!r}; docker:27-cli has no bash."

    def test_it_uses_no_bash_arrays(self):
        body = COMPOSE_SH.read_text()
        assert not re.search(r"^\s*\w+=\(", body, re.M), "Arrays are a bashism."

    def test_serving_the_built_frontend_is_the_default(self):
        """A host has to opt out of it, not remember to opt in."""
        body = COMPOSE_SH.read_text()
        assert "PG_SERVE_FRONTEND:-1" in body, (
            "The serve overlay is off unless asked for, so any host that "
            "forgets the variable is back on the dev server."
        )


@needs_repo
class TestTheServeOverlayServesABuild:
    def setup_method(self):
        self.frontend = yaml.safe_load(SERVE_YML.read_text())["services"]["frontend"]

    def test_it_builds_the_production_dockerfile(self):
        assert self.frontend["build"]["dockerfile"] == "Dockerfile.prod", (
            "Dockerfile and Dockerfile.dev both run `npm run dev`; only "
            "Dockerfile.prod builds the assets and serves them from nginx."
        )

    def test_traefik_is_pointed_at_nginx_not_vite(self):
        """A label map merge that missed would route to a port nothing serves."""
        labels = self.frontend["labels"]
        port = "traefik.http.services.frontend.loadbalancer.server.port=80"
        assert port in labels, f"labels are {labels}; Traefik would still dial Vite on 5173."

    def test_the_healthcheck_probes_the_port_nginx_listens_on(self):
        probe = " ".join(self.frontend["healthcheck"]["test"])
        assert ":80/" in probe, (
            f"healthcheck is {probe!r}; the dev overlay's 5173 probe would "
            "leave the container permanently unhealthy."
        )
