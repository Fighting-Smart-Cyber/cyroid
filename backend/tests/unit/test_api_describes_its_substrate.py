"""The API's own description is the one document a reader consults to find out what is true.

It said "Cyber Range Orchestrator In Docker" and taught a create-range / add-networks / add-VMs
quick start on every install, including the ones where every step of it is refused. Two readers
meet this: a person in Swagger, and an assistant reading `/api/v1/schema/ai-context` in order to
generate calls -- and the second will faithfully generate the refused ones.

These ask the two functions directly rather than reloading the module under a different
environment. Reloading means clearing the `get_settings` lru_cache, and every module that
captured `get_settings()` at import then holds a different object than the one a later test
patches -- which is how this file, in its first form, made `test_deploy_uses_pool` fail when run
after it and pass when run alone.
"""

from proving_ground.main import _ai_overview, _concepts

ERA_A_WORDS = ("In Docker", "Docker-based", "POST /api/v1/vms")


class TestOnKubernetes:
    def test_the_overview_names_no_era_a_concept(self):
        for word in ERA_A_WORDS:
            assert word not in _concepts(True), word

    def test_the_quick_start_is_one_that_works(self):
        quick = _concepts(True).split("## Quick Start")[1]
        assert "/blueprints" in quick
        # The two routes that answer 409 on this substrate.
        assert "POST /api/v1/networks" not in quick
        assert "POST /api/v1/vms" not in quick

    def test_the_assistant_guide_says_which_substrate_this_is(self):
        overview = _ai_overview(True)
        assert "Kubernetes" in overview and "Docker-based" not in overview


class TestOnDocker:
    def test_era_a_keeps_the_text_it_had(self):
        quick = _concepts(False).split("## Quick Start")[1]
        assert "POST /api/v1/networks" in quick and "POST /api/v1/vms" in quick
        assert "Docker-based" in _ai_overview(False)


class TestTheDocumentIsWhole:
    def test_the_shared_half_is_still_appended(self):
        from proving_ground.main import AI_CONTEXT, API_DESCRIPTION

        assert "## Authentication" in API_DESCRIPTION
        assert "## Key Endpoints" in AI_CONTEXT
