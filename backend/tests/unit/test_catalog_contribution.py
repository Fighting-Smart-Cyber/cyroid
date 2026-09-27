"""Contributing a locally-modified blueprint back to its catalog (PG-147)."""

import subprocess
import textwrap
from pathlib import Path

import pytest
import yaml

from proving_ground.catalog.contribution import (
    ADDED,
    CHANGED,
    REMOVED,
    apply_changes,
    build_patch,
    diff_blueprint_against_catalog,
    render_yaml,
)
from proving_ground.services.catalog_service import build_config_from_yaml


# A catalog item in the shape cyroid-catalog publishes: some fields stated,
# plenty left to the installer's defaults, and one VM still on the deprecated
# template_name spelling.
CATALOG_YAML = {
    "name": "Red Team Training Lab",
    "description": "A small lab.",
    "seed_id": "red-team-training-lab",
    "networks": [
        {"name": "corp", "subnet": "10.10.0.0/24", "gateway": "10.10.0.1"},
        {"name": "dmz", "subnet": "10.10.1.0/24", "gateway": "10.10.1.1", "internet_enabled": True},
    ],
    "vms": [
        {
            "hostname": "dc-01",
            "base_image_tag": "windows-2019",
            "cpu": 4,
            "ram_mb": 8192,
            "network_name": "corp",
            "ip_address": "10.10.0.10",
        },
        {
            "hostname": "web-01",
            "template_name": "ubuntu-22.04",
            "network_name": "dmz",
            "ip_address": "10.10.1.10",
            "environment": {"APP_ENV": "prod"},
        },
    ],
}


def installed_config(doc=None):
    """The config the platform would hold after installing this catalog item."""
    return build_config_from_yaml(doc or CATALOG_YAML)


def test_freshly_installed_blueprint_has_nothing_to_contribute():
    """The defaults table must match the installer's, or every install looks edited."""
    diff = diff_blueprint_against_catalog(
        CATALOG_YAML,
        installed_config(),
        name=CATALOG_YAML["name"],
        description=CATALOG_YAML["description"],
    )
    assert diff.changes == [], [c.key for c in diff.changes]
    assert not diff


def test_default_valued_field_the_catalog_omitted_is_not_a_change():
    config = installed_config()
    web = next(v for v in config["vms"] if v["hostname"] == "web-01")
    assert web["cpu"] == 1 and web["ram_mb"] == 1024  # installer defaults
    diff = diff_blueprint_against_catalog(CATALOG_YAML, config)
    assert [c.key for c in diff.changes] == []


def test_raising_a_default_valued_field_is_a_change():
    config = installed_config()
    web = next(v for v in config["vms"] if v["hostname"] == "web-01")
    web["ram_mb"] = 4096

    diff = diff_blueprint_against_catalog(CATALOG_YAML, config)
    change = one(diff, "vms/web-01/ram_mb")
    assert change.kind == ADDED  # the catalog never stated it
    assert change.before == 1024 and change.after == 4096

    doc = apply_changes(CATALOG_YAML, diff.changes)
    web_doc = next(v for v in doc["vms"] if v["hostname"] == "web-01")
    assert web_doc["ram_mb"] == 4096


def test_image_change_is_written_to_the_spelling_the_catalog_uses():
    config = installed_config()
    web = next(v for v in config["vms"] if v["hostname"] == "web-01")
    web["base_image_tag"] = "ubuntu-24.04"

    diff = diff_blueprint_against_catalog(CATALOG_YAML, config)
    change = one(diff, "vms/web-01/template_name")
    assert change.before == "ubuntu-22.04" and change.after == "ubuntu-24.04"

    doc = apply_changes(CATALOG_YAML, diff.changes)
    web_doc = next(v for v in doc["vms"] if v["hostname"] == "web-01")
    assert web_doc["template_name"] == "ubuntu-24.04"
    assert "base_image_tag" not in web_doc  # no silent schema migration


def test_template_name_normalisation_is_not_reported_as_a_change():
    """The installer rewrites template_name to base_image_tag; that is not an edit."""
    diff = diff_blueprint_against_catalog(CATALOG_YAML, installed_config())
    assert not [c for c in diff.changes if "template_name" in c.key]
    assert not [c for c in diff.changes if "base_image_tag" in c.key]


def test_added_and_removed_vms():
    config = installed_config()
    config["vms"] = [v for v in config["vms"] if v["hostname"] != "dc-01"]
    config["vms"].append(
        {
            "hostname": "kali",
            "base_image_tag": "kali",
            "cpu": 2,
            "ram_mb": 1024,
            "disk_gb": 20,
            "network_name": "dmz",
            "ip_address": "10.10.1.50",
        }
    )

    diff = diff_blueprint_against_catalog(CATALOG_YAML, config)
    assert one(diff, "vms/dc-01").kind == REMOVED
    added = one(diff, "vms/kali")
    assert added.kind == ADDED
    # Values equal to the installer's defaults are not written back out.
    assert added.after == {
        "hostname": "kali",
        "base_image_tag": "kali",
        "cpu": 2,
        "network_name": "dmz",
        "ip_address": "10.10.1.50",
    }

    doc = apply_changes(CATALOG_YAML, diff.changes)
    assert [v["hostname"] for v in doc["vms"]] == ["web-01", "kali"]


def test_network_field_change():
    config = installed_config()
    corp = next(n for n in config["networks"] if n["name"] == "corp")
    corp["internet_enabled"] = True

    diff = diff_blueprint_against_catalog(CATALOG_YAML, config)
    change = one(diff, "networks/corp/internet_enabled")
    assert change.kind == ADDED and change.after is True

    doc = apply_changes(CATALOG_YAML, diff.changes)
    assert next(n for n in doc["networks"] if n["name"] == "corp")["internet_enabled"]


def test_environment_variables_add_change_and_remove():
    config = installed_config()
    web = next(v for v in config["vms"] if v["hostname"] == "web-01")
    web["environment"] = {"APP_ENV": "staging", "DEBUG": "1"}

    diff = diff_blueprint_against_catalog(CATALOG_YAML, config)
    assert one(diff, "vms/web-01/environment/APP_ENV").kind == CHANGED
    assert one(diff, "vms/web-01/environment/DEBUG").kind == ADDED

    doc = apply_changes(CATALOG_YAML, diff.changes)
    env = next(v for v in doc["vms"] if v["hostname"] == "web-01")["environment"]
    assert env == {"APP_ENV": "staging", "DEBUG": "1"}


def test_removing_every_environment_variable_drops_the_key():
    doc = apply_changes(
        CATALOG_YAML,
        diff_for(dict(CATALOG_YAML), drop_env="web-01").changes,
    )
    web = next(v for v in doc["vms"] if v["hostname"] == "web-01")
    assert "environment" not in web


def test_unknown_catalog_keys_survive_untouched():
    original = yaml.safe_load(yaml.safe_dump(CATALOG_YAML))
    original["requires_images"] = ["custom-web"]
    original["vms"][0]["x_vendor_note"] = "keep me"

    config = installed_config(original)
    config["vms"][0]["cpu"] = 8

    diff = diff_blueprint_against_catalog(original, config)
    doc = apply_changes(original, diff.changes)
    assert doc["requires_images"] == ["custom-web"]
    assert doc["vms"][0]["x_vendor_note"] == "keep me"
    assert doc["seed_id"] == "red-team-training-lab"


def test_only_selected_changes_are_applied():
    config = installed_config()
    next(v for v in config["vms"] if v["hostname"] == "dc-01")["cpu"] = 8
    next(v for v in config["vms"] if v["hostname"] == "web-01")["ram_mb"] = 4096

    diff = diff_blueprint_against_catalog(CATALOG_YAML, config)
    assert len(diff.changes) == 2

    selected = diff.select(["vms/dc-01/cpu"])
    assert len(selected) == 1
    doc = apply_changes(CATALOG_YAML, selected)
    assert next(v for v in doc["vms"] if v["hostname"] == "dc-01")["cpu"] == 8
    assert "ram_mb" not in next(v for v in doc["vms"] if v["hostname"] == "web-01")

    assert diff.select(None) == diff.changes  # None means everything


def test_hostname_containing_a_dot_round_trips():
    original = yaml.safe_load(yaml.safe_dump(CATALOG_YAML))
    original["vms"][0]["hostname"] = "dc-01.corp.local"
    config = installed_config(original)
    config["vms"][0]["cpu"] = 16

    diff = diff_blueprint_against_catalog(original, config)
    change = one(diff, "vms/dc-01.corp.local/cpu")
    assert change.path == ("vms", "dc-01.corp.local", "cpu")

    doc = apply_changes(original, [change])
    assert doc["vms"][0]["cpu"] == 16


def test_msel_and_content_are_reported_as_not_contributable():
    config = installed_config()
    config["msel"] = {"content": "events", "format": "yaml"}
    config["content_ids"] = ["a-uuid"]

    diff = diff_blueprint_against_catalog(CATALOG_YAML, config)
    assert len(diff.notes) == 2
    assert any("msel.md" in n for n in diff.notes)
    assert any("Content Library" in n for n in diff.notes)


def test_patch_applies_cleanly_to_the_catalog_file(tmp_path: Path):
    """The whole point: `git apply` in a catalog clone must accept this."""
    item_path = "blueprints/red-team-training-lab/blueprint.yaml"
    original_text = render_yaml(CATALOG_YAML)

    config = installed_config()
    next(v for v in config["vms"] if v["hostname"] == "dc-01")["cpu"] = 8
    diff = diff_blueprint_against_catalog(CATALOG_YAML, config)

    result = build_patch(
        CATALOG_YAML, diff.changes, item_path=item_path, original_text=original_text
    )
    assert result.applies_to_source
    assert "-  cpu: 4" in result.patch and "+  cpu: 8" in result.patch

    repo = tmp_path / "catalog"
    target = repo / item_path
    target.parent.mkdir(parents=True)
    target.write_text(original_text, encoding="utf-8")
    git(repo, "init", "-q")
    (repo / "patch.diff").write_text(result.patch, encoding="utf-8")
    git(repo, "apply", "patch.diff")

    assert yaml.safe_load(target.read_text())["vms"][0]["cpu"] == 8


def test_a_commented_catalog_file_yields_a_patch_that_says_it_will_not_apply():
    original_text = textwrap.dedent(
        """\
        # Red team lab, maintained by the catalog team.
        name: Red Team Training Lab
        vms:
          - hostname: dc-01
            base_image_tag: windows-2019
        """
    )
    doc = yaml.safe_load(original_text)
    config = build_config_from_yaml(doc)
    config["vms"][0]["cpu"] = 2

    diff = diff_blueprint_against_catalog(doc, config)
    result = build_patch(doc, diff.changes, item_path="b.yaml", original_text=original_text)
    assert not result.applies_to_source
    assert any("git apply" in n for n in result.notes)
    assert yaml.safe_load(result.blueprint_yaml)["vms"][0]["cpu"] == 2


def test_applying_a_change_for_a_vm_the_catalog_lacks_is_refused():
    config = installed_config()
    config["vms"][0]["cpu"] = 8
    diff = diff_blueprint_against_catalog(CATALOG_YAML, config)

    shrunk = {**CATALOG_YAML, "vms": [CATALOG_YAML["vms"][1]]}
    with pytest.raises(ValueError, match="dc-01"):
        apply_changes(shrunk, diff.changes)


def test_nothing_changed_means_an_empty_patch():
    diff = diff_blueprint_against_catalog(CATALOG_YAML, installed_config())
    result = build_patch(CATALOG_YAML, diff.changes, item_path="b.yaml")
    assert result.patch == ""


# --- helpers -------------------------------------------------------------


def one(diff, key):
    """The single change with this key, asserting it is the only one."""
    matches = [c for c in diff.changes if c.key == key]
    assert len(matches) == 1, f"{key} not uniquely present in {[c.key for c in diff.changes]}"
    return matches[0]


def diff_for(original, *, drop_env):
    config = installed_config(original)
    vm = next(v for v in config["vms"] if v["hostname"] == drop_env)
    vm["environment"] = {}
    return diff_blueprint_against_catalog(original, config)


def git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
