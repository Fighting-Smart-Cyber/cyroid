"""One factory builds the object-storage client, and a missing bucket is not a crash.

Six places constructed their own `Minio(...)` from the same four settings. That is six places
to forget when the object store stops being the bundled MinIO -- which ADR-0012 says it will,
and which the Azure decision makes concrete, because Azure Blob has no S3 API and a Blob
implementation has to go behind ONE seam rather than six.
"""

import importlib

import pytest
from minio.error import S3Error

from proving_ground.config import get_settings
from proving_ground.services import object_store


class _Client:
    """A stand-in that records what was asked of it."""

    def __init__(self, exists=True, exists_raises=None, make_raises=None):
        self._exists = exists
        self._exists_raises = exists_raises
        self._make_raises = make_raises
        self.made = []

    def bucket_exists(self, name):
        if self._exists_raises:
            raise self._exists_raises
        return self._exists

    def make_bucket(self, name):
        if self._make_raises:
            raise self._make_raises
        self.made.append(name)


def s3error(code="AccessDenied"):
    return S3Error(code, "refused", "resource", "req", "host", "response")


@pytest.fixture(autouse=True)
def _clear_settings_cache():
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


class TestTheClientIsBuiltInOnePlace:
    def test_no_module_constructs_its_own(self):
        """The guard against the six coming back."""
        import pathlib

        root = pathlib.Path(object_store.__file__).resolve().parents[1]
        offenders = [
            p.relative_to(root).as_posix()
            for p in root.rglob("*.py")
            if p.name != "object_store.py" and "Minio(" in p.read_text()
        ]
        assert (
            offenders == []
        ), f"construct the client via object_store.object_client(): {offenders}"

    def test_it_honours_the_tls_setting(self, monkeypatch):
        monkeypatch.setenv("MINIO_SECURE", "true")
        monkeypatch.setenv("MINIO_ENDPOINT", "s3.example.invalid")
        get_settings.cache_clear()
        importlib.reload(object_store)

        client = object_store.object_client()
        assert client._base_url.is_https, "MINIO_SECURE=true must produce an https client"


class TestAMissingBucketIsNotACrash:
    """`StorageService.__init__` called make_bucket and re-raised, so an identity scoped to a
    pre-provisioned bucket -- IRSA, Workload Identity, an IL4 tenant -- could not construct the
    service at all. It surfaced as a 500 on the first request, not as a config error."""

    def test_an_existing_bucket_is_left_alone(self):
        client = _Client(exists=True)
        assert object_store.ensure_bucket(client, "b") is True
        assert client.made == []

    def test_a_missing_bucket_is_created_when_allowed(self, monkeypatch):
        monkeypatch.setenv("MINIO_CREATE_BUCKET", "true")
        get_settings.cache_clear()
        client = _Client(exists=False)
        assert object_store.ensure_bucket(client, "b") is True
        assert client.made == ["b"]

    def test_creation_can_be_refused_without_raising(self, monkeypatch):
        monkeypatch.setenv("MINIO_CREATE_BUCKET", "false")
        get_settings.cache_clear()
        client = _Client(exists=False)
        assert object_store.ensure_bucket(client, "b") is False
        assert client.made == []

    def test_a_refused_create_reports_false_rather_than_raising(self, monkeypatch):
        monkeypatch.setenv("MINIO_CREATE_BUCKET", "true")
        get_settings.cache_clear()
        client = _Client(exists=False, make_raises=s3error())
        assert object_store.ensure_bucket(client, "b") is False

    def test_an_unreachable_store_reports_false_rather_than_raising(self):
        client = _Client(exists_raises=ConnectionError("no route to host"))
        assert object_store.ensure_bucket(client, "b") is False
