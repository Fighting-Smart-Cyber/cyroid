"""Parsing capability declarations out of a blueprint's `config` JSON."""

import pytest

from proving_ground.capability.models import Scope
from proving_ground.capability.specs import (
    capabilities_from_blueprint_config,
    capability_from_config,
)

DIGEST = "registry.example/logc2@sha256:" + "d" * 64


def entry(**kw):
    base = {
        "name": "logc2",
        "version": "1.0.0",
        "scope": "per-learner",
        "chart": {"name": "logc2", "version": "1.4.2", "repository": "https://charts.example"},
        "hooks": {"verify": {"image": DIGEST, "command": ["/bin/verify"]}},
    }
    base.update(kw)
    return base


class TestHappyPath:
    def test_a_minimal_capability_parses(self):
        spec = capability_from_config(entry())
        assert spec.name == "logc2"
        assert spec.scope is Scope.PER_LEARNER
        assert spec.seed is None and spec.reset is None

    def test_optional_hooks_are_picked_up(self):
        spec = capability_from_config(
            entry(
                hooks={
                    "verify": {"image": DIGEST, "command": ["/v"]},
                    "seed": {"image": DIGEST, "command": ["/s"], "timeout_seconds": 90},
                    "reset": {"image": DIGEST, "command": ["/r"]},
                }
            )
        )
        assert spec.seed.timeout_seconds == 90
        assert spec.reset.command == ("/r",)

    def test_a_blueprint_declaring_no_capabilities_is_valid(self):
        assert capabilities_from_blueprint_config({}) == []
        assert capabilities_from_blueprint_config(None) == []
        assert capabilities_from_blueprint_config({"networks": []}) == []

    def test_several_capabilities_parse_in_order(self):
        specs = capabilities_from_blueprint_config({"capabilities": [entry(), entry(name="tyros")]})
        assert [s.name for s in specs] == ["logc2", "tyros"]


class TestErrorsNameTheCapabilityAndField:
    def test_a_missing_scope_is_rejected_by_name(self):
        bad = entry()
        del bad["scope"]
        with pytest.raises(ValueError, match="'logc2'.*'scope'"):
            capability_from_config(bad)

    def test_an_unknown_scope_lists_the_allowed_ones(self):
        with pytest.raises(ValueError, match="per-learner, per-cohort, shared"):
            capability_from_config(entry(scope="per-squad"))

    def test_a_missing_verify_hook_is_rejected(self):
        with pytest.raises(ValueError, match="must declare a verify hook"):
            capability_from_config(entry(hooks={}))

    def test_an_unpinned_image_is_rejected(self):
        with pytest.raises(ValueError, match="digest-pinned"):
            capability_from_config(
                entry(hooks={"verify": {"image": "logc2:latest", "command": ["/v"]}})
            )

    def test_a_shell_string_command_is_rejected(self):
        # "sh -c 'rm -rf /'" as one string is a different program than a list of arguments.
        with pytest.raises(ValueError, match="list of arguments, not a shell string"):
            capability_from_config(
                entry(hooks={"verify": {"image": DIGEST, "command": "/bin/verify --all"}})
            )

    def test_the_failing_index_is_named_when_the_name_is_missing(self):
        bad = entry()
        del bad["name"]
        with pytest.raises(ValueError, match=r"capabilities\[1\].*'name'"):
            capabilities_from_blueprint_config({"capabilities": [entry(), bad]})

    def test_capabilities_must_be_a_list(self):
        with pytest.raises(ValueError, match="must be a list"):
            capabilities_from_blueprint_config({"capabilities": {"name": "logc2"}})


class TestWebSurface:
    def test_a_capability_can_name_its_browser_facing_service(self):
        spec = capability_from_config(entry(web={"service": "podinfo", "port": 9898}))
        assert (spec.web.service, spec.web.port, spec.web.path) == ("podinfo", 9898, "/")

    def test_a_capability_without_one_has_none(self):
        assert capability_from_config(entry()).web is None

    def test_a_web_block_missing_its_port_is_refused_by_name(self):
        with pytest.raises(ValueError, match="web.*port"):
            capability_from_config(entry(web={"service": "podinfo"}))
