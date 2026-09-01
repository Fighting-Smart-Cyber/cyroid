"""Secrets the platform holds about itself, rather than about a range.

Currently one: the credential the in-UI update uses to fetch from the code
remote. It is stored here rather than in the environment because an operator
with a browser and no shell has no way to edit a .env, and because Compose
loads .env into *every* container -- a token there would be readable from any
service that happens to be compromised, not just the one that needs it.

Values are encrypted at rest with Fernet, keyed from the JWT secret, matching
export_service's existing treatment of stored passwords. That protects a
database dump or a backup; it does not protect against someone who already has
the application's configuration, which is the same bound the rest of the
system's secrets sit inside.
"""

from typing import Optional
from uuid import UUID

from sqlalchemy import ForeignKey, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from proving_ground.models.base import Base, TimestampMixin, UUIDMixin

# The credential the platform update uses to fetch from the code remote.
GIT_CREDENTIAL_KEY = "git_remote_credential"


class PlatformSecret(Base, UUIDMixin, TimestampMixin):
    """One named secret, encrypted at rest.

    A key/value table rather than columns on a settings row: the alternative
    means a migration for every secret the platform ever needs to hold, and a
    row whose shape encodes what those are.
    """

    __tablename__ = "platform_secrets"

    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)

    # Fernet ciphertext. Never returned by the API -- the read endpoint reports
    # only whether a value is set, and when it was last changed.
    value_encrypted: Mapped[str] = mapped_column(Text, nullable=False)

    # A non-secret companion, so a credential can carry its username without a
    # second row. For a token-based git credential this is the account name the
    # remote expects.
    public_part: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    updated_by_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    def __repr__(self) -> str:
        # Deliberately without the value, so a stray log line cannot leak it.
        return f"<PlatformSecret key={self.key!r}>"
