"""Read and write the platform's own secrets.

Encryption reuses export_service's arrangement -- Fernet with a key derived
from the JWT secret -- so there is one way secrets are stored at rest rather
than two.
"""

import base64
import hashlib
import logging
from typing import Optional, Tuple

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.orm import Session

from proving_ground.config import get_settings
from proving_ground.models.platform_secret import PlatformSecret

logger = logging.getLogger(__name__)


def _key() -> bytes:
    """Fernet key derived from the JWT secret.

    Same derivation as export_service._get_encryption_key. It follows that
    rotating the JWT secret makes stored secrets unreadable -- they have to be
    set again, which is why reads fail soft rather than raising.
    """
    digest = hashlib.sha256(get_settings().jwt_secret_key.encode()).digest()
    return base64.urlsafe_b64encode(digest)


def set_secret(
    db: Session, key: str, value: str, public_part: Optional[str] = None, user_id=None
) -> PlatformSecret:
    """Store or replace a secret. The plaintext is never logged."""
    token = Fernet(_key()).encrypt(value.encode()).decode()
    row = db.query(PlatformSecret).filter(PlatformSecret.key == key).first()
    if row is None:
        row = PlatformSecret(key=key)
        db.add(row)
    row.value_encrypted = token
    row.public_part = public_part
    row.updated_by_id = user_id
    db.commit()
    db.refresh(row)
    logger.info(f"Platform secret {key!r} set")
    return row


def get_secret(db: Session, key: str) -> Tuple[Optional[str], Optional[PlatformSecret]]:
    """The decrypted value and its row, or (None, row) if it cannot be read.

    Returns rather than raises on a decryption failure: the usual cause is a
    rotated JWT secret, and the caller's useful response is to ask for the
    secret again, not to return a 500.
    """
    row = db.query(PlatformSecret).filter(PlatformSecret.key == key).first()
    if row is None:
        return None, None
    try:
        return Fernet(_key()).decrypt(row.value_encrypted.encode()).decode(), row
    except (InvalidToken, ValueError):
        logger.warning(
            f"Platform secret {key!r} could not be decrypted -- the JWT secret has "
            f"probably changed since it was stored. It needs setting again."
        )
        return None, row


def delete_secret(db: Session, key: str) -> bool:
    row = db.query(PlatformSecret).filter(PlatformSecret.key == key).first()
    if row is None:
        return False
    db.delete(row)
    db.commit()
    logger.info(f"Platform secret {key!r} deleted")
    return True
