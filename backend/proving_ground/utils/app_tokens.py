"""Signing for the two credentials a range application needs, which are not the same problem.

pg-gateway terminates requests from the software a range is training against -- "third party at
best, hostile by design in a red-team exercise", in the words of the module that publishes it.
It is therefore the process most likely to be compromised, and the question is what it holds.

With the platform's symmetric `JWT_SECRET_KEY` it could **mint** a platform access token or a
console ticket for any range on the install: a memory-disclosure or request-smuggling bug there
would not stay a gateway compromise. Access tokens, console tickets and ws tickets stay on that
secret precisely because the gateway never sees it.

**The handoff code is asymmetric.** It crosses an origin boundary: the API mints it with a
private key, the gateway verifies it with the public half, and the gateway cannot forge one for
a range its holder was never entitled to. That is a genuine cross-process credential and the
asymmetry is the point.

**The ticket is not.** The gateway redeems the code, sets the ticket cookie, and checks that
cookie on every later request -- one process, minting and verifying. Asymmetry buys nothing
there, and signing it with the pair above actively broke it: holding only the public half, the
gateway signed with the `jwt_secret_key` fallback and then tried to verify with the public key,
so every ticket it issued failed its own check and no range application could be opened at all
(found on pg-devtest at 0.53.1). It gets its own symmetric secret, shared with the API and with
nothing else -- reusing `jwt_secret_key` would work and would hand the gateway exactly the
capability this module exists to withhold.

Unconfigured, both fall back to the symmetric secret and HS256. That keeps a developer install
and every existing test working unchanged; an install that actually runs a gateway configures
them, and `require_production_secrets` is where "and must" will live once the gateway ships.

The code's verifying key may be a public key PEM or a certificate PEM. The certificate form is
there so the chart can mint the pair with Helm's `genSelfSignedCert`, which produces a cert and a
key but has no way to emit a bare public key.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Optional
from urllib.parse import parse_qsl, urlencode
from uuid import UUID, uuid4

from proving_ground.config import get_settings

logger = logging.getLogger(__name__)

_SYMMETRIC = "HS256"


@lru_cache(maxsize=4)
def _public_key_from(material: str) -> str:
    """A verifying key, accepting either a public key PEM or a certificate PEM."""
    if "BEGIN CERTIFICATE" not in material:
        return material
    from cryptography.hazmat.primitives import serialization
    from cryptography.x509 import load_pem_x509_certificate

    cert = load_pem_x509_certificate(material.encode())
    return (
        cert.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )


def signing_key() -> tuple[str, str]:
    """(key, algorithm) for minting an app credential."""
    settings = get_settings()
    private = (settings.app_token_private_key or "").strip()
    if private:
        return private, settings.app_token_algorithm
    return settings.jwt_secret_key, _SYMMETRIC


def verifying_key() -> tuple[str, str]:
    """(key, algorithm) for checking one.

    Asymmetric verification needs the PUBLIC half; falling back to the private key here would
    work and would also mean a process that only needs to verify holds the ability to sign,
    which is the whole point of this module.
    """
    settings = get_settings()
    public = (settings.app_token_public_key or "").strip()
    if public:
        return _public_key_from(public), settings.app_token_algorithm
    private = (settings.app_token_private_key or "").strip()
    if private:
        # Signing configured without its public half: the API can still verify what it minted.
        return private, settings.app_token_algorithm
    return settings.jwt_secret_key, _SYMMETRIC


def ticket_key() -> tuple[str, str]:
    """(key, algorithm) for the ticket -- symmetric, because one process does both halves.

    Falls back to `jwt_secret_key` when unset, which is a developer install and the test suite.
    An install running a gateway sets `APP_TICKET_SECRET` on both the API and the gateway; the
    chart generates it and keeps it across upgrades, like the key pair.
    """
    settings = get_settings()
    secret = (settings.app_ticket_secret or "").strip()
    return (secret or settings.jwt_secret_key), _SYMMETRIC


def encode_ticket(payload: dict) -> str:
    from jose import jwt

    key, algorithm = ticket_key()
    return jwt.encode(payload, key, algorithm=algorithm)


def decode_ticket(token: str) -> Optional[dict]:
    """The claims, or None if the token is not one we issued. Never raises."""
    from jose import jwt
    from jose.exceptions import JWTError

    key, algorithm = ticket_key()
    try:
        return jwt.decode(token, key, algorithms=[algorithm])
    except JWTError:
        return None
    except Exception as exc:  # noqa: BLE001 - malformed key material must not 500 the request
        logger.warning("app ticket could not be verified: %s", exc)
        return None


def encode(payload: dict) -> str:
    from jose import jwt

    key, algorithm = signing_key()
    return jwt.encode(payload, key, algorithm=algorithm)


def decode(token: str) -> Optional[dict]:
    """The claims, or None if the token is not one we issued. Never raises."""
    from jose import jwt
    from jose.exceptions import JWTError

    key, algorithm = verifying_key()
    try:
        return jwt.decode(token, key, algorithms=[algorithm])
    except JWTError:
        return None
    except Exception as exc:  # noqa: BLE001 - malformed key material must not 500 the request
        logger.warning("app token could not be verified: %s", exc)
        return None


# --- The two credentials themselves -------------------------------------------------------
#
# These live here, rather than in api/kubernetes_apps.py where they grew up, because pg-gateway
# has to mint nothing and verify both, and importing that module would drag a Kubernetes client,
# the ORM and the platform's own settings into the proxy. `kubernetes_apps` re-exports them, so
# every existing caller and test still says what it said.

APP_COOKIE_NAME = "pg_app_ticket"
APP_CODE_QUERY = "pg_app_code"
APP_TICKET_MINUTES = 30
APP_CODE_SECONDS = 30


def create_app_ticket(range_id: UUID, user_id: UUID, minutes: int = APP_TICKET_MINUTES) -> str:
    return encode_ticket(
        {
            "sub": str(user_id),
            "range": str(range_id),
            "typ": "app",
            "exp": datetime.now(timezone.utc) + timedelta(minutes=minutes),
        }
    )


def verify_app_ticket(token: str, range_id: UUID) -> Optional[UUID]:
    """None on anything wrong -- bad signature, expired, another range, not an app ticket."""
    payload = decode_ticket(token)
    if payload is None or payload.get("typ") != "app" or payload.get("range") != str(range_id):
        return None
    try:
        return UUID(payload["sub"])
    except (KeyError, ValueError):
        return None


def create_app_code(range_id: UUID, user_id: UUID, seconds: int = APP_CODE_SECONDS) -> str:
    """The one-time handoff that carries an entitlement across the origin boundary.

    Thirty seconds and one use, because a query string is written to every access log it passes
    through and stays in the browser's history: it has to be worthless by the time anyone reads
    it back. It is not the session -- it is exchanged for the ticket cookie on the first
    request, which then redirects to the same URL without it, so the long-lived credential is
    never in a URL at all.
    """
    return encode(
        {
            "sub": str(user_id),
            "range": str(range_id),
            "typ": "app-code",
            "jti": uuid4().hex,
            "exp": datetime.now(timezone.utc) + timedelta(seconds=seconds),
        }
    )


# Codes already spent, by `jti`, each kept only until it would have expired anyway. One process,
# one set: with more than one replica a code can be spent once per pod, so single use is what
# closes the ordinary case and the thirty-second life is what bounds the rest. Moving this to
# Redis is a decided next step, and is why it is a function rather than an inline dict lookup.
_redeemed: dict[str, float] = {}


def _redeem(jti: str, expires_at: float) -> bool:
    """False if this code has been spent already."""
    now = time.time()
    for spent, until in list(_redeemed.items()):
        if until <= now:
            del _redeemed[spent]
    if jti in _redeemed:
        return False
    _redeemed[jti] = expires_at
    return True


def verify_app_code(token: str, range_id: UUID) -> Optional[UUID]:
    """The user the code was issued to, once. None on anything wrong, including a second use."""
    payload = decode(token)
    if payload is None or payload.get("typ") != "app-code":
        return None
    if payload.get("range") != str(range_id):
        return None
    jti, expires_at = payload.get("jti"), payload.get("exp")
    if not jti or not expires_at or not _redeem(str(jti), float(expires_at)):
        return None
    try:
        return UUID(payload["sub"])
    except (KeyError, ValueError):
        return None


def code_in(uri: str) -> str:
    """The handoff code carried in a request URI, or empty."""
    _, _, query = uri.partition("?")
    for key, value in parse_qsl(query, keep_blank_values=True):
        if key == APP_CODE_QUERY:
            return value
    return ""


def uri_without_code(uri: str) -> str:
    """The same URI with the handoff code removed -- where the exchange redirects to."""
    path, _, query = uri.partition("?")
    kept = [(k, v) for k, v in parse_qsl(query, keep_blank_values=True) if k != APP_CODE_QUERY]
    return f"{path}?{urlencode(kept)}" if kept else path
