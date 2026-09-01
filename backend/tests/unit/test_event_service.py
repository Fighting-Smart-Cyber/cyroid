# backend/tests/unit/test_event_service.py
import pytest
from unittest.mock import MagicMock
from uuid import uuid4
from proving_ground.services.event_service import EventService
from proving_ground.models.event_log import EventType


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.add = MagicMock()
    db.commit = MagicMock()
    db.refresh = MagicMock()
    return db


def test_log_event(mock_db):
    service = EventService(mock_db)
    range_id = uuid4()

    service.log_event(range_id=range_id, event_type=EventType.VM_STARTED, message="Test VM started")

    mock_db.add.assert_called_once()
    mock_db.commit.assert_called_once()
