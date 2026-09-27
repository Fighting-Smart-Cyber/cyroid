"""Contribute a locally-modified blueprint back to the catalog it came from.

A blueprint installed from a catalog is translated on the way in by
``catalog_service.build_config_from_yaml``: catalog YAML becomes the flat
``RangeBlueprint.config`` the platform runs on. That translation is lossy in
both directions, so this module does **not** invert it.

Instead it computes the *difference* between the installed config and the
catalog's own ``blueprint.yaml``, then applies the changes the user selects
back onto the original document. Everything the translation never modelled --
comments are lost to the YAML round-trip, but unknown keys, ordering and the
catalog's own schema choices are not -- survives untouched, which is what makes
the result reviewable as a patch rather than a rewrite.

Two asymmetries drive most of the code here:

* ``build_config_from_yaml`` fills in defaults (``cpu: 1``, ``ram_mb: 1024``,
  ``is_isolated: false`` ...). A naive comparison reports every one of them as
  a change on a catalog item that simply omitted the key. A config value equal
  to the default is therefore only a change when the original *stated* a
  different value.
* The catalog may address a VM's image with the deprecated ``template_name``
  rather than ``base_image_tag``. Both mean the same thing, so the value is
  compared once and written back to whichever key the original used. Migrating
  the catalog's schema is a bigger change than the user asked to contribute.

Paths are tuples, not dotted strings: a hostname may legitimately contain a
dot, and an escaping scheme is a bug waiting to happen.
"""

from __future__ import annotations

import copy
import difflib
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

import yaml

Path = Tuple[str, ...]

CHANGED = "changed"
ADDED = "added"
REMOVED = "removed"

# Mirrors the defaults applied by catalog_service.build_config_from_yaml.
# Keep the two in step: a default that drifts here reports phantom changes.
NETWORK_DEFAULTS: Dict[str, Any] = {
    "is_isolated": False,
    "internet_enabled": False,
    "dhcp_enabled": False,
}
VM_DEFAULTS: Dict[str, Any] = {
    "cpu": 1,
    "ram_mb": 1024,
    "disk_gb": 20,
}

NETWORK_FIELDS: Tuple[str, ...] = (
    "subnet",
    "gateway",
    "is_isolated",
    "internet_enabled",
    "dhcp_enabled",
)
# Scalar VM fields compared straight across. The image tag, environment and
# network interfaces need their own handling and are deliberately absent.
VM_SCALAR_FIELDS: Tuple[str, ...] = (
    "cpu",
    "ram_mb",
    "disk_gb",
    "windows_version",
    "arch",
    "position_x",
    "position_y",
)
VM_LEGACY_NIC_FIELDS: Tuple[str, ...] = ("ip_address", "network_name")

METADATA_FIELDS: Tuple[str, ...] = ("name", "description", "base_subnet_prefix")


@dataclass(frozen=True)
class FieldChange:
    """One reviewable difference between the catalog item and the local copy."""

    path: Path
    kind: str
    before: Any
    after: Any
    label: str

    @property
    def key(self) -> str:
        """Stable identifier a client can send back to select this change."""
        return "/".join(self.path)


@dataclass
class ContributionDiff:
    """The full difference, plus what could not be expressed as one."""

    changes: List[FieldChange] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.changes)

    def select(self, keys: Optional[Sequence[str]]) -> List[FieldChange]:
        """Return the subset the caller asked for; ``None`` means all of it."""
        if keys is None:
            return list(self.changes)
        wanted = set(keys)
        return [c for c in self.changes if c.key in wanted]


def _by_name(items: Any, key: str) -> Dict[str, dict]:
    """Index a list of dicts by one of their fields, skipping unusable rows."""
    out: Dict[str, dict] = {}
    for entry in items or []:
        if not isinstance(entry, dict):
            continue
        name = entry.get(key)
        if isinstance(name, str) and name:
            out[name] = entry
    return out


def _effective(original: dict, key: str, default: Any = None) -> Tuple[Any, bool]:
    """The original's value for ``key``, and whether it stated one at all."""
    if key in original:
        return original[key], True
    return default, False


def _image_key(original_vm: dict) -> str:
    """Whichever key this catalog item uses to name a VM's image."""
    if "base_image_tag" in original_vm:
        return "base_image_tag"
    if "template_name" in original_vm:
        return "template_name"
    return "base_image_tag"


def _drop_defaults(vm: dict) -> dict:
    """Render a VM in catalog shape: no nulls, no values equal to the default."""
    out: Dict[str, Any] = {}
    for key, value in vm.items():
        if value is None:
            continue
        if key in VM_DEFAULTS and value == VM_DEFAULTS[key]:
            continue
        out[key] = value
    return out


def _drop_network_defaults(network: dict) -> dict:
    out: Dict[str, Any] = {}
    for key, value in network.items():
        if value is None:
            continue
        if key in NETWORK_DEFAULTS and value == NETWORK_DEFAULTS[key]:
            continue
        out[key] = value
    return out


def diff_blueprint_against_catalog(
    original: dict,
    config: dict,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
) -> ContributionDiff:
    """Compare a local blueprint against the catalog document it was installed from.

    Args:
        original: the catalog item's parsed ``blueprint.yaml``.
        config: the local ``RangeBlueprint.config``.
        name: the local blueprint's name, compared against the document's.
        description: the local blueprint's description.

    Returns:
        Every difference that can be written back to ``blueprint.yaml``, plus
        notes for the ones that cannot.
    """
    diff = ContributionDiff()
    local_meta = {"name": name, "description": description}

    for meta_field in METADATA_FIELDS:
        local = local_meta.get(meta_field) if meta_field in local_meta else config.get(meta_field)
        if local is None:
            continue
        before, stated = _effective(original, meta_field)
        if before != local:
            diff.changes.append(
                FieldChange(
                    path=(meta_field,),
                    kind=CHANGED if stated else ADDED,
                    before=before,
                    after=local,
                    label=meta_field.replace("_", " "),
                )
            )

    _diff_networks(original, config, diff)
    _diff_vms(original, config, diff)

    if config.get("msel"):
        diff.notes.append(
            "MSEL and walkthrough content lives in the catalog's msel.md and "
            "content.json, not blueprint.yaml; changes to it are not included."
        )
    if config.get("content_ids"):
        diff.notes.append(
            "Linked Content Library items are not part of blueprint.yaml and " "are not included."
        )
    return diff


def _diff_networks(original: dict, config: dict, diff: ContributionDiff) -> None:
    originals = _by_name(original.get("networks"), "name")
    locals_ = _by_name(config.get("networks"), "name")

    for net_name, local in locals_.items():
        if net_name not in originals:
            diff.changes.append(
                FieldChange(
                    path=("networks", net_name),
                    kind=ADDED,
                    before=None,
                    after=_drop_network_defaults(local),
                    label=f"network '{net_name}' added",
                )
            )
            continue
        orig = originals[net_name]
        for net_field in NETWORK_FIELDS:
            if net_field not in local:
                continue
            after = local[net_field]
            before, stated = _effective(orig, net_field, NETWORK_DEFAULTS.get(net_field))
            if before == after:
                continue
            diff.changes.append(
                FieldChange(
                    path=("networks", net_name, net_field),
                    kind=CHANGED if stated else ADDED,
                    before=before,
                    after=after,
                    label=f"network '{net_name}' {net_field.replace('_', ' ')}",
                )
            )

    for net_name, orig in originals.items():
        if net_name not in locals_:
            diff.changes.append(
                FieldChange(
                    path=("networks", net_name),
                    kind=REMOVED,
                    before=orig,
                    after=None,
                    label=f"network '{net_name}' removed",
                )
            )


def _diff_vms(original: dict, config: dict, diff: ContributionDiff) -> None:
    originals = _by_name(original.get("vms"), "hostname")
    locals_ = _by_name(config.get("vms"), "hostname")

    for hostname, local in locals_.items():
        if hostname not in originals:
            diff.changes.append(
                FieldChange(
                    path=("vms", hostname),
                    kind=ADDED,
                    before=None,
                    after=_drop_defaults(local),
                    label=f"VM '{hostname}' added",
                )
            )
            continue
        _diff_one_vm(hostname, originals[hostname], local, diff)

    for hostname, orig in originals.items():
        if hostname not in locals_:
            diff.changes.append(
                FieldChange(
                    path=("vms", hostname),
                    kind=REMOVED,
                    before=orig,
                    after=None,
                    label=f"VM '{hostname}' removed",
                )
            )


def _diff_one_vm(hostname: str, orig: dict, local: dict, diff: ContributionDiff) -> None:
    for vm_field in VM_SCALAR_FIELDS:
        if vm_field not in local:
            continue
        after = local[vm_field]
        before, stated = _effective(orig, vm_field, VM_DEFAULTS.get(vm_field))
        if before == after:
            continue
        # A position the catalog never recorded is layout noise, not a change
        # worth contributing.
        if not stated and after is None:
            continue
        diff.changes.append(
            FieldChange(
                path=("vms", hostname, vm_field),
                kind=CHANGED if stated else ADDED,
                before=before,
                after=after,
                label=f"VM '{hostname}' {vm_field.replace('_', ' ')}",
            )
        )

    # The image tag: one value, two possible spellings in the original.
    local_image = local.get("base_image_tag")
    if local_image is not None:
        write_key = _image_key(orig)
        before = orig.get("base_image_tag") or orig.get("template_name")
        if before != local_image:
            diff.changes.append(
                FieldChange(
                    path=("vms", hostname, write_key),
                    kind=CHANGED if before is not None else ADDED,
                    before=before,
                    after=local_image,
                    label=f"VM '{hostname}' image",
                )
            )

    _diff_vm_environment(hostname, orig, local, diff)
    _diff_vm_interfaces(hostname, orig, local, diff)


def _diff_vm_environment(hostname: str, orig: dict, local: dict, diff: ContributionDiff) -> None:
    orig_env = orig.get("environment") or {}
    local_env = local.get("environment") or {}
    if not isinstance(orig_env, dict) or not isinstance(local_env, dict):
        return
    for key in sorted(set(orig_env) | set(local_env)):
        before = orig_env.get(key)
        after = local_env.get(key)
        if before == after:
            continue
        if key not in local_env:
            kind, after = REMOVED, None
        elif key not in orig_env:
            kind, before = ADDED, None
        else:
            kind = CHANGED
        diff.changes.append(
            FieldChange(
                path=("vms", hostname, "environment", key),
                kind=kind,
                before=before,
                after=after,
                label=f"VM '{hostname}' environment {key}",
            )
        )


def _diff_vm_interfaces(hostname: str, orig: dict, local: dict, diff: ContributionDiff) -> None:
    """Diff the NIC shape, which may flip between legacy single and multi-NIC."""
    local_nics = local.get("network_interfaces")
    orig_nics = orig.get("network_interfaces")

    if local_nics is not None:
        if orig_nics != local_nics:
            diff.changes.append(
                FieldChange(
                    path=("vms", hostname, "network_interfaces"),
                    kind=CHANGED if orig_nics is not None else ADDED,
                    before=orig_nics,
                    after=local_nics,
                    label=f"VM '{hostname}' network interfaces",
                )
            )
        # Moving to multi-NIC leaves the legacy keys behind; drop them.
        if orig_nics is None:
            for legacy in VM_LEGACY_NIC_FIELDS:
                if legacy in orig:
                    diff.changes.append(
                        FieldChange(
                            path=("vms", hostname, legacy),
                            kind=REMOVED,
                            before=orig[legacy],
                            after=None,
                            label=f"VM '{hostname}' {legacy.replace('_', ' ')} removed",
                        )
                    )
        return

    for legacy in VM_LEGACY_NIC_FIELDS:
        if legacy not in local:
            continue
        after = local[legacy]
        before, stated = _effective(orig, legacy)
        if before == after or (not stated and after is None):
            continue
        diff.changes.append(
            FieldChange(
                path=("vms", hostname, legacy),
                kind=CHANGED if stated else ADDED,
                before=before,
                after=after,
                label=f"VM '{hostname}' {legacy.replace('_', ' ')}",
            )
        )


def _find(items: Any, key: str, name: str) -> Optional[dict]:
    for entry in items or []:
        if isinstance(entry, dict) and entry.get(key) == name:
            return entry
    return None


def _apply_to_collection(doc: dict, plural: str, key: str, change: FieldChange) -> None:
    """Add or remove a whole network/VM entry."""
    name = change.path[1]
    entries = doc.setdefault(plural, [])
    if not isinstance(entries, list):
        raise ValueError(f"blueprint.yaml '{plural}' is not a list")
    if change.kind == REMOVED:
        doc[plural] = [e for e in entries if not (isinstance(e, dict) and e.get(key) == name)]
        return
    existing = _find(entries, key, name)
    payload = copy.deepcopy(change.after) or {}
    payload.setdefault(key, name)
    if existing is None:
        entries.append(payload)
    else:
        existing.update(payload)


def apply_changes(original: dict, changes: Sequence[FieldChange]) -> dict:
    """Apply selected changes onto the catalog document, leaving the rest alone.

    The result is the original document with only the selected edits made --
    unknown keys, key order and the catalog's own schema choices are preserved,
    which is what keeps the resulting patch small enough to review.

    Raises:
        ValueError: if a change targets an entry the document does not contain,
            which means the diff and the document have drifted apart.
    """
    doc = copy.deepcopy(original)

    for change in changes:
        path = change.path

        if len(path) == 1:
            if change.kind == REMOVED:
                doc.pop(path[0], None)
            else:
                doc[path[0]] = copy.deepcopy(change.after)
            continue

        plural = path[0]
        if plural not in ("networks", "vms"):
            raise ValueError(f"unsupported change path {change.key!r}")
        ident = "name" if plural == "networks" else "hostname"

        if len(path) == 2:
            _apply_to_collection(doc, plural, ident, change)
            continue

        target = _find(doc.get(plural), ident, path[1])
        if target is None:
            raise ValueError(f"{change.key!r} targets {path[1]!r}, which is not in blueprint.yaml")

        if len(path) == 3:
            if change.kind == REMOVED:
                target.pop(path[2], None)
            else:
                target[path[2]] = copy.deepcopy(change.after)
            continue

        if len(path) == 4 and path[2] == "environment":
            env = target.setdefault("environment", {})
            if not isinstance(env, dict):
                raise ValueError(f"VM {path[1]!r} environment is not a mapping")
            if change.kind == REMOVED:
                env.pop(path[3], None)
                if not env:
                    target.pop("environment", None)
            else:
                env[path[3]] = copy.deepcopy(change.after)
            continue

        raise ValueError(f"unsupported change path {change.key!r}")

    return doc


def render_yaml(doc: dict) -> str:
    """Render a blueprint document the way catalogs write it."""
    return yaml.safe_dump(
        doc,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
    )


def unified_patch(before: str, after: str, *, item_path: str) -> str:
    """A unified diff between two renderings of blueprint.yaml."""
    if before == after:
        return ""
    lines = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{item_path}",
        tofile=f"b/{item_path}",
    )
    patch = "".join(lines)
    if patch and not patch.endswith("\n"):
        patch += "\n"
    return patch


@dataclass
class ContributionPatch:
    """A reviewable contribution: the patch, the whole file, and the caveats."""

    patch: str
    blueprint_yaml: str
    applied: List[FieldChange]
    notes: List[str] = field(default_factory=list)
    applies_to_source: bool = True


def build_patch(
    original_doc: dict,
    changes: Sequence[FieldChange],
    *,
    item_path: str,
    original_text: Optional[str] = None,
    notes: Optional[Sequence[str]] = None,
) -> ContributionPatch:
    """Render selected changes as a patch against the catalog's blueprint.yaml.

    The patch is taken against the catalog file's own text when re-rendering
    the parsed document reproduces it byte for byte. When it does not -- the
    file carries comments, or a different quoting or indentation style -- the
    patch is taken against the normalised rendering instead, and
    ``applies_to_source`` is False: it still describes the change exactly, but
    ``git apply`` against the untouched catalog file will reject it, so
    ``blueprint_yaml`` is the thing to use.
    """
    modified = apply_changes(original_doc, changes)
    after_text = render_yaml(modified)
    normalised = render_yaml(original_doc)

    applies_to_source = original_text is None or original_text == normalised
    before_text = original_text if applies_to_source and original_text else normalised

    collected = list(notes or [])
    if not applies_to_source:
        collected.append(
            "The catalog file's formatting (comments, quoting or key order) is "
            "not reproduced by a YAML round-trip, so this patch is taken "
            "against a normalised rendering and will not apply cleanly with "
            "`git apply`. Use the full blueprint.yaml below instead."
        )

    return ContributionPatch(
        patch=unified_patch(before_text, after_text, item_path=item_path),
        blueprint_yaml=after_text,
        applied=list(changes),
        notes=collected,
        applies_to_source=applies_to_source,
    )
