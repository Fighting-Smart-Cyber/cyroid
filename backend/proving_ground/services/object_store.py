"""One place that builds the object-storage client.

There were six. `storage_service.py`, four in `api/content.py` and one in `api/admin.py` each
constructed a `Minio(...)` from the same four settings, which is six places to forget when the
object store stops being the bundled MinIO -- and ADR-0012 says it will, because "S3-compatible
object store" is something the environment supplies.

Two things this centralises that were wrong in all six:

* **TLS.** The chart never emits `MINIO_SECURE`, so `minio_secure` kept its `False` default and
  the client dialled plain HTTP. Against a bundled MinIO on the pod network that is merely
  unencrypted; against any real endpoint it does not work at all, and an accredited boundary
  will not carry it.
* **Bucket creation.** `StorageService.__init__` called `make_bucket` and re-raised on failure,
  so an identity scoped to a pre-provisioned bucket -- an IRSA role, a Workload Identity, an
  IL4 tenant's bucket -- could not construct the service at all. It surfaced as a 500 on the
  first request rather than as a configuration error at install.

The Azure decision makes this the seam that matters: Azure Blob has no S3 API, so a Blob
implementation goes behind this factory rather than behind six call sites.
"""

import logging
from minio import Minio
from minio.error import S3Error

from proving_ground.config import get_settings

logger = logging.getLogger(__name__)


def object_client() -> Minio:
    """The object-storage client this install is configured for."""
    settings = get_settings()
    return Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )


def artifact_bucket() -> str:
    return get_settings().minio_bucket


def content_bucket() -> str:
    """The bucket content assets live in.

    It was the bare literal "proving-ground-content" in two places with no setting behind it,
    so an environment that pre-provisions its buckets had no way to name them. Renaming it is
    still out of bounds -- CLAUDE.md lists it as a catalog integration point -- but naming a
    DIFFERENT one has to be possible.
    """
    return get_settings().minio_content_bucket


def ensure_bucket(client: Minio, bucket: str) -> bool:
    """Make sure `bucket` is usable, and say whether it is.

    Never raises. A missing bucket we are not permitted to create is a deployment fact, not a
    programming error, and the caller decides what to do about it -- which for a read path is
    usually "return nothing" rather than "500".
    """
    try:
        if client.bucket_exists(bucket):
            return True
    except S3Error as exc:
        logger.warning("object store: could not check bucket %s: %s", bucket, exc)
        return False
    except Exception as exc:  # noqa: BLE001 - a DNS or TLS failure is the same answer here
        logger.warning("object store: %s is unreachable: %s", bucket, exc)
        return False

    if not get_settings().minio_create_bucket:
        logger.warning(
            "object store: bucket %s does not exist and MINIO_CREATE_BUCKET is off, so it was "
            "not created. Pre-provision it, or turn creation on.",
            bucket,
        )
        return False

    try:
        client.make_bucket(bucket)
        logger.info("object store: created bucket %s", bucket)
        return True
    except S3Error as exc:
        logger.warning("object store: could not create bucket %s: %s", bucket, exc)
        return False
