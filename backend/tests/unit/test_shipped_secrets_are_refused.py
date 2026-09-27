"""An install that kept a credential this repository ships must not start.

`jwt_secret_key` ships as a known string so `./scripts/dev-setup.sh` works with no configuration.
Everywhere else that string is public: it signs every session token, so holding it forges any
user -- and the `typ` claim that stops a ticket being replayed as a session says nothing at all
about a token that was forged rather than replayed.

`debug` is what distinguishes the laptop from a deployment, which is why the chart sets it and
why these tests pin both halves: the refusal, and the fact that the chart does not leave it at
the code default.
"""

import pathlib
import re

import pytest

from proving_ground.config import InsecureDefaultError, Settings, require_production_secrets

HELPERS = (
    pathlib.Path(__file__).resolve().parents[3]
    / "deploy"
    / "helm"
    / "proving-ground"
    / "templates"
    / "_helpers.tpl"
)


class TestTheShippedSecretIsRefused:
    def test_a_deployment_that_kept_it_does_not_start(self):
        with pytest.raises(InsecureDefaultError) as raised:
            require_production_secrets(Settings(debug=False))
        # The refusal has to say what to do, not only that it refused.
        assert "JWT_SECRET_KEY" in str(raised.value)
        assert "Helm chart" in str(raised.value)

    def test_a_deployment_with_a_real_secret_starts(self):
        require_production_secrets(Settings(debug=False, jwt_secret_key="a-generated-secret"))

    def test_a_laptop_still_starts(self):
        # The whole point of the shipped default: dev-setup.sh configures nothing.
        require_production_secrets(Settings(debug=True))

    def test_the_default_is_named_rather_than_spelled_twice(self):
        # Two copies of the string is how one of them gets changed and the check stops matching.
        assert Settings().jwt_secret_key == Settings.JWT_DEFAULT_SECRET


class TestTheChartDoesNotLeaveDebugOn:
    def test_the_pods_are_told_debug_is_off(self):
        # `debug` defaults to True in code, for the laptop. A deployed install that never sets it
        # would run FastAPI's debug behaviour on a surface facing learners -- and would skip the
        # refusal above, because the refusal is gated on exactly this flag.
        assert HELPERS.exists()
        env = HELPERS.read_text()
        assert re.search(r"-\s*name:\s*DEBUG", env), "_helpers.tpl sets no DEBUG on the pods"
