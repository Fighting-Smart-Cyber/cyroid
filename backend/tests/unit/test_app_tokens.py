"""pg-gateway must be able to CHECK a range-application credential without being able to MINT one.

The gateway terminates requests from the software a range trains against -- "third party at best,
hostile by design in a red-team exercise" (api/kubernetes_apps.py). Holding the platform's
symmetric JWT_SECRET_KEY would let a compromise of that process issue console tickets and
platform access tokens for every range on the install, so the two app credentials are signed
with a private key the API holds and verified with a public key the gateway holds.

Access tokens, console tickets and ws tickets stay symmetric on purpose: the gateway never sees
them, so making them asymmetric would buy nothing and change three more code paths.
"""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from proving_ground.api import kubernetes_apps as apps
from proving_ground.config import get_settings
from proving_ground.utils import app_tokens


@pytest.fixture
def keypair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    public = (
        key.public_key()
        .public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        .decode()
    )
    return private, public


def _configure(monkeypatch, private, public, ticket_secret=""):
    """Give app_tokens its own view of settings.

    Deliberately NOT get_settings.cache_clear(): that lru_cache is process-wide, and modules
    that captured `settings = get_settings()` at import keep the old object while everything
    else gets a new one. Doing it here made test_deploy_uses_pool fail in the suite and pass
    alone -- the same pollution that has bitten this repository before.
    """
    real = get_settings()
    stub = SimpleNamespace(
        app_token_private_key=private,
        app_token_public_key=public,
        app_token_algorithm="RS256",
        app_ticket_secret=ticket_secret,
        jwt_secret_key=real.jwt_secret_key,
    )
    monkeypatch.setattr(app_tokens, "get_settings", lambda: stub)
    app_tokens._public_key_from.cache_clear()


class TestUnconfiguredItStaysSymmetric:
    """A developer install and the whole existing suite must keep working untouched."""

    def test_it_falls_back_to_the_platform_secret(self):
        key, alg = app_tokens.signing_key()
        assert alg == "HS256" and key == get_settings().jwt_secret_key

    def test_a_ticket_still_round_trips(self):
        rng, user = uuid.uuid4(), uuid.uuid4()
        assert apps.verify_app_ticket(apps.create_app_ticket(rng, user), rng) == user


class TestConfiguredTheVerifierCannotMint:
    def test_a_ticket_round_trips_on_the_key_pair(self, monkeypatch, keypair):
        private, public = keypair
        _configure(monkeypatch, private, public)

        rng, user = uuid.uuid4(), uuid.uuid4()
        assert apps.verify_app_ticket(apps.create_app_ticket(rng, user), rng) == user

    def test_the_verifying_key_is_the_public_half(self, monkeypatch, keypair):
        """The point of the exercise: what the gateway holds cannot sign."""
        private, public = keypair
        _configure(monkeypatch, private, public)

        verify_key, alg = app_tokens.verifying_key()
        assert alg == "RS256"
        assert "PUBLIC KEY" in verify_key
        assert "PRIVATE" not in verify_key

        from jose import jwt
        from jose.exceptions import JWSError

        with pytest.raises((JWSError, AttributeError, ValueError, TypeError)):
            jwt.encode({"typ": "app"}, verify_key, algorithm="RS256")

    def test_a_token_signed_with_the_platform_secret_is_refused(self, monkeypatch, keypair):
        """A gateway compromise must not be able to fall back to the symmetric path."""
        private, public = keypair
        forged = __import__("jose").jwt.encode(
            {
                "sub": str(uuid.uuid4()),
                "range": str(uuid.uuid4()),
                "typ": "app",
                "exp": datetime.now(timezone.utc) + timedelta(minutes=5),
            },
            get_settings().jwt_secret_key,
            algorithm="HS256",
        )
        _configure(monkeypatch, private, public)
        assert app_tokens.decode(forged) is None

    def test_a_handoff_code_round_trips_and_is_still_one_use(self, monkeypatch, keypair):
        private, public = keypair
        _configure(monkeypatch, private, public)

        rng, user = uuid.uuid4(), uuid.uuid4()
        code = apps.create_app_code(rng, user)
        assert apps.verify_app_code(code, rng) == user
        assert apps.verify_app_code(code, rng) is None, "a spent code must not work twice"

    def test_another_ranges_ticket_is_refused(self, monkeypatch, keypair):
        private, public = keypair
        _configure(monkeypatch, private, public)

        mine, theirs, user = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
        assert apps.verify_app_ticket(apps.create_app_ticket(mine, user), theirs) is None


class TestTheVerifyingKeyMayBeACertificate:
    """Helm's genSelfSignedCert emits a cert and a key and cannot emit a bare public key."""

    def test_a_certificate_pem_is_accepted(self, monkeypatch, keypair):
        import datetime as dt

        from cryptography import x509
        from cryptography.hazmat.primitives import hashes
        from cryptography.x509.oid import NameOID

        private, _ = keypair
        key = serialization.load_pem_private_key(private.encode(), password=None)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pg-app-tokens")])
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=1))
            .not_valid_after(dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1))
            .sign(key, hashes.SHA256())
        )
        pem = cert.public_bytes(serialization.Encoding.PEM).decode()

        _configure(monkeypatch, private, pem)
        rng, user = uuid.uuid4(), uuid.uuid4()
        assert apps.verify_app_ticket(apps.create_app_ticket(rng, user), rng) == user


class TestTheGatewayCanVerifyTheTicketItMints:
    """0.53.1: it could not, and no range application could be opened at all.

    pg-gateway holds the PUBLIC half of the code's key pair and no private key. The ticket was
    signed with the same pair, so `signing_key()` fell through to the jwt_secret_key/HS256
    fallback while `verifying_key()` returned the public key and RS256. Every ticket the gateway
    issued failed its own check, and the learner got "This application needs a session" on the
    request straight after a successful handoff.

    Nothing caught it because every test here configured BOTH halves on one settings object, so
    the two sides always agreed. These configure the gateway's actual shape: public key, no
    private key.
    """

    def test_a_verify_only_process_round_trips_its_own_ticket(self, monkeypatch, keypair):
        _, public = keypair
        _configure(monkeypatch, "", public, ticket_secret="a-ticket-secret")  # the gateway
        rid, uid = uuid.uuid4(), uuid.uuid4()

        ticket = app_tokens.create_app_ticket(rid, uid)

        assert app_tokens.verify_app_ticket(ticket, rid) == uid

    def test_the_ticket_does_not_travel_on_the_codes_key(self, monkeypatch, keypair):
        """The two credentials are different problems; signing both with the pair is the bug."""
        from jose import jwt

        _, public = keypair
        _configure(monkeypatch, "", public, ticket_secret="a-ticket-secret")

        ticket = app_tokens.create_app_ticket(uuid.uuid4(), uuid.uuid4())
        assert jwt.get_unverified_header(ticket)["alg"] == "HS256"

    def test_the_ticket_secret_is_not_the_platform_secret(self, monkeypatch, keypair):
        """Reusing jwt_secret_key would work and would let the gateway mint an access token."""
        _, public = keypair
        _configure(monkeypatch, "", public, ticket_secret="a-ticket-secret")

        key, alg = app_tokens.ticket_key()
        assert alg == "HS256"
        assert key == "a-ticket-secret" != get_settings().jwt_secret_key

    def test_unset_it_still_falls_back_so_a_dev_install_works(self, monkeypatch, keypair):
        _, public = keypair
        _configure(monkeypatch, "", public)  # no ticket secret

        key, _ = app_tokens.ticket_key()
        assert key == get_settings().jwt_secret_key

    def test_the_api_and_the_gateway_agree_on_a_ticket(self, monkeypatch, keypair):
        """They must: the API still serves the ForwardAuth path for pre-switch-over ranges."""
        private, public = keypair
        rid, uid = uuid.uuid4(), uuid.uuid4()

        _configure(monkeypatch, private, public, ticket_secret="shared")  # the API
        minted_by_api = app_tokens.create_app_ticket(rid, uid)

        _configure(monkeypatch, "", public, ticket_secret="shared")  # the gateway
        assert app_tokens.verify_app_ticket(minted_by_api, rid) == uid

    def test_a_ticket_signed_with_another_secret_is_refused(self, monkeypatch, keypair):
        _, public = keypair
        _configure(monkeypatch, "", public, ticket_secret="one-secret")
        rid, uid = uuid.uuid4(), uuid.uuid4()
        forged = app_tokens.create_app_ticket(rid, uid)

        _configure(monkeypatch, "", public, ticket_secret="another-secret")
        assert app_tokens.verify_app_ticket(forged, rid) is None
