"""Network names use the pg- prefix; env var keys are unchanged."""

import pytest


def test_mgmt_network_default(monkeypatch):
    monkeypatch.delenv("PROVING_GROUND_MGMT_NETWORK", raising=False)
    from proving_ground.config import Settings

    assert Settings().proving_ground_mgmt_network == "pg-mgmt"


def test_ranges_network_default(monkeypatch):
    monkeypatch.delenv("PROVING_GROUND_RANGES_NETWORK", raising=False)
    from proving_ground.config import Settings

    assert Settings().proving_ground_ranges_network == "pg-ranges"


def test_management_network_default(monkeypatch):
    # This asserts the CURRENT default, not intended behaviour: "pg-management"
    # is not a Docker network that exists on this platform (VyOS-router ranges
    # aren't in use), so nothing actually creates it. Renaming it preserved a
    # pre-existing latent bug rather than fixing it -- see the ledger note at
    # .superpowers/sdd/2026-08-26-phase-0-1-pg-rename-implementation/progress.md:43.
    monkeypatch.delenv("MANAGEMENT_NETWORK_NAME", raising=False)
    from proving_ground.config import Settings

    assert Settings().management_network_name == "pg-management"


def test_catalog_identifiers_unchanged():
    """These are catalog-facing and must never be renamed."""
    from proving_ground.config import Settings

    s = Settings()
    assert s.image_namespace == "proving-ground"
    assert s.minio_bucket == "proving-ground-artifacts"


def test_env_var_keys_unchanged(monkeypatch):
    """Keys stay PROVING_GROUND_*; only values change.

    Asserting the field names exist can't fail independently of the tests
    above, so this also proves the env var actually overrides the default --
    not just that a same-named pydantic field happens to exist.
    """
    monkeypatch.setenv("PROVING_GROUND_MGMT_NETWORK", "pg-mgmt-env-override")
    monkeypatch.setenv("PROVING_GROUND_RANGES_NETWORK", "pg-ranges-env-override")
    from proving_ground.config import Settings

    fields = Settings.model_fields
    assert "proving_ground_mgmt_network" in fields
    assert "proving_ground_ranges_network" in fields

    s = Settings()
    assert s.proving_ground_mgmt_network == "pg-mgmt-env-override"
    assert s.proving_ground_ranges_network == "pg-ranges-env-override"
