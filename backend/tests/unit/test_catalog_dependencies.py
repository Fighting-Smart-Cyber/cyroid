"""Resolving what a catalog blueprint needs before installing it (PG-149)."""

from proving_ground.catalog.dependencies import (
    BASE_IMAGE,
    BLUEPRINT,
    CONTENT,
    IMAGE,
    Dependency,
    DependencyInstallError,
    resolve_install_plan,
)

BLUEPRINT_ITEM = {
    "id": "red-team-lab",
    "type": "blueprint",
    "name": "Red Team Lab",
    "path": "blueprints/red-team-lab",
    "requires_base_images": ["ubuntu-22-04", "windows-2019"],
    "requires_images": ["custom-web"],
}

INDEX = [
    BLUEPRINT_ITEM,
    {
        "id": "ubuntu-22-04",
        "type": "base_image",
        "name": "Ubuntu 22.04",
        "path": "base_images/ubuntu-22-04",
    },
    {
        "id": "windows-2019",
        "type": "base_image",
        "name": "Windows Server 2019",
        "path": "base_images/windows-2019",
    },
]


def plan_for(item=None, *, index=None, installed=(), images=("custom-web",), **kw):
    return resolve_install_plan(
        item or BLUEPRINT_ITEM,
        index_items=index if index is not None else INDEX,
        installed_item_ids=set(installed),
        available_image_dirs=set(images),
        **kw,
    )


def test_steps_run_in_dependency_order():
    """Images must exist before the blueprint that references them by tag.

    Content is last because it is created by the blueprint installer, not
    referenced by it -- see INSTALL_ORDER.
    """
    plan = plan_for(has_content=True)
    assert [s.kind for s in plan.steps] == [
        BASE_IMAGE,
        BASE_IMAGE,
        IMAGE,
        BLUEPRINT,
        CONTENT,
    ]


def test_every_declared_dependency_becomes_a_step():
    plan = plan_for()
    assert [s.key for s in plan.steps] == [
        "base_image/ubuntu-22-04",
        "base_image/windows-2019",
        "image/custom-web",
        "blueprint/red-team-lab",
    ]
    assert plan.total_steps == 4
    assert plan.warnings == []


def test_already_installed_dependencies_are_skipped_not_reinstalled():
    """AC: skips already-installed dependencies (idempotent)."""
    plan = plan_for(installed=["ubuntu-22-04"])

    ubuntu = one(plan, "base_image/ubuntu-22-04")
    assert ubuntu.satisfied and ubuntu.note == "already installed"
    assert ubuntu not in plan.pending
    assert plan.total_steps == 3
    assert len(plan.steps) == 4  # still reported, just not run


def test_reinstalling_an_installed_blueprint_leaves_nothing_to_do():
    plan = plan_for(installed=["ubuntu-22-04", "windows-2019", "red-team-lab"], images=())
    assert [s.key for s in plan.pending] == []
    assert plan.total_steps == 0


def test_a_base_image_missing_from_the_index_is_reported_not_skipped_silently():
    """The old code logged a warning and carried on, so the blueprint installed broken."""
    plan = plan_for(index=[BLUEPRINT_ITEM])  # no base_image entries at all

    missing = plan.missing
    assert [m.ref for m in missing] == ["ubuntu-22-04", "windows-2019"]
    assert all(not m.available for m in missing)
    assert any("not in the catalog index" in w for w in plan.warnings)
    # Missing steps are not attempted.
    assert [s.key for s in plan.pending] == ["image/custom-web", "blueprint/red-team-lab"]


def test_a_required_image_without_a_dockerfile_is_reported_missing():
    plan = plan_for(images=())
    custom = one(plan, "image/custom-web")
    assert not custom.available
    assert custom.note == "no Dockerfile in the catalog"
    assert any("no Dockerfile" in w for w in plan.warnings)


def test_content_is_a_step_only_when_the_blueprint_ships_it():
    assert not [s for s in plan_for().steps if s.kind == CONTENT]

    plan = plan_for(has_content=True)
    content = one(plan, "content/red-team-lab")
    assert content.name == "Red Team Lab"
    assert content in plan.pending

    installed = plan_for(has_content=True, content_installed=True)
    assert one(installed, "content/red-team-lab").satisfied


def test_duplicate_and_blank_requirements_collapse():
    item = {
        **BLUEPRINT_ITEM,
        "requires_base_images": ["ubuntu-22-04", "ubuntu-22-04", "", None],
        "requires_images": ["custom-web", "custom-web"],
    }
    plan = plan_for(item)
    assert [s.key for s in plan.steps] == [
        "base_image/ubuntu-22-04",
        "image/custom-web",
        "blueprint/red-team-lab",
    ]


def test_a_blueprint_with_no_requirements_is_a_single_step():
    plan = plan_for({"id": "bare", "type": "blueprint", "name": "Bare", "path": "b/bare"})
    assert [s.key for s in plan.steps] == ["blueprint/bare"]
    assert plan.total_steps == 1


def test_labels_name_the_thing_being_installed():
    plan = plan_for(has_content=True)
    assert one(plan, "base_image/windows-2019").label == (
        "Installing VM image: Windows Server 2019"
    )
    assert one(plan, "image/custom-web").label == "Building image: custom-web"
    assert one(plan, "content/red-team-lab").label == "Linking content: Red Team Lab"
    assert one(plan, "blueprint/red-team-lab").label == "Installing blueprint: Red Team Lab"


def test_describe_summarises_the_plan():
    plan = plan_for(installed=["ubuntu-22-04"], images=())
    assert plan.describe() == "2 step(s), 1 already installed, 1 missing from catalog"


def test_dependency_error_names_the_step_that_failed():
    dep = Dependency(kind=IMAGE, ref="custom-web", name="custom-web")
    err = DependencyInstallError(dep, RuntimeError("docker build exited 1"))
    assert "Building image: custom-web" in str(err)
    assert "docker build exited 1" in str(err)
    assert err.dependency is dep


def one(plan, key):
    matches = [s for s in plan.steps if s.key == key]
    assert len(matches) == 1, f"{key} not in {[s.key for s in plan.steps]}"
    return matches[0]
