# proving_ground/services/storage_service.py
"""MinIO storage service for artifact management."""
import hashlib
import io
import logging
from typing import Optional, BinaryIO
from minio.error import S3Error

from proving_ground.services.object_store import artifact_bucket, ensure_bucket, object_client

logger = logging.getLogger(__name__)


class StorageService:
    """Service for managing file storage with MinIO."""

    def __init__(self):
        self.client = object_client()
        self.bucket = artifact_bucket()
        # Was: make_bucket, and re-raise on failure. That meant an identity scoped to a
        # pre-provisioned bucket could not construct the service at all, surfacing as a 500 on
        # the first artifact request rather than as a configuration problem at install. Called
        # for its effect -- create the bucket when that is permitted, log and carry on when it
        # is not; put_object already reports what happens if it really is missing.
        ensure_bucket(self.client, self.bucket)

    def upload_file(
        self,
        file_data: BinaryIO,
        object_name: str,
        content_type: str = "application/octet-stream",
    ) -> tuple[str, int]:
        """
        Upload a file to storage.

        Returns:
            Tuple of (sha256_hash, file_size)
        """
        # Read file data and calculate hash
        file_bytes = file_data.read()
        sha256_hash = hashlib.sha256(file_bytes).hexdigest()
        file_size = len(file_bytes)

        # Upload to MinIO
        try:
            self.client.put_object(
                self.bucket,
                object_name,
                io.BytesIO(file_bytes),
                length=file_size,
                content_type=content_type,
            )
            logger.info(f"Uploaded file: {object_name} ({file_size} bytes)")
            return sha256_hash, file_size
        except S3Error as e:
            logger.error(f"Failed to upload file: {e}")
            raise

    def download_file(self, object_name: str) -> Optional[bytes]:
        """Download a file from storage."""
        try:
            response = self.client.get_object(self.bucket, object_name)
            data = response.read()
            response.close()
            response.release_conn()
            return data
        except S3Error as e:
            if e.code == "NoSuchKey":
                return None
            logger.error(f"Failed to download file: {e}")
            raise

    def delete_file(self, object_name: str) -> bool:
        """Delete a file from storage."""
        try:
            self.client.remove_object(self.bucket, object_name)
            logger.info(f"Deleted file: {object_name}")
            return True
        except S3Error as e:
            logger.error(f"Failed to delete file: {e}")
            return False

    def get_presigned_url(
        self,
        object_name: str,
        expires_seconds: int = 3600,
    ) -> str:
        """Get a presigned URL for downloading a file."""
        from datetime import timedelta

        try:
            url = self.client.presigned_get_object(
                self.bucket,
                object_name,
                expires=timedelta(seconds=expires_seconds),
            )
            return url
        except S3Error as e:
            logger.error(f"Failed to generate presigned URL: {e}")
            raise

    def file_exists(self, object_name: str) -> bool:
        """Check if a file exists in storage."""
        try:
            self.client.stat_object(self.bucket, object_name)
            return True
        except S3Error:
            return False

    def list_files(self, prefix: str = "") -> list[str]:
        """List files with optional prefix."""
        try:
            objects = self.client.list_objects(self.bucket, prefix=prefix)
            return [obj.object_name for obj in objects]
        except S3Error as e:
            logger.error(f"Failed to list files: {e}")
            return []


# Singleton instance
_storage_service: Optional[StorageService] = None


def get_storage_service() -> StorageService:
    """Get the storage service singleton."""
    global _storage_service
    if _storage_service is None:
        _storage_service = StorageService()
    return _storage_service
