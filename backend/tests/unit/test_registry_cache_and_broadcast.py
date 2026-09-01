"""Two defects seen in one deploy's logs.

    Failed to push '172.30.0.16:5000/dockurr/windows@sha256:...' to registry
    Failed to cache internet-pulled image ... to registry
    Failed to broadcast notification: EventBroadcaster.broadcast() missing 1
        required positional argument: 'message'

Neither was fatal, and both were swallowed and logged -- which is why they
persisted.
"""

import inspect


class TestRegistryCaching:
    """The image came FROM the mirror; caching it back is pointless.

    The host-side transfer fails first because the host daemon speaks HTTPS to
    a plain-HTTP mirror, so the code falls through to a direct pull inside
    DinD. That pull uses a reference already qualified with our own registry,
    so it is a registry pull -- but it was recorded as an internet pull, which
    sent it on to be cached back into the registry it had just come from. That
    push then failed every deploy, because a digest reference cannot be pushed
    at all: docker push takes a tag.
    """

    def _src(self):
        from proving_ground.services.docker_service import DockerService

        return inspect.getsource(DockerService.pull_image_to_dind)

    def test_a_pull_from_our_own_mirror_is_not_called_an_internet_pull(self):
        src = self._src()
        assert "from_mirror" in src, (
            "A pull from the local mirror is still recorded as an internet "
            "pull, so it gets re-cached into the registry it came from."
        )
        idx = src.find("from_mirror")
        assert (
            'result["source"] = "registry"' in src[idx:]
        ), "The source is not corrected for a mirror pull."

    def test_a_digest_reference_is_not_pushed(self):
        src = self._src()
        assert '"@sha256:" in image' in src, (
            "A digest-referenced image is still handed to the registry push, "
            "which cannot push it -- docker push takes a tag."
        )


class TestNotificationBroadcast:
    """broadcast() is async and takes (event_type, message, ...).

    It was called with a single dict and never awaited, so no notification has
    ever reached a client. The TypeError was caught and logged as a warning,
    which is why it survived.
    """

    def _src(self):
        from proving_ground.services.notification_service import NotificationService

        return inspect.getsource(NotificationService._broadcast_notification)

    def test_it_passes_event_type_and_message(self):
        src = self._src()
        assert "event_type=" in src and "message=" in src, (
            "The broadcast is still called positionally with a dict; it raises "
            "TypeError before anything is sent."
        )
        assert "broadcaster.broadcast(event_data)" not in src

    def test_the_coroutine_is_actually_run(self):
        src = self._src()
        assert "create_task" in src or "asyncio.run" in src, (
            "The coroutine is never scheduled, so even a well-formed call "
            "would not deliver anything."
        )

    def test_it_handles_being_called_without_a_running_loop(self):
        """Notifications are created from sync endpoints and worker threads."""
        src = self._src()
        assert "RuntimeError" in src, (
            "No fallback for the no-running-loop case, which is how every "
            "sync caller reaches this."
        )
