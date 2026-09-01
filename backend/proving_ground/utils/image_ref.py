"""Container image reference handling.

A RepoDigest read from a running container is qualified with whatever registry
it was pulled from. On this host that is the local mirror, so a captured golden
image recorded 172.30.0.16:5000/dockurr/windows@sha256:... -- correct here and
meaningless anywhere else.

Worse, it is not only a portability problem. The mirror-qualified form also:

  * never matches the warm pool's image set, which is populated from tags, so
    every Windows range fell back to cold provisioning; and
  * cannot be pulled by the host daemon, which speaks HTTPS to a plain-HTTP
    mirror ("server gave HTTP response to HTTPS client"), so the pull happened
    inside DinD and the cache-back failed -- repeating on every deploy.

So references are stored registry-agnostic (repo@sha256:...) and the registry
is applied when pulling, which is where it belongs.
"""

from typing import Optional


# A first path segment is a registry host if it looks like one: it has a dot
# (a domain) or a colon (host:port). "dockurr/windows" has neither, so the
# leading segment there is a namespace and must be kept.
def _looks_like_registry(segment: str) -> bool:
    return "." in segment or ":" in segment or segment == "localhost"


def strip_registry(reference: Optional[str]) -> Optional[str]:
    """Drop a leading registry host, keeping repo[:tag] or repo@digest."""
    if not reference:
        return reference
    head, sep, tail = reference.partition("/")
    if sep and _looks_like_registry(head):
        return tail
    return reference


def is_digest_ref(reference: Optional[str]) -> bool:
    """True for repo@sha256:... references.

    A digest reference cannot be pushed to a registry: there is no tag to push
    to, and splitting one on its last colon yields the nonsense repository
    "<repo>@sha256". Callers that build a push target need the difference.
    """
    return bool(reference) and "@sha256:" in reference


def repo_of(reference: Optional[str]) -> Optional[str]:
    """The bare repository path: no registry, no tag, no digest.

    Used to decide whether a warm pool member carrying dockurr/windows:latest
    can host a range that wants dockurr/windows@sha256:... -- it can. The exact
    digest is still pulled if absent, and pulling inside a warm container beats
    provisioning a cold one.
    """
    if not reference:
        return reference
    ref = strip_registry(reference)
    if "@" in ref:
        return ref.split("@", 1)[0]
    # Only the last segment may carry a tag; a port would have been the registry.
    head, sep, tail = ref.rpartition("/")
    if ":" in tail:
        tail = tail.split(":", 1)[0]
    return f"{head}{sep}{tail}"


def with_registry(reference: Optional[str], registry: Optional[str]) -> Optional[str]:
    """Qualify a reference with a registry, replacing one already present."""
    if not reference or not registry:
        return reference
    return f"{registry.rstrip('/')}/{strip_registry(reference)}"


def runtime_ref_for_pull(reference: Optional[str], mirror: Optional[str]) -> Optional[str]:
    """The form to hand the pull path for a stored runtime reference.

    Storage is registry-agnostic so it means the same thing on every
    deployment; pulling needs somewhere to pull *from*. In an air-gapped range
    that has to be the local mirror, so qualify here rather than leaving a bare
    reference to resolve against docker.io.
    """
    return with_registry(reference, mirror) if mirror else reference
