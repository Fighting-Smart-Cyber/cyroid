"""A catalog source and everything its index declares are untrusted input.

SEC-029: the source URL reaches ``git clone``. git reads an argument beginning
with a dash as an option, and several of its transports run a command, so a URL
an admin pastes in must never be able to become one.

SEC-030: index.json is fetched from a remote repository, so every path in it is
attacker-controlled. Nothing the index names may be read from, or written to,
outside the catalog.

These drive the real service against real catalogs on disk. git is never
executed: the one test that reaches the command line captures the argv instead.
"""

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from proving_ground.models.blueprint import RangeBlueprint
from proving_ground.models.catalog import (
    CatalogInstalledItem,
    CatalogSource,
    CatalogSourceType,
)
from proving_ground.schemas.catalog import (
    CatalogItemSummary,
    CatalogSourceCreate,
    CatalogSourceUpdate,
)
from proving_ground.services import catalog_service as catalog_service_module
from proving_ground.services import scenario_filesystem
from proving_ground.services.catalog_service import CatalogService, resolve_declared_path

USER_ID = uuid4()

BLUEPRINT_YAML = """\
name: Red Team Lab
description: A small lab.
seed_id: sec030-red-team-lab
networks:
  - name: corp
    subnet: 10.10.0.0/24
    gateway: 10.10.0.1
vms:
  - hostname: dc-01
    base_image_tag: ubuntu-22-04
    network_name: corp
    ip_address: 10.10.0.10
"""

SECRET = "TOP SECRET, AND NOT PART OF ANY CATALOG"


def blueprint_item(**overrides):
    """An index entry in the shape a real catalog publishes."""
    item = {
        "id": "red-team-lab",
        "type": "blueprint",
        "name": "Red Team Lab",
        "description": "A small lab.",
        "path": "blueprints/red-team-lab",
        "version": "1.0.0",
    }
    item.update(overrides)
    return item


def write_catalog(root, items):
    """Write an index.json listing exactly the entries given."""
    root.mkdir(parents=True, exist_ok=True)
    (root / "index.json").write_text(
        json.dumps({"catalog": {"name": "Test Catalog", "version": "1.0"}, "items": items}),
        encoding="utf-8",
    )
    return root


def write_blueprint(root, path="blueprints/red-team-lab", readme=None):
    item_dir = root / path
    item_dir.mkdir(parents=True, exist_ok=True)
    (item_dir / "blueprint.yaml").write_text(BLUEPRINT_YAML, encoding="utf-8")
    if readme is not None:
        (item_dir / "README.md").write_text(readme, encoding="utf-8")
    return item_dir


@pytest.fixture
def outside(tmp_path):
    """A directory beside the catalog holding something no catalog may reach."""
    target = tmp_path / "outside"
    target.mkdir()
    (target / "secret.md").write_text(SECRET, encoding="utf-8")
    return target


@pytest.fixture
def images_dir(tmp_path, monkeypatch):
    """Keep image-project copies inside the test's tmp dir, not /data/images."""
    target = tmp_path / "data-images"
    monkeypatch.setattr(catalog_service_module, "IMAGES_DIR", str(target))
    return target


@pytest.fixture
def scenarios_dir(tmp_path, monkeypatch):
    """Keep installed scenarios inside the test's tmp dir, not /data/scenarios."""
    target = tmp_path / "data-scenarios"
    monkeypatch.setattr(scenario_filesystem, "SCENARIOS_DIR", target)
    return target


@pytest.fixture
def service(db_session):
    return CatalogService(db_session)


def local_source(db_session, root):
    source = CatalogSource(name="Test Catalog", source_type=CatalogSourceType.LOCAL, url=str(root))
    db_session.add(source)
    db_session.commit()
    return source


# ============ SEC-029: the source URL reaches git ============


@pytest.mark.parametrize(
    "url",
    [
        "--upload-pack=touch /tmp/pg-owned",
        "--config=core.pager=id",
        "-c",
    ],
)
def test_a_url_git_would_read_as_an_option_is_refused_at_the_schema(url):
    with pytest.raises(ValidationError) as exc:
        CatalogSourceCreate(name="Catalog", url=url)
    assert "must not begin with '-'" in str(exc.value)


@pytest.mark.parametrize(
    "url",
    [
        "ext::sh -c id",
        "ext::sh",
        "file:///etc",
        "javascript:alert(1)",
    ],
)
def test_a_transport_that_is_not_a_clone_is_refused_at_the_schema(url):
    with pytest.raises(ValidationError):
        CatalogSourceCreate(name="Catalog", url=url)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.test/org/catalog.git",
        "http://10.10.100.100:3000/org/catalog.git",
        "ssh://git@example.test/org/catalog.git",
        "git://example.test/org/catalog.git",
        "git@example.test:org/catalog.git",
        # An air-gapped site clones from a mirror on disk.
        "/data/mirrors/catalog.git",
    ],
)
def test_the_urls_a_catalog_actually_uses_are_accepted(url):
    assert CatalogSourceCreate(name="Catalog", url=url).url == url


def test_a_local_source_is_still_a_filesystem_path():
    created = CatalogSourceCreate(
        name="Catalog", source_type=CatalogSourceType.LOCAL, url="/data/catalogs/local"
    )
    assert created.url == "/data/catalogs/local"


def test_a_branch_git_would_read_as_an_option_is_refused():
    with pytest.raises(ValidationError) as exc:
        CatalogSourceCreate(
            name="Catalog", url="https://example.test/c.git", branch="--upload-pack=id"
        )
    assert "must not begin with '-'" in str(exc.value)


def test_an_update_is_held_to_the_same_rules():
    with pytest.raises(ValidationError):
        CatalogSourceUpdate(url="--upload-pack=id")
    with pytest.raises(ValidationError):
        CatalogSourceUpdate(url="ext::sh -c id")
    assert CatalogSourceUpdate(url="https://example.test/c.git").url == "https://example.test/c.git"


def test_the_service_refuses_a_url_that_never_went_through_the_schema(
    db_session, tmp_path, monkeypatch
):
    """A row can predate the schema check, so the call site refuses as well."""

    def never(*args, **kwargs):
        raise AssertionError(f"git was invoked with {args!r}")

    monkeypatch.setattr(catalog_service_module.subprocess, "run", never)

    source = CatalogSource(
        name="Hostile",
        source_type=CatalogSourceType.GIT,
        url="--upload-pack=touch /tmp/pg-owned",
    )
    db_session.add(source)
    db_session.commit()

    svc = CatalogService(db_session)
    svc.settings = SimpleNamespace(catalog_storage_dir=str(tmp_path / "catalogs"))

    with pytest.raises(ValueError) as exc:
        svc.sync_source(source)
    assert "must not begin with '-'" in str(exc.value)
    # The operator is told why, rather than being left with a source that
    # reports 'syncing' forever.
    assert source.error_message and "'-'" in source.error_message


def test_the_clone_puts_its_positionals_after_a_terminator(db_session, tmp_path, monkeypatch):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(catalog_service_module.subprocess, "run", fake_run)

    source = CatalogSource(
        name="Catalog",
        source_type=CatalogSourceType.GIT,
        url="https://example.test/org/catalog.git",
        branch="main",
    )
    db_session.add(source)
    db_session.commit()

    svc = CatalogService(db_session)
    svc.settings = SimpleNamespace(catalog_storage_dir=str(tmp_path / "catalogs"))
    svc._sync_git(source)

    cmd = captured["cmd"]
    assert "--" in cmd, cmd
    # Everything after the terminator is a positional, and nothing before it
    # came from the source.
    assert cmd[cmd.index("--") + 1] == "https://example.test/org/catalog.git"
    assert cmd[: cmd.index("--")] == ["git", "clone", "--depth", "1", "--branch", "main"]


# ============ SEC-030: index.json declares the paths ============


def test_resolve_declared_path_refuses_an_escape_and_allows_a_normal_path(tmp_path):
    root = tmp_path / "catalog"
    (root / "blueprints").mkdir(parents=True)

    assert (
        resolve_declared_path(root, "blueprints", what="item path")
        == (root / "blueprints").resolve()
    )
    assert resolve_declared_path(root, "", what="item path") == root.resolve()

    for declared in ("../outside", "/etc", "blueprints/../../outside"):
        with pytest.raises(ValueError) as exc:
            resolve_declared_path(root, declared, what="item path")
        assert "outside the catalog root" in str(exc.value)


def test_a_symlink_out_of_the_catalog_is_refused_even_though_the_index_looks_innocent(
    tmp_path, outside
):
    root = tmp_path / "catalog"
    (root / "blueprints").mkdir(parents=True)
    (root / "blueprints" / "leak").symlink_to(outside)

    with pytest.raises(ValueError):
        resolve_declared_path(root, "blueprints/leak", what="item path")


@pytest.mark.parametrize("path", ["../outside", "../../../../etc", "/etc"])
def test_an_index_path_that_leaves_the_catalog_is_refused_at_the_schema(path):
    with pytest.raises(ValidationError):
        CatalogItemSummary(id="x", type="blueprint", name="X", path=path)


def test_an_item_id_that_is_a_path_is_refused_at_the_schema():
    with pytest.raises(ValidationError):
        CatalogItemSummary(id="../../escape", type="scenario", name="X")


def test_browsing_omits_the_unusable_entry_and_keeps_the_rest(service, db_session, tmp_path):
    root = write_catalog(
        tmp_path / "catalog",
        [
            blueprint_item(id="hostile", path="../outside"),
            blueprint_item(),
        ],
    )
    write_blueprint(root)
    source = local_source(db_session, root)

    assert [item.id for item in service.list_items(source)] == ["red-team-lab"]


def test_one_unusable_entry_does_not_blank_the_catalog_page(service, db_session, tmp_path):
    """An index the platform cannot read at all is still mostly readable."""
    root = write_catalog(
        tmp_path / "catalog",
        [
            blueprint_item(id="mystery", type="not-a-type"),
            blueprint_item(),
        ],
    )
    write_blueprint(root)
    source = local_source(db_session, root)

    assert [item.id for item in service.list_items(source)] == ["red-team-lab"]


def test_an_item_whose_path_leaves_the_catalog_has_no_detail_to_read(
    service, db_session, tmp_path, outside
):
    root = write_catalog(tmp_path / "catalog", [blueprint_item(path="../outside")])
    source = local_source(db_session, root)

    assert service.get_item_detail(source, "red-team-lab") is None


def test_a_symlinked_item_directory_is_not_read_back(service, db_session, tmp_path, outside):
    """The index says 'blueprints/leak'; the catalog committed a link out."""
    root = write_catalog(tmp_path / "catalog", [blueprint_item(path="blueprints/leak")])
    (root / "blueprints").mkdir(parents=True, exist_ok=True)
    (root / "blueprints" / "leak").symlink_to(outside)
    source = local_source(db_session, root)

    assert service.get_item_detail(source, "red-team-lab") is None


def test_a_symlinked_readme_is_not_read_back(service, db_session, tmp_path, outside):
    root = write_catalog(tmp_path / "catalog", [blueprint_item()])
    item_dir = write_blueprint(root)
    (item_dir / "README.md").symlink_to(outside / "secret.md")
    source = local_source(db_session, root)

    assert service.get_item_detail(source, "red-team-lab") is None
    assert SECRET not in json.dumps([i.model_dump() for i in service.list_items(source)])


def test_an_item_declared_outside_the_catalog_cannot_be_installed(
    service, db_session, tmp_path, outside, images_dir
):
    root = write_catalog(tmp_path / "catalog", [blueprint_item(path="../outside")])
    source = local_source(db_session, root)

    with pytest.raises(ValueError):
        service.install_item(source, "red-team-lab", USER_ID, build_images=False)

    assert db_session.query(CatalogInstalledItem).count() == 0
    assert db_session.query(RangeBlueprint).count() == 0
    assert (outside / "secret.md").read_text(encoding="utf-8") == SECRET


def test_a_required_image_that_climbs_out_of_the_catalog_refuses_the_install(
    service, db_session, tmp_path, images_dir
):
    """requires_images entries become path segments and are not schema-checked."""
    root = write_catalog(
        tmp_path / "catalog", [blueprint_item(requires_images=["../../../../etc"])]
    )
    write_blueprint(root)
    source = local_source(db_session, root)

    with pytest.raises(ValueError) as exc:
        service.install_item(source, "red-team-lab", USER_ID, build_images=False)
    assert "outside the catalog root" in str(exc.value)

    assert db_session.query(RangeBlueprint).count() == 0
    assert not images_dir.exists() or list(images_dir.iterdir()) == []


def test_a_required_base_image_path_that_leaves_the_catalog_refuses_the_install(
    service, db_session, tmp_path, outside, images_dir
):
    root = write_catalog(
        tmp_path / "catalog",
        [
            blueprint_item(requires_base_images=["ubuntu-22-04"]),
            {
                "id": "ubuntu-22-04",
                "type": "base_image",
                "name": "Ubuntu",
                "description": "",
                "path": "../outside",
                "version": "1.0.0",
            },
        ],
    )
    write_blueprint(root)
    source = local_source(db_session, root)

    with pytest.raises(ValueError) as exc:
        service.install_item(source, "red-team-lab", USER_ID, build_images=False)
    assert "outside the catalog root" in str(exc.value)


def test_an_image_project_name_cannot_write_outside_the_image_library(
    service, tmp_path, images_dir
):
    """The name arrives from the index, so the destination is resolved too."""
    src = tmp_path / "project"
    src.mkdir()
    (src / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")

    with pytest.raises(ValueError) as exc:
        service._install_image_from_path(src, "../escaped", False)
    assert "the image library" in str(exc.value)
    assert not (images_dir.parent / "escaped").exists()


def test_a_scenario_id_that_is_a_path_writes_nothing_outside_the_scenario_directory(
    service, db_session, tmp_path, scenarios_dir
):
    root = write_catalog(
        tmp_path / "catalog",
        [
            {
                "id": "../escape",
                "type": "scenario",
                "name": "Escape",
                "description": "",
                "path": "scenarios",
                "version": "1.0.0",
            }
        ],
    )
    (root / "scenarios").mkdir(parents=True)
    (root / "scenarios" / "escape.yaml").write_text("name: escape\n", encoding="utf-8")
    source = local_source(db_session, root)

    with pytest.raises(ValueError):
        service.install_item(source, "../escape", USER_ID, build_images=False)

    assert not (tmp_path / "escape.yaml").exists()
    assert not scenarios_dir.exists() or list(scenarios_dir.iterdir()) == []


def test_a_normal_catalog_still_browses_and_installs(
    service, db_session, tmp_path, images_dir, scenarios_dir
):
    root = write_catalog(tmp_path / "catalog", [blueprint_item()])
    write_blueprint(root, readme="# Red Team Lab\n\nA small lab.\n")
    source = local_source(db_session, root)

    assert [item.id for item in service.list_items(source)] == ["red-team-lab"]

    detail = service.get_item_detail(source, "red-team-lab")
    assert detail is not None
    assert detail.readme.startswith("# Red Team Lab")

    installed = service.install_item(source, "red-team-lab", USER_ID, build_images=False)
    assert installed.catalog_item_id == "red-team-lab"
    blueprint = (
        db_session.query(RangeBlueprint)
        .filter(RangeBlueprint.id == installed.local_resource_id)
        .first()
    )
    assert blueprint is not None and blueprint.name == "Red Team Lab"


class TestTheOneClickInstallerResolvesToo:
    """The dependency-resolving route bypasses the guarded loop, so it needs its own.

    `_install_blueprint`'s `requires_images` loop is guarded, but the one-click installer calls
    `install_item(..., install_dependencies=False)` and copies each image step itself. A
    directory symlinked out of the catalog was copied in with its contents intact -- and the
    install-plan route, which any authenticated account can call, stat'd whatever path the index
    declared, answering "does this file exist" for anywhere the API can reach.
    """

    def test_both_joins_go_through_the_resolver(self):
        import inspect

        from proving_ground.catalog import installer

        source = inspect.getsource(installer)
        # The two sites the audit proved reachable. A bare `catalog_root / <declared>` here is
        # the defect; every declared path has to be resolved and contained first.
        assert "catalog_root / item.get(" not in source
        assert "catalog_root / step.path" not in source
        assert source.count("resolve_declared_path(") >= 2

    def test_a_symlinked_image_directory_is_refused(self, tmp_path):
        from proving_ground.services.catalog_service import resolve_declared_path

        root = tmp_path / "catalog"
        (root / "images").mkdir(parents=True)
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "secret.env").write_text("TOKEN=x")
        (root / "images" / "leak").symlink_to(outside, target_is_directory=True)

        with pytest.raises(ValueError, match="outside the catalog root"):
            resolve_declared_path(root, "images/leak", what="image 'leak' path")
