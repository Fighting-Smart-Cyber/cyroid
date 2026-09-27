"""Which range a hostname names -- one parser, not two.

The canonical-UUID rule lived in `kubernetes_range_service.range_id_from_key` and the
suffix-and-port handling lived inside `kubernetes_apps._host_and_range`. Both halves answer one
question, and pg-gateway has to answer it identically or a request authorised for one range
reaches another. A second implementation of a security-relevant parser is how those drift, so
there is now one, in a module that imports nothing from the engine.
"""

import uuid

import pytest

from proving_ground.services import kubernetes_range_service as svc
from proving_ground.utils.range_hosts import range_id_from_host, range_id_from_key

SUFFIX = "apps.example.com"


@pytest.fixture
def rid():
    return uuid.uuid4()


class TestOnlyTheCanonicalSpelling:
    """A second accepted spelling is a second host, and the ticket cookie is host-scoped -- so
    it is a second security context for the same range, not a convenience."""

    def test_the_canonical_form_resolves(self, rid):
        assert range_id_from_key(str(rid)) == rid

    @pytest.mark.parametrize("shape", ["braced", "urn", "nohyphens"])
    def test_the_other_forms_uuid_accepts_are_refused(self, rid, shape):
        key = {
            "braced": f"{{{rid}}}",
            "urn": f"urn:uuid:{rid}",
            "nohyphens": str(rid).replace("-", ""),
        }[shape]
        assert range_id_from_key(key) is None

    def test_the_service_still_exposes_it(self, rid):
        """`svc.range_id_from_key` is what callers and tests already say."""
        assert svc.range_id_from_key(svc.range_key(rid)) == rid


class TestTheHostMustNameExactlyOneRange:
    def test_a_range_host_resolves(self, rid):
        assert range_id_from_host(f"{rid}.{SUFFIX}", SUFFIX) == rid

    def test_a_port_is_not_part_of_the_name(self, rid):
        assert range_id_from_host(f"{rid}.{SUFFIX}:8442", SUFFIX) == rid

    def test_case_and_a_trailing_dot_do_not_change_the_answer(self, rid):
        assert range_id_from_host(f"{str(rid).upper()}.{SUFFIX.upper()}.", SUFFIX) == rid

    def test_a_deeper_label_is_refused(self, rid):
        """Without this, a.b.<suffix> resolves and a range answers on names nobody published."""
        assert range_id_from_host(f"extra.{rid}.{SUFFIX}", SUFFIX) is None

    @pytest.mark.parametrize(
        "authority",
        ["", "apps.example.com", "nope.apps.example.com", "somethingelse.com"],
    )
    def test_anything_that_does_not_name_a_range_is_refused(self, authority):
        assert range_id_from_host(authority, SUFFIX) is None

    def test_a_port_that_is_not_digits_does_not_survive(self, rid):
        """It stays in the string the suffix is matched against, and therefore fails."""
        assert range_id_from_host(f"{rid}.{SUFFIX}:notaport", SUFFIX) is None

    def test_no_configured_suffix_means_nothing_resolves(self, rid):
        assert range_id_from_host(f"{rid}.{SUFFIX}", "") is None

    def test_the_suffix_boundary_is_a_label_not_a_substring(self, rid):
        """`evilapps.example.com` must not match a suffix of `apps.example.com`."""
        assert range_id_from_host(f"{rid}.evil{SUFFIX}", SUFFIX) is None
