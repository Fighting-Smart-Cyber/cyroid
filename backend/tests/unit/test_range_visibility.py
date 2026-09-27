"""A range is private unless someone decides otherwise.

Reported after the console fix: blocking the console did not stop another
account seeing that the range existed, listing it, and opening its detail. Only
the console was refused.

The cause was a default nobody chose. Visibility was inferred from tags, and
`filter_by_visibility` treated "has no tags" as public — so every range anyone
created was visible to every non-student account. Sharing was the absence of an
action rather than an action.

Visibility is now explicit: private, shared (by named user or by tag), or
public. Existing rows migrate to PRIVATE, which is the point — preserving the
old behaviour would mean making everything public.
"""

import inspect
import pathlib
import re

from proving_ground.api import deps
from proving_ground.models.range import RangeVisibility


def _code_of(fn) -> str:
    """Source with the docstring removed.

    These checks are about what a function DOES. Docstrings here deliberately
    name the very things being excluded, in order to explain why -- and three
    separate assertions in this session tripped over their own explanations
    before this helper existed.
    """
    src = inspect.getsource(fn)
    parts = src.split('"""')
    return parts[0] + "".join(parts[2:]) if len(parts) >= 3 else src


MIGRATION = (
    pathlib.Path(__file__).resolve().parents[2]
    / "alembic"
    / "versions"
    / "d1a2b3c4e5f6_add_range_visibility.py"
)


class TestTheDefaultIsClosed:
    def test_the_model_defaults_to_private(self):
        src = inspect.getsource(__import__("proving_ground.models.range", fromlist=["x"]))
        assert "default=RangeVisibility.PRIVATE" in src, (
            "a new range defaults to something other than private, which is the "
            "permissive default this replaced"
        )

    def test_the_migration_lands_existing_rows_private(self):
        src = MIGRATION.read_text()
        assert 'server_default="private"' in src, (
            "existing ranges do not migrate to private; the old open behaviour "
            "survives the migration"
        )

    def test_the_column_is_not_nullable(self):
        """A NULL visibility has no defined meaning and would have to be
        guessed at by every reader."""
        assert "nullable=False" in MIGRATION.read_text()

    def test_the_three_states_are_what_the_api_accepts(self):
        assert {v.value for v in RangeVisibility} == {"private", "shared", "public"}


class TestSightIsGrantedOnlyByOwnershipShareOrPublic:
    def test_the_filter_does_not_treat_untagged_as_public(self):
        src = _code_of(deps._filter_ranges_by_visibility)
        assert (
            "~model_class.id.in_" not in src
        ), "the range filter is back to 'no tags = public', which is the bug"

    def test_the_filter_offers_exactly_the_intended_paths(self):
        src = _code_of(deps._filter_ranges_by_visibility)
        for expected in (
            "model_class.created_by == current_user.id",
            "RangeVisibility.PUBLIC",
            "RangeShare.user_id == current_user.id",
        ):
            assert expected in src, f"missing grant path: {expected}"

    def test_a_tag_only_reveals_a_shared_range(self):
        """Otherwise holding a tag would surface a range its owner made
        private, which is worse than the default it replaced."""
        src = _code_of(deps._filter_ranges_by_visibility)
        tag_branch = src[src.index("if user_tags:") :]
        assert "RangeVisibility.SHARED" in tag_branch

    def test_direct_access_applies_the_same_rule_as_the_list(self):
        """A private range must not be merely hidden from the list while a GET
        by id still returns it."""
        src = _code_of(deps.check_resource_access)
        assert 'resource_type == "range"' in src
        assert "RangeVisibility.PUBLIC" in src
        assert "RangeShare" in src

    def test_a_private_range_refuses_rather_than_falling_through(self):
        src = _code_of(deps.check_resource_access)
        block = src[src.index('if resource_type == "range":') :]
        block = block[: block.index("# Check resource tags")]
        assert "HTTP_403_FORBIDDEN" in block


class TestChangingVisibilityIsTheOwnersToMake:
    def test_setting_visibility_requires_control_not_access(self):
        """Someone a range was shared with must not be able to widen it."""
        from proving_ground.api import ranges as ranges_api

        src = _code_of(ranges_api.set_range_visibility)
        assert "check_resource_control" in src
        assert "check_resource_access" not in src

    def test_reading_visibility_requires_only_access(self):
        from proving_ground.api import ranges as ranges_api

        src = _code_of(ranges_api.get_range_visibility)
        assert "check_resource_access" in src

    def test_an_unknown_visibility_is_rejected(self):
        from proving_ground.api import ranges as ranges_api

        src = _code_of(ranges_api.set_range_visibility)
        assert "HTTP_400_BAD_REQUEST" in src

    def test_omitting_the_share_list_leaves_it_alone(self):
        """Flipping private -> shared should not silently drop existing grants."""
        from proving_ground.api import ranges as ranges_api

        src = _code_of(ranges_api.set_range_visibility)
        assert "if data.shared_with is not None:" in src

    def test_sharing_with_an_unknown_user_is_rejected(self):
        from proving_ground.api import ranges as ranges_api

        src = _code_of(ranges_api.set_range_visibility)
        assert "No such user" in src


class TestSharingIsNotControl:
    def test_a_share_does_not_appear_in_any_control_path(self):
        """Sharing shows a range. It does not hand over the power to tear it
        down, and the control check must not consult the share table."""
        src = _code_of(deps.check_resource_control)
        assert "RangeShare" not in src
        assert "visibility" not in src

    def test_console_access_is_not_widened_by_a_share(self):
        """A shared range is visible, not operable. Console entitlement stays
        owner, admin, assigned learner or event participant."""
        from proving_ground.api import vms as vms_api

        src = _code_of(vms_api.check_console_access)
        assert "RangeShare" not in src
