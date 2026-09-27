"""Where this platform gets randomness, and where it will fetch a URL from.

SEC-096 -- generated credentials. The admin console generated a new account's
password with the browser's ordinary RNG, which is seeded once per context and
recoverable from a handful of its outputs. That half of the finding is in the
frontend and is tested in ``frontend/src/lib/generatedPassword.test.ts``; the
backend half of the finding turned out not to exist -- nothing under
``proving_ground`` generates a password, and the admin reset endpoint only sets
a flag. The guard below keeps it that way: the first module that reaches for
``random`` to make a credential has to argue with a failing test first.

SEC-039 -- server-side request forgery. Every ISO download endpoint takes a URL
out of the request body and fetches it from inside the cluster. Nothing checked
the scheme or the destination, so it was a forgery primitive pointed at the
pod's own network: link-local instance metadata, the Kubernetes API, this
release's own Postgres. These tests assert the refusals by name, because a
refusal an operator cannot read is one they work around.

No network is touched. Addresses are given as literals where possible, and the
resolver and the HTTP client are replaced where they are not.
"""

import ast
from pathlib import Path

import pytest

from proving_ground.api import cache

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = BACKEND_ROOT / "proving_ground"


# --- SEC-096: generated credentials ----------------------------------------


def _modules_importing(module_name: str) -> list:
    """Every module under proving_ground that imports `module_name`."""
    offenders = []
    for path in PACKAGE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                if any(alias.name.split(".")[0] == module_name for alias in node.names):
                    offenders.append(path.relative_to(BACKEND_ROOT))
            elif isinstance(node, ast.ImportFrom):
                if node.module and node.module.split(".")[0] == module_name:
                    offenders.append(path.relative_to(BACKEND_ROOT))
    return sorted(set(offenders))


def test_nothing_in_the_backend_draws_randomness_from_the_predictable_rng():
    """`random` is a simulation tool, not a source of secrets.

    Mersenne Twister is fully recoverable from 624 consecutive outputs and is
    seeded from the clock by default. A password, a token or a one-use code
    made from it is guessable by anyone who can watch a few of them. `secrets`
    is the module for that and it is what a new generator must use.
    """
    offenders = _modules_importing("random")
    assert offenders == [], (
        "These modules import `random`. If any of them generates a credential, a token or a "
        "one-use code, it must use `secrets` instead: " + ", ".join(str(p) for p in offenders)
    )


def test_the_admin_password_reset_issues_no_credential():
    """The reset endpoint flags the account; it does not mint a password.

    This is the half of SEC-096 that did not exist, pinned so that it stays
    that way. A reset that starts generating a password server-side has to
    pick a generator, and this test is where that decision gets noticed.
    """
    source = (PACKAGE_ROOT / "api" / "users.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    handler = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "admin_reset_password"
    )
    body = ast.get_source_segment(source, handler) or ""
    assert "password_reset_required" in body
    assert "get_password_hash" not in body, (
        "admin_reset_password now sets a password. Whatever generates it must come from "
        "`secrets`, and this test should be replaced with one that proves it does."
    )


# --- SEC-039: where an ISO may be fetched from -----------------------------


@pytest.fixture(autouse=True)
def _no_internal_sources(monkeypatch):
    """Default posture: the opt-in is off unless a test turns it on."""
    monkeypatch.delenv(cache.ALLOW_INTERNAL_ISO_SOURCES_ENV, raising=False)
    monkeypatch.delenv(cache.MAX_ISO_BYTES_ENV, raising=False)


def test_link_local_metadata_is_refused_and_named():
    with pytest.raises(cache.UnsafeDownloadURL) as refusal:
        cache.ensure_download_url_allowed("https://169.254.169.254/latest/meta-data/iam/")
    message = str(refusal.value)
    assert "169.254.169.254" in message
    assert "link-local" in message


def test_loopback_is_refused_and_named():
    with pytest.raises(cache.UnsafeDownloadURL) as refusal:
        cache.ensure_download_url_allowed("https://127.0.0.1:8000/api/v1/users")
    assert "loopback" in str(refusal.value)


def test_a_private_address_is_refused_and_says_how_to_permit_it():
    with pytest.raises(cache.UnsafeDownloadURL) as refusal:
        cache.ensure_download_url_allowed("https://10.43.0.1/api/v1/namespaces")
    message = str(refusal.value)
    assert "private address" in message
    # The refusal has to name the setting, or an operator with a real internal mirror has no
    # way forward but to route around the check.
    assert cache.ALLOW_INTERNAL_ISO_SOURCES_ENV in message


def test_the_shared_cgnat_range_is_refused(monkeypatch):
    """100.64.0.0/10 is a cluster network here, and `is_private` does not report it.

    CPython stopped counting the RFC 6598 shared range as private in 3.11.9 and
    3.12.4, so a destination check written against `is_private` alone silently
    stopped covering it. That matters on this product's own targets: a VPC short
    of RFC 1918 space gets a 100.64.0.0/16 secondary CIDR for pod addresses, and
    the Kubernetes API often answers on the service network beside it. The port
    below is the one the finding named.
    """
    with pytest.raises(cache.UnsafeDownloadURL) as refusal:
        cache.ensure_download_url_allowed("https://100.127.255.254:6443/api/v1/namespaces")
    message = str(refusal.value)
    assert "100.64.0.0/10" in message
    # Opt-in-able, not absolute: a disconnected site really can mirror ISOs on a
    # 100.64 address, and a refusal with no way forward gets routed around.
    assert cache.ALLOW_INTERNAL_ISO_SOURCES_ENV in message

    monkeypatch.setenv(cache.ALLOW_INTERNAL_ISO_SOURCES_ENV, "true")
    cache.ensure_download_url_allowed("https://100.64.0.5/isos/windows.iso")


def test_the_classifier_refuses_by_default_rather_than_allowing_by_default():
    """An address none of the named tests recognise must still be refused.

    No address in the current registry reaches the last test -- every range
    that is not globally routable is already link-local, loopback, private,
    reserved or shared. That is the point: this pins the shape of the
    classifier, so that the next range `ipaddress` reclassifies falls out of
    the bottom as a refusal instead of as a permitted destination, which is
    exactly how 100.64.0.0/10 was opened. A stand-in is the only way to reach
    the branch at all.
    """

    class _UnrecognisedAddress:
        version = 6
        is_link_local = False
        is_unspecified = False
        is_multicast = False
        is_reserved = False
        is_loopback = False
        is_private = False
        is_global = False

    assert cache._refusal_for_address(_UnrecognisedAddress()) is not None


def test_a_public_https_url_is_allowed():
    cache.ensure_download_url_allowed("https://93.184.216.34/windows.iso")
    # IPv6 too: the catch-all on is_global must not refuse an ordinary public mirror.
    cache.ensure_download_url_allowed("https://[2606:2800:220:1:248:1893:25c8:1946]/windows.iso")


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/shadow",
        "gopher://10.0.0.1:6379/_FLUSHALL",
        "ftp://mirror.example.com/x.iso",
        "/etc/shadow",
    ],
)
def test_only_http_urls_are_fetched(url):
    with pytest.raises(cache.UnsafeDownloadURL):
        cache.ensure_download_url_allowed(url)


def test_plain_http_is_refused_by_default_and_permitted_by_the_opt_in(monkeypatch):
    with pytest.raises(cache.UnsafeDownloadURL) as refusal:
        cache.ensure_download_url_allowed("http://93.184.216.34/windows.iso")
    assert cache.ALLOW_INTERNAL_ISO_SOURCES_ENV in str(refusal.value)

    monkeypatch.setenv(cache.ALLOW_INTERNAL_ISO_SOURCES_ENV, "true")
    cache.ensure_download_url_allowed("http://93.184.216.34/windows.iso")


def test_the_opt_in_reaches_a_private_mirror_but_never_link_local(monkeypatch):
    monkeypatch.setenv(cache.ALLOW_INTERNAL_ISO_SOURCES_ENV, "true")

    # The case the opt-in exists for: a disconnected install mirroring ISOs on its own network.
    cache.ensure_download_url_allowed("http://10.10.100.50/isos/windows.iso")

    # The case it must never reach, because nothing serves an ISO from there.
    with pytest.raises(cache.UnsafeDownloadURL) as refusal:
        cache.ensure_download_url_allowed("http://169.254.169.254/latest/meta-data/")
    assert "does not cover it" in str(refusal.value)


def test_every_resolved_address_is_checked_not_just_the_first(monkeypatch):
    """One public record and one private record is a bypass if only the first is looked at.

    The socket is handed whichever address the resolver picks, so a name that
    answers with both must be refused outright.
    """

    def fake_getaddrinfo(host, *args, **kwargs):
        return [
            (2, 1, 6, "", ("93.184.216.34", 0)),
            (2, 1, 6, "", ("169.254.169.254", 0)),
        ]

    monkeypatch.setattr(cache.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(cache.UnsafeDownloadURL) as refusal:
        cache.ensure_download_url_allowed("https://mirror.example.com/windows.iso")
    assert "169.254.169.254" in str(refusal.value)


def test_a_name_that_does_not_resolve_is_refused_with_an_instruction(monkeypatch):
    import socket as socket_module

    def fake_getaddrinfo(host, *args, **kwargs):
        raise socket_module.gaierror(-2, "Name or service not known")

    monkeypatch.setattr(cache.socket, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(cache.UnsafeDownloadURL) as refusal:
        cache.ensure_download_url_allowed("https://nowhere.invalid/windows.iso")
    assert "Upload the ISO" in str(refusal.value) or "upload the ISO" in str(refusal.value)


# --- SEC-039: the fetch itself ---------------------------------------------


class _FakeResponse:
    def __init__(self, status_code=200, headers=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.closed = False

    def close(self):
        self.closed = True

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError("test did not expect an error status")


def test_a_redirect_into_link_local_is_refused(monkeypatch):
    """A permitted host answering 302 is how the destination check gets walked past.

    requests follows redirects itself, so the check that ran on the URL the
    admin supplied would never see the address the connection ends up at.
    """
    hops = []

    def fake_get(url, **kwargs):
        hops.append(url)
        assert (
            kwargs["allow_redirects"] is False
        ), "redirects must be followed here, not by requests"
        return _FakeResponse(302, {"location": "https://169.254.169.254/latest/meta-data/"})

    monkeypatch.setattr("requests.get", fake_get)
    with pytest.raises(cache.UnsafeDownloadURL) as refusal:
        cache.open_validated_download("https://93.184.216.34/windows.iso", timeout=5)
    assert "169.254.169.254" in str(refusal.value)
    assert hops == ["https://93.184.216.34/windows.iso"]


def test_a_redirect_to_a_permitted_host_is_followed(monkeypatch):
    responses = [
        _FakeResponse(302, {"location": "https://93.184.216.35/real.iso"}),
        _FakeResponse(200, {"content-length": "1024"}),
    ]

    def fake_get(url, **kwargs):
        return responses.pop(0)

    monkeypatch.setattr("requests.get", fake_get)
    result = cache.open_validated_download("https://93.184.216.34/windows.iso", timeout=5)
    assert result.status_code == 200


def test_a_redirect_loop_ends_in_a_refusal(monkeypatch):
    def fake_get(url, **kwargs):
        return _FakeResponse(302, {"location": "https://93.184.216.34/windows.iso"})

    monkeypatch.setattr("requests.get", fake_get)
    with pytest.raises(cache.UnsafeDownloadURL) as refusal:
        cache.open_validated_download("https://93.184.216.34/windows.iso", timeout=5)
    assert "redirected more than" in str(refusal.value)


def test_a_declared_length_over_the_cap_is_refused_before_a_byte_is_written(monkeypatch):
    monkeypatch.setenv(cache.MAX_ISO_BYTES_ENV, "1024")
    response = _FakeResponse(200, {"content-length": "999999999"})
    monkeypatch.setattr("requests.get", lambda url, **kwargs: response)

    with pytest.raises(cache.UnsafeDownloadURL) as refusal:
        cache.open_validated_download("https://93.184.216.34/windows.iso", timeout=5)
    assert cache.MAX_ISO_BYTES_ENV in str(refusal.value)
    assert response.closed, "the connection must be dropped, not left streaming"


def test_the_size_cap_falls_back_rather_than_failing_closed_on_junk(monkeypatch):
    """An unparseable cap must not stop every download on the host."""
    monkeypatch.setenv(cache.MAX_ISO_BYTES_ENV, "lots")
    assert cache.max_iso_bytes() == cache.DEFAULT_MAX_ISO_BYTES

    monkeypatch.setenv(cache.MAX_ISO_BYTES_ENV, "-1")
    assert cache.max_iso_bytes() == cache.DEFAULT_MAX_ISO_BYTES

    monkeypatch.setenv(cache.MAX_ISO_BYTES_ENV, "4096")
    assert cache.max_iso_bytes() == 4096


def test_the_default_cap_clears_the_largest_image_the_catalogues_name():
    """macOS installers run to roughly 16 GB, so a cap below that breaks a real download."""
    assert cache.DEFAULT_MAX_ISO_BYTES >= 24 * 1024 * 1024 * 1024
