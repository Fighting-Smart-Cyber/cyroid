"""The platform update, in-cluster -- the Era B branch of the three update endpoints.

On the Compose path (ADR-0014) an update is a sidecar container running `git` and
`compose.sh` against the checkout the API was started from. A pod has neither the checkout nor
a Docker socket, and "Could not check for updates" is what that looked like on pg-preprod.

In the cluster the release is a Flux `HelmRelease` -- `scripts/install-k8s.sh` creates it, and
the substrate's helm-controller reconciles it -- so:

    check   = the chart versions the registry holds, read with the image pull secret
    update  = the HelmRelease's `spec.chart.spec.version` set to the newest release
    status  = the HelmRelease's Ready condition against the version it was asked for

Same properties as the Compose path, kept on purpose: the target is resolved server-side from
what the registry publishes and re-validated as X.Y.Z; the request carries no ref. What runs
the upgrade is the controller that already holds the credentials to do so, not a subprocess in
the API.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException, status
from pydantic import BaseModel

from proving_ground.capability.flux import HELM_GROUP, HELM_PLURAL, HELM_VERSION
from proving_ground.capability.kubernetes_client import KubernetesApiClient
from proving_ground.config import get_settings
from proving_ground.utils.versions import is_newer, latest_release, parse_version

logger = logging.getLogger(__name__)

__all__ = ["authorized_realm", "check", "credential", "is_kubernetes", "start", "status_of"]


def is_kubernetes() -> bool:
    return get_settings().range_substrate == "kubernetes"


class Registry(BaseModel):
    """Where the chart lives and what pulls it, both from the pod's environment."""

    repository: str  # registry.example/group/project/charts/proving-ground
    username: str
    password: str

    @property
    def host(self) -> str:
        return self.repository.split("/", 1)[0]

    @property
    def path(self) -> str:
        return self.repository.split("/", 1)[1]

    @classmethod
    def from_environment(cls) -> "Registry":
        settings = get_settings()
        if not settings.chart_repository:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="CHART_REPOSITORY is not set; this install cannot look for updates.",
            )
        if "/" not in settings.chart_repository:
            # `host` and `path` split this value on the first slash. Without one, `path` raises
            # IndexError somewhere further in and the admin is told the registry is unreachable,
            # which sends them to the network rather than to the setting that is wrong.
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=(
                    f"CHART_REPOSITORY is {settings.chart_repository!r}, which names a host and "
                    "no repository beneath it. It should look like "
                    "registry.example/group/project/charts/proving-ground."
                ),
            )
        try:
            with open(settings.registry_credentials_file, encoding="utf-8") as f:
                auths = json.load(f).get("auths") or {}
        except (OSError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"The image pull secret is not mounted where expected: {exc}",
            ) from exc
        host = settings.chart_repository.split("/", 1)[0]
        entry = auths.get(host) or next(iter(auths.values()), None)
        if not entry:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"The pull secret has no credentials for {host}.",
            )
        username, password = entry.get("username"), entry.get("password")
        if not (username and password) and entry.get("auth"):
            username, _, password = base64.b64decode(entry["auth"]).decode().partition(":")
        return cls(repository=settings.chart_repository, username=username, password=password)


_CHALLENGE = re.compile(r'(\w+)="([^"]*)"')

# Where a token request may be sent, when the registry's answer names somewhere other than the
# registry itself. Comma-separated hostnames. Read from the environment directly rather than
# through Settings because the setting only exists to be an escape hatch for a refusal, and a
# refusal an operator cannot lift without a code change is worse than the thing it prevents.
REALM_HOSTS_ENV = "UPDATE_REALM_HOSTS"


def _hostname(authority: str) -> str:
    """The host out of a `host`, `host:port` or `[v6]:port` authority, lowercased.

    An address literal is bracketed in an authority and unbracketed once a URL is parsed, so
    the realm's host arrives already unbracketed and the registry's does not. Splitting
    `[::1]:5000` on the first colon makes the expected host `[`, which no realm can equal --
    an install reached over IPv6 could then never check for updates, and would be told its
    registry is named `[`.
    """
    if authority.startswith("[") and "]" in authority:
        return authority[1 : authority.index("]")].lower()
    host, _, _port = authority.partition(":")
    return host.lower()


def _parent_domain(host: str) -> str:
    """The domain `host` sits beneath, or "" when it sits directly beneath a suffix.

    Two labels are treated as having no parent: the parent of `ghcr.io` would be `io`, and
    trusting that would trust every host on the internet that ends in it. An address has no
    parent either -- the "domain" of 10.0.0.1 would be 0.0.1, which 1.0.0.1 ends with, and an
    IPv4-mapped IPv6 address carries the same dotted tail (::ffff:10.0.0.1), which is why the
    test is what the address parser accepts rather than what looks like digits.
    """
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return ""
    labels = host.split(".")
    if len(labels) < 3 or all(label.isdigit() for label in labels):
        return ""
    return ".".join(labels[1:])


def _configured_realm_hosts() -> list[str]:
    return [h.strip().lower() for h in os.environ.get(REALM_HOSTS_ENV, "").split(",") if h.strip()]


def authorized_realm(realm: str, registry_host: str) -> str:
    """The realm URL a token may be requested from, or a refusal naming what was expected.

    A registry answers an unauthenticated request with `WWW-Authenticate: Bearer realm="<url>"`
    and the client fetches a token from that URL with its credentials attached. The URL is
    chosen by the server being talked to, so a registry that is impersonated, compromised or
    simply misconfigured names any host it likes and is handed the deploy token that pulls
    every image this install runs. Nothing about that is visible to the operator: the update
    check succeeds, and the credential is gone.

    So the realm is pinned before anything is sent. The registry's own host is the ordinary
    case. The exception that has to keep working is the split deployment this platform is
    released from, where the container registry and the service that issues its tokens are
    sibling names under one domain -- registry.<domain> answering with a realm on
    code.<domain>. A realm on the registry's parent domain is therefore allowed too, which
    keeps the token inside the domain that already holds the registry instead of letting it
    leave for one an attacker picked. A registry published directly beneath a multi-label
    public suffix -- a pages-style host -- would have unrelated siblings allowed by that rule;
    no container registry is served from one, and UPDATE_REALM_HOSTS is how to be exact.
    """
    expected = _hostname(registry_host)
    try:
        parsed = urlsplit(realm)
        host = (parsed.hostname or "").lower()
        scheme = parsed.scheme
    except ValueError as exc:
        # urlsplit raises rather than answers on a bracketed authority that is not an address
        # -- `https://[registry.example]/token` is one. Uncaught that leaves the pin as the
        # only thing in this module that can fail without naming itself: a 500 on `start`, and
        # on `check` the registry's own refusal dressed up as "could not reach the chart
        # registry", which sends the operator to the network instead of to the registry.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"The chart registry {expected} asked for its auth token from {realm!r}, which "
                f"does not parse as a URL ({exc}). The registry pull credentials are not sent "
                f"there. Expected a realm on {expected}."
            ),
        ) from exc

    if scheme != "https":
        # The credentials go out as Basic auth, which is the token in reversible encoding. A
        # realm that is not https -- or is not a URL at all, which is what a Basic challenge
        # looks like through this parser -- never gets to carry them.
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"The chart registry {expected} asked for its auth token from {realm!r}, which "
                f"is not an https URL. The registry pull credentials are not sent there. "
                f"Expected a realm on {expected}."
            ),
        )

    parent = _parent_domain(expected)
    if host == expected or host in _configured_realm_hosts():
        return realm
    if parent and (host == parent or host.endswith(f".{parent}")):
        return realm

    raise HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail=(
            f"The chart registry {expected} asked for its auth token from {host}, which is not "
            f"{expected}"
            + (f" and is not beneath {parent}" if parent else "")
            + ". The registry pull credentials are not sent there. If that host really is this "
            f"registry's token service, name it in {REALM_HOSTS_ENV}."
        ),
    )


def list_chart_versions(
    registry: Registry,
    timeout: float = 15.0,
    transport: Optional[httpx.BaseTransport] = None,
) -> list[str]:
    """Tags of the chart's OCI repository -- the versions that were actually published.

    Token auth as the distribution spec has it: the registry's 401 names the realm and service,
    a token is fetched there with the pull secret's credentials, and the tag list is read with
    it. The realm is pinned first -- see `authorized_realm`.

    `transport` exists so the refusal can be proven without a registry to talk to; nothing in
    the platform passes it.
    """
    # Redirects are not followed, explicitly rather than by relying on the default. A realm that
    # passes the pin and then answers 302 would otherwise re-aim the credentialled request at a
    # host the pin never saw.
    with httpx.Client(timeout=timeout, follow_redirects=False, transport=transport) as http:
        probe = http.get(f"https://{registry.host}/v2/")
        challenge = dict(_CHALLENGE.findall(probe.headers.get("www-authenticate", "")))
        headers = {}
        if probe.status_code == 401 and "realm" in challenge:
            token = http.get(
                authorized_realm(challenge["realm"], registry.host),
                params={
                    "service": challenge.get("service", ""),
                    "scope": f"repository:{registry.path}:pull",
                },
                auth=(registry.username, registry.password),
            )
            token.raise_for_status()
            headers = {"Authorization": f"Bearer {token.json()['token']}"}
        tags = http.get(f"https://{registry.host}/v2/{registry.path}/tags/list", headers=headers)
        if tags.status_code == 404:
            return []  # nothing published yet is an honest answer, not an error
        tags.raise_for_status()
        return list(tags.json().get("tags") or [])


def _run(coro):
    return asyncio.run(coro)


async def _release(kube: KubernetesApiClient) -> dict | None:
    settings = get_settings()
    return await kube.get_custom_object(
        group=HELM_GROUP,
        version=HELM_VERSION,
        plural=HELM_PLURAL,
        namespace=settings.pod_namespace,
        name=settings.helm_release_name,
    )


def _desired(live: dict) -> dict:
    """The object as something to apply: what a person would have written, not what the API
    server returned. Server-managed fields (`resourceVersion`, `uid`, `managedFields`, `status`)
    make a create fail with "resourceVersion should not be set" -- a 500, not the 409 the apply
    path knows how to follow up."""
    metadata = live.get("metadata") or {}
    return {
        "apiVersion": live["apiVersion"],
        "kind": live["kind"],
        "metadata": {
            "name": metadata["name"],
            "namespace": metadata["namespace"],
            "labels": dict(metadata.get("labels") or {}),
            "annotations": dict(metadata.get("annotations") or {}),
        },
        "spec": json.loads(json.dumps(live["spec"])),
    }


# ------------------------------------------------------------------------------- endpoints


def check(current_version: str, *, versions: Optional[list[str]] = None) -> dict[str, Any]:
    """`checked=False` with the reason when the registry cannot be consulted -- never a quiet
    `update_available=False`; not knowing is not the same as having nothing to pull."""
    try:
        registry = Registry.from_environment()
        if versions is None:
            versions = list_chart_versions(registry)
    except HTTPException as exc:
        return {"checked": False, "current_version": current_version, "detail": str(exc.detail)}
    except Exception as exc:  # noqa: BLE001 - reported to the admin
        logger.warning("Update check against the chart registry failed: %s", exc)
        return {
            "checked": False,
            "current_version": current_version,
            "detail": f"Could not reach the chart registry: {exc}",
        }

    latest = latest_release(versions)
    available = is_newer(latest, current_version)
    detail = None
    if latest is None:
        detail = "No X.Y.Z chart versions published yet."
    elif parse_version(current_version) is None:
        detail = (
            f"This install reports version {current_version!r}, which is not a release. "
            f"The newest release is {latest}; move it there deliberately."
        )
    return {
        "checked": True,
        "channel": "release",
        "update_available": available,
        "current_version": current_version,
        "latest_tag": latest,
        "detail": detail,
    }


def start(current_version: str, started_by: str) -> dict[str, Any]:
    """Set the HelmRelease to the newest published release. helm-controller does the rest."""
    registry = Registry.from_environment()
    target = latest_release(list_chart_versions(registry))
    if not target or parse_version(target) is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="No X.Y.Z chart version is published to update to.",
        )
    if not is_newer(target, current_version):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Already at {current_version}; the newest published release is {target}.",
        )
    target = target.lstrip("v")

    async def _do() -> None:
        kube = await KubernetesApiClient.connect()
        try:
            release = await _release(kube)
            if release is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=(
                        "This install is not managed by a HelmRelease, so there is nothing "
                        "to advance. Re-run scripts/install-k8s.sh once to adopt it."
                    ),
                )
            desired = _desired(release)
            desired["spec"]["chart"]["spec"]["version"] = target
            # The image tag follows the chart version -- the chart's appVersion is the tag --
            # so no value has to change; an explicit override in values is left alone.
            annotations = desired["metadata"].setdefault("annotations", {})
            annotations["pg.update/requested-at"] = datetime.now(timezone.utc).isoformat()
            annotations["pg.update/requested-by"] = started_by
            await kube.apply_custom_object(
                group=HELM_GROUP,
                version=HELM_VERSION,
                plural=HELM_PLURAL,
                namespace=desired["metadata"]["namespace"],
                name=desired["metadata"]["name"],
                body=desired,
            )
        finally:
            await kube.close()

    _run(_do())
    logger.warning(
        "Platform update to %s requested by %s; helm-controller will roll it out",
        target,
        started_by,
    )
    return {
        "started": True,
        "job_id": f"helmrelease/{get_settings().helm_release_name}@{target}",
        "message": (
            f"Update to {target} requested. The cluster rolls it out; the API restarts as part "
            "of it, so this page will lose contact for a minute and then reconnect."
        ),
    }


def status_of() -> dict[str, Any]:
    """What the HelmRelease says: running while the asked-for version is not the deployed one."""

    async def _do() -> dict[str, Any]:
        kube = await KubernetesApiClient.connect()
        try:
            release = await _release(kube)
        finally:
            await kube.close()
        if release is None:
            return {"state": "idle"}
        wanted = ((release.get("spec") or {}).get("chart") or {}).get("spec", {}).get("version")
        st = release.get("status") or {}
        history = st.get("history") or []
        deployed = history[0].get("chartVersion") if history else None
        ready = next((c for c in st.get("conditions", []) if c.get("type") == "Ready"), {})
        message = ready.get("message")
        requested_at = (release["metadata"].get("annotations") or {}).get("pg.update/requested-at")
        if deployed == wanted and ready.get("status") == "True":
            state = "succeeded"
        elif ready.get("status") == "False" and st.get("lastAttemptedRevision") == wanted:
            state = "failed"
        else:
            state = "running"
        return {
            "state": state,
            "job_id": f"helmrelease/{release['metadata']['name']}@{wanted}",
            "log_tail": message,
            "started_at": requested_at,
        }

    return _run(_do())


def credential() -> dict[str, Any]:
    """The update credential on this path is the image pull secret, managed by the install."""
    try:
        registry = Registry.from_environment()
    except HTTPException:
        return {"configured": False, "readable": False, "username": None}
    return {"configured": True, "readable": True, "username": registry.username}


def refuse_credential_change() -> None:
    raise HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail=(
            "On the Kubernetes path the update credential is the image pull secret, set by "
            "scripts/install-k8s.sh. Change it there, not here."
        ),
    )
