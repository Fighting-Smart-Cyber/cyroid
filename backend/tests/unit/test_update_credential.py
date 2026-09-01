"""The update needs a credential the platform can hold itself.

The host authenticates to the code remote through a git helper that shells out
to `glab`, using a token in the operator's own store. The update container has
neither the binary nor the token, so the first real run got as far as `git
fetch` and stopped at "could not read Password".

The credential therefore belongs to the platform. Not in the environment: an
operator with a browser and no shell cannot edit a .env, and Compose loads .env
into every container, so a token there is readable from any service that
happens to be compromised rather than only the one that needs it.
"""

import inspect

from proving_ground.api import admin as admin_api


def _update_path_source() -> str:
    """The update's whole implementation, not just the endpoint.

    The endpoint delegates the container spec and the git preamble to pieces it
    shares with the update check, so that one definition covers both. These
    assertions are about what an update does, never about which function
    currently holds a given line.
    """
    return "\n".join(
        [
            inspect.getsource(admin_api.start_platform_update),
            inspect.getsource(admin_api._repo_container_spec),
            admin_api._GIT_SETUP,
        ]
    )


from proving_ground.services import platform_secret_service


class TestTheTokenIsWriteOnly:
    def test_the_read_endpoint_reports_status_not_the_token(self):
        """The decrypted value may only be used as a yes/no, never returned.

        Asserted on how `value` is used rather than whether it appears: it has
        one legitimate use here -- `readable=value is not None` -- and a blanket
        substring check calls that a leak.
        """
        src = inspect.getsource(admin_api.get_update_credential)
        assert "configured=" in src
        uses = [
            ln.strip() for ln in src.splitlines() if "value" in ln and "value_encrypted" not in ln
        ]
        allowed = {
            "value, row = platform_secret_service.get_secret(db, GIT_CREDENTIAL_KEY)",
            "readable=value is not None,",
        }
        unexpected = [u for u in uses if u not in allowed and not u.startswith("#")]
        assert not unexpected, (
            f"The decrypted value is used for something other than a presence "
            f"check: {unexpected}. A token that can be read back can be "
            f"exfiltrated by anything that reaches the API."
        )

    def test_the_status_model_has_no_token_field(self):
        fields = set(admin_api.GitCredentialStatus.model_fields)
        assert "token" not in fields and "value" not in fields, (
            f"GitCredentialStatus exposes {fields}; the token must never leave " f"the server."
        )

    def test_the_model_repr_omits_the_value(self):
        """A stray log line should not print a secret."""
        from proving_ground.models.platform_secret import PlatformSecret

        src = inspect.getsource(PlatformSecret.__repr__)
        assert "value_encrypted" not in src


class TestStorage:
    def test_it_is_encrypted_at_rest(self):
        src = inspect.getsource(platform_secret_service.set_secret)
        assert (
            "Fernet" in src and "encrypt" in src
        ), "The token is stored in plaintext, so a database dump hands it over."

    def test_an_unreadable_secret_does_not_raise(self):
        """Rotating the JWT secret makes stored values undecryptable. The useful
        response is to ask for it again, not to 500."""
        src = inspect.getsource(platform_secret_service.get_secret)
        assert "InvalidToken" in src and "return None" in src

    def test_an_unreadable_credential_is_reported_as_such(self):
        src = inspect.getsource(admin_api.get_update_credential)
        assert "readable=" in src, (
            "An undecryptable credential is reported as configured, so the "
            "update fails later with a confusing git error instead."
        )


class TestItReachesTheUpdateContainer:
    def test_the_token_goes_through_the_environment_not_the_command(self):
        """A token on the command line shows up in `ps` and in the container's
        own Config.Cmd, which any reader of the Docker socket can see."""
        src = _update_path_source()
        assert "GIT_ASKPASS_TOKEN" in src
        assert '"environment": env' in src
        assert (
            "password=$GIT_ASKPASS_TOKEN" in src
        ), "The helper should read the token from the environment at run time."
        # The literal token must not be interpolated into the script.
        assert "{git_token}" not in src, (
            "The token is formatted into the script text, which puts it in the "
            "container's command."
        )

    def test_a_missing_credential_still_attempts_the_update(self):
        """A public remote, or one reachable another way, should still work."""
        src = _update_path_source()
        assert 'if [ -n "${GIT_ASKPASS_TOKEN:-}" ]' in src, (
            "The helper is configured unconditionally, so an update with no "
            "stored credential breaks a remote that needs none."
        )

    def test_an_undecryptable_credential_stops_before_launching(self):
        src = _update_path_source()
        assert "cannot be decrypted" in src, (
            "The update launches with no usable credential and fails at fetch, "
            "where the cause is much harder to see."
        )


class TestAdminOnly:
    def test_every_credential_endpoint_requires_an_admin(self):
        for fn in (
            admin_api.set_update_credential,
            admin_api.get_update_credential,
            admin_api.delete_update_credential,
        ):
            assert "AdminUser" in inspect.getsource(fn), f"{fn.__name__} does not require an admin."
