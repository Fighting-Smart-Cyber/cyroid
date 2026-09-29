"""Automated checks behind the signed ASD STIG attestations.

A `proposed` attestation records that somebody read the code once. A signed one asserts that the
requirement IS met, and an assertion nobody re-checks decays: add a SOAP endpoint, swap bcrypt for
something else, render a password field as text, and the statement in compliance/attestations.yml
becomes false with nothing failing and nobody told. That is the same failure mode as a scanner whose
output nobody reads.

So every signed entry has a check here, named after its requirement, and
test_every_signed_attestation_has_an_automated_check refuses to let a signed entry exist without one.
The human act is still a human act - reviewing and merging this file and the statuses that go with it
- but what it commits to is "this check correctly represents the requirement", which is a smaller and
far more durable claim than "I read the whole codebase on the 28th".
"""

import pathlib
import re

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
ATTESTATIONS = REPO_ROOT / "compliance" / "attestations.yml"
BACKEND_SRC = REPO_ROOT / "backend" / "proving_ground"
FRONTEND_SRC = REPO_ROOT / "frontend" / "src"
REQUIREMENTS = REPO_ROOT / "backend" / "requirements.txt"

needs_repo = pytest.mark.skipif(
    not ATTESTATIONS.exists(), reason="repo root not present (attestations.yml missing)"
)


def _py_sources():
    return [p for p in BACKEND_SRC.rglob("*.py")]


def _grep_backend(pattern):
    """Files whose source matches `pattern`, case-insensitively."""
    rx = re.compile(pattern, re.IGNORECASE)
    return [
        str(p.relative_to(REPO_ROOT))
        for p in _py_sources()
        if rx.search(p.read_text(errors="ignore"))
    ]


# ── APSC-DV-000190 / -000200 / -000230 / -000240: no SOAP, no WS-Security, no SAML ────────────────
#
# All four are conditional on those technologies being present. The determination is an absence, so
# the check is an absence: no library that speaks them, and no code that handles their elements.

SOAP_SAML_LIBS = ("zeep", "suds", "spyne", "pysaml", "python3-saml", "xmlsec", "onelogin")
SOAP_SAML_CODE = (
    r"\bwsse\b|ws-security|wssecurity|SubjectConfirmation|NotOnOrAfter|samlp:|\bSAMLResponse\b"
)


def _assert_no_soap_or_saml():
    reqs = REQUIREMENTS.read_text().lower()
    present = [lib for lib in SOAP_SAML_LIBS if lib in reqs]
    assert (
        not present
    ), f"requirements.txt now pulls in {present}; the WS-Security/SAML requirements are back in scope"
    hits = _grep_backend(SOAP_SAML_CODE)
    assert (
        not hits
    ), f"WS-Security/SAML handling appeared in {hits}; four attestations are now false"


@needs_repo
def test_APSC_DV_000190_no_ws_security_timestamps_to_protect():
    _assert_no_soap_or_saml()


@needs_repo
def test_APSC_DV_000200_no_ws_security_validity_periods_to_verify():
    _assert_no_soap_or_saml()


@needs_repo
def test_APSC_DV_000230_no_saml_subject_confirmation_to_constrain():
    _assert_no_soap_or_saml()


@needs_repo
def test_APSC_DV_000240_no_saml_conditions_to_enforce():
    _assert_no_soap_or_saml()


# ── APSC-DV-001810 / -001820: no PKI-based authentication path ───────────────────────────────────
#
# Both are conditional on PKI authentication. If a client-certificate or CAC path is ever added these
# stop being not-applicable immediately, and so does the whole password family for any non-certificate
# exemption route - which is the trap worth failing a build over.

PKI_AUTH_CODE = r"ssl_client_verify|SSL_CLIENT_CERT|client_cert_auth|verify_client_cert|x509.*authenticate|mutual.?tls|\bmtls\b"


def _assert_no_pki_auth():
    hits = _grep_backend(PKI_AUTH_CODE)
    assert not hits, (
        f"client-certificate authentication appeared in {hits}. APSC-DV-001810/-001820 are no longer "
        "not-applicable, and the password family applies to any non-certificate exemption path."
    )


@needs_repo
def test_APSC_DV_001810_no_certification_path_to_validate():
    _assert_no_pki_auth()


@needs_repo
def test_APSC_DV_001820_no_certificate_subject_to_map():
    _assert_no_pki_auth()


# ── APSC-DV-001740: passwords stored only as a cryptographic representation ──────────────────────


@needs_repo
def test_APSC_DV_001740_passwords_are_stored_only_as_a_bcrypt_hash():
    from proving_ground.utils.security import get_password_hash, pwd_context, verify_password

    assert pwd_context.schemes() == (
        "bcrypt",
    ), f"password hashing schemes are {pwd_context.schemes()}; the attestation says bcrypt only"
    hashed = get_password_hash("a-password-long-enough")
    assert hashed.startswith("$2"), f"hash is not a bcrypt hash: {hashed[:8]!r}"
    assert "a-password-long-enough" not in hashed
    assert verify_password("a-password-long-enough", hashed)
    assert not verify_password("something-else-entirely", hashed)


# ── APSC-DV-001850: passwords and PINs are never displayed as clear text ─────────────────────────


@needs_repo
def test_APSC_DV_001850_credential_inputs_are_masked():
    masked = 0
    offenders = []
    for path in FRONTEND_SRC.rglob("*.tsx"):
        body = path.read_text(errors="ignore")
        masked += body.count('type="password"')
        # A credential field UNCONDITIONALLY rendered as text is the failure this requirement names,
        # and it is what this catches: a literal type="text" on a tag that mentions a credential.
        # A masked-by-default field with an explicit reveal - type={shown ? 'text' : 'password'} -
        # deliberately passes. That is the accepted implementation of "not displayed as clear text"
        # and the admin create-user form uses it, because an admin has to read a generated password
        # to hand it over. An assessor is free to disagree with that reading; it is stated in the
        # attestation rather than hidden in a regex.
        for match in re.finditer(r"<input[^>]*>", body, re.IGNORECASE | re.DOTALL):
            tag = match.group(0)
            if 'type="text"' in tag and re.search(
                r"password|passphrase|\bpin\b|secret", tag, re.IGNORECASE
            ):
                offenders.append(f"{path.relative_to(REPO_ROOT)}: {tag[:90]}")
    assert masked > 0, "no masked password inputs found at all; has the UI moved?"
    assert not offenders, f"credential fields rendered as clear text: {offenders}"


# ── APSC-DV-000460: approved authorisations are enforced for logical access ──────────────────────


@needs_repo
def test_APSC_DV_000460_role_dependencies_exist_and_are_applied():
    from proving_ground.api import deps

    assert callable(getattr(deps, "require_role", None)), "require_role is gone"
    assert callable(getattr(deps, "require_admin", None)), "require_admin is gone"
    users = flag = 0
    for path in (BACKEND_SRC / "api").glob("*.py"):
        body = path.read_text(errors="ignore")
        users += body.count("require_admin") + body.count("require_role")
        flag += body.count("is_admin")
    assert users >= 2, f"the role dependencies are defined but barely used ({users} references)"
    assert flag >= 1, "no is_admin ownership checks remain"


# ── APSC-DV-003280: no shipped default credential reaches a live install ────────────────────────


@needs_repo
def test_APSC_DV_003280_the_shipped_default_secret_cannot_reach_production():
    from proving_ground.config import InsecureDefaultError, Settings, require_production_secrets

    class FakeSettings:
        debug = False
        jwt_secret_key = Settings.JWT_DEFAULT_SECRET

    with pytest.raises(InsecureDefaultError):
        require_production_secrets(FakeSettings())


# ── APSC-DV-001680: minimum 15-character password length ────────────────────────────────────────


@needs_repo
def test_APSC_DV_001680_the_minimum_password_length_is_enforced():
    """Detail lives in test_password_policy.py; this is the attestation's own linkage to it."""
    from proving_ground.utils.security import PASSWORD_MIN_LENGTH, password_policy_error

    assert PASSWORD_MIN_LENGTH == 15
    assert password_policy_error("x" * 14) is not None
    assert password_policy_error("x" * 15) is None


# ── the guard that keeps this file honest ───────────────────────────────────────────────────────


@needs_repo
def test_every_signed_attestation_has_an_automated_check():
    """A closed requirement without a check is a claim nobody re-verifies. Refuse the combination."""
    entries = yaml.safe_load(ATTESTATIONS.read_text())["requirements"]
    # Only the statuses that CLOSE a requirement need a check. `open` admits a gap and asserts
    # nothing, so demanding automated proof of it would be nonsense.
    closing = ("compliant", "not_applicable", "inherited")
    signed = sorted(sid for sid, e in entries.items() if (e or {}).get("status") in closing)
    checked = {
        name.replace("test_APSC_DV_", "APSC-DV-").split("_")[0]
        for name in globals()
        if name.startswith("test_APSC_DV_")
    }
    missing = [sid for sid in signed if sid not in checked]
    assert not missing, (
        "these attestations are signed but nothing here re-checks them, so they will decay into "
        f"false statements without failing: {missing}. Add a check named test_<ID>_... or set the "
        "entry back to status: proposed."
    )
