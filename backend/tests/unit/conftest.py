# backend/tests/unit/conftest.py
"""Conftest for unit tests - minimal fixtures without app imports."""
import pytest
from unittest.mock import MagicMock


# Override parent conftest by not importing the app
# Unit tests should mock all dependencies


@pytest.fixture
def mock_docker_service():
    """Mock Docker service for unit tests."""
    mock_service = MagicMock()
    mock_service.get_range_client_sync.return_value = MagicMock()
    return mock_service


# The chart's default, not the engine's. `capability_chart_repositories` defaults to empty --
# a blueprint may name no chart repository at all until an install says otherwise -- and the
# repository the sample blueprint's capability comes from is what deploy/helm/proving-ground
# ships in that setting. Almost nothing in this suite is about the policy, so the suite runs as
# a chart install does. A test that IS about the policy sets its own value or builds a
# ChartRepositoryPolicy directly; both override this.
# The repository the shipped sample blueprint's capability comes from, and the example host the
# rest of this suite spells its fixtures with.
SAMPLE_CHART_REPOSITORY = "https://stefanprodan.github.io,https://charts.example"


@pytest.fixture(autouse=True)
def _permit_the_sample_chart_repository(monkeypatch):
    from proving_ground.config import get_settings

    monkeypatch.setattr(
        get_settings(), "capability_chart_repositories", SAMPLE_CHART_REPOSITORY, raising=False
    )
