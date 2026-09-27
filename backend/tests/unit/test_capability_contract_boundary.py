# backend/tests/unit/test_capability_contract_boundary.py
"""The capability contract is the licence boundary, so the build enforces it (PG-28).

Under ADR-0011 the engine is public and AGPL-3.0 while a distribution's
capability packages belong to whoever wrote them. That separation only holds
if a package can implement the contract without reaching into the engine: the
moment `proving_ground.capability` pulls in `proving_ground.models` or
`proving_ground.services`, a package built against the contract transitively
depends on platform internals, and the line between "implements a published
interface" and "derives from the engine" stops being clear.

Today that is true — `proving_ground/capability/` imports nothing else from
`proving_ground` — but it is true by good design rather than by enforcement,
and nothing would have noticed it changing. That is what these tests are for.
They are about a licence boundary, so they fail loudly and explain why.
"""

import ast
import pathlib
import subprocess
import sys

import pytest

import proving_ground.capability as contract
from proving_ground.capability import (
    CapabilityRuntime,
    CapabilitySpec,
    ChartRef,
    HookSpec,
    Scope,
)

PACKAGE = pathlib.Path(contract.__file__).parent
ENGINE_ROOT = "proving_ground"

# ADR-0004: a trainable capability is a package plus seed, reset and verify,
# with exec as the explicit escape hatch. Changing this set is a contract
# change, which is a decision, not a refactor.
CONTRACT_METHODS = {"install", "seed", "reset", "verify", "exec", "uninstall"}

# Digest-pinned, because ImageRef refuses anything else (ADR-0007).
PROBE_IMAGE = "example.invalid/probe@sha256:" + "0" * 64

# The names a capability package is entitled to depend on.
CONTRACT_EXPORTS = {
    "CapabilityRuntime",
    "CapabilitySpec",
    "ChartRef",
    "Delivery",
    "ExecResult",
    "HookResult",
    "HookSpec",
    "ImageRef",
    "InstallResult",
    "Isolation",
    "Placement",
    "PlacementRequest",
    "Scope",
    "VerifyResult",
    "placement_for_assignment",
    "resolve_placement",
}


def imported_modules(path: pathlib.Path):
    """Every module name this file imports, including inside functions."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:  # relative: within the package by construction
                continue
            if node.module:
                out.add(node.module)
        elif isinstance(node, ast.Import):
            out.update(alias.name for alias in node.names)
    return out


def test_the_contract_imports_nothing_from_the_rest_of_the_engine():
    """A package implementing the contract must not inherit platform internals."""
    offenders = {}
    for path in sorted(PACKAGE.glob("*.py")):
        leaked = {
            module
            for module in imported_modules(path)
            if module == ENGINE_ROOT or module.startswith(f"{ENGINE_ROOT}.")
            if not module.startswith(f"{ENGINE_ROOT}.capability")
        }
        if leaked:
            offenders[path.name] = sorted(leaked)

    assert not offenders, (
        "The capability contract now imports engine internals:\n  "
        + "\n  ".join(f"{name} -> {mods}" for name, mods in offenders.items())
        + "\n\nADR-0011 makes this package the licence boundary: a capability "
        "package depends on the contract and nothing else under proving_ground. "
        "If the contract needs something from the engine, the thing it needs "
        "belongs in the contract, not an import out of it."
    )


def test_importing_the_contract_does_not_drag_in_the_platform():
    """The static check misses a lazy import inside a function; this does not."""
    probe = (
        "import sys, json;"
        "import proving_ground.capability;"
        "print(json.dumps(sorted("
        "  m for m in sys.modules"
        "  if m.startswith('proving_ground.') "
        "  and not m.startswith('proving_ground.capability')"
        ")))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=str(pathlib.Path(__file__).resolve().parents[2]),
    )
    assert result.returncode == 0, result.stderr
    loaded = [m for m in __import__("json").loads(result.stdout) if m != "proving_ground"]
    assert not loaded, (
        "Importing the capability contract loaded engine modules:\n  "
        + "\n  ".join(loaded)
        + "\nA capability package that imports the contract now pulls these in too."
    )


def test_the_contract_does_not_import_the_docker_sdk():
    """Era A must not reach the substrate's public interface."""
    for path in sorted(PACKAGE.glob("*.py")):
        assert "docker" not in {m.split(".")[0] for m in imported_modules(path)}, (
            f"{path.name} imports the Docker SDK. CLAUDE.md rule 1, and a "
            "capability package must never need it."
        )


def test_the_published_surface_is_exactly_the_contract():
    """__all__ is the promise; it should not drift silently in either direction."""
    published = set(contract.__all__)
    assert published == CONTRACT_EXPORTS, (
        "The published contract changed.\n"
        f"  added:   {sorted(published - CONTRACT_EXPORTS) or '-'}\n"
        f"  removed: {sorted(CONTRACT_EXPORTS - published) or '-'}\n"
        "Adding a name widens what a capability package may depend on; removing "
        "one breaks packages already written against it. Either is a decision."
    )
    unreachable = [name for name in published if not hasattr(contract, name)]
    assert not unreachable, f"__all__ names nothing importable: {unreachable}"


def test_the_runtime_offers_exactly_the_hooks_adr_0004_specifies():
    declared = {
        name
        for name in vars(CapabilityRuntime)
        if not name.startswith("_") and callable(getattr(CapabilityRuntime, name, None))
    }
    assert declared == CONTRACT_METHODS, (
        f"CapabilityRuntime's surface changed: added {sorted(declared - CONTRACT_METHODS)}, "
        f"removed {sorted(CONTRACT_METHODS - declared)}. ADR-0004 names these six; "
        "a capability package is written against them."
    )
    assert CapabilityRuntime.__abstractmethods__ == frozenset(CONTRACT_METHODS), (
        "Every contract method must stay abstract, or a runtime can silently "
        "inherit a do-nothing implementation of seed, reset or verify."
    )


def test_scope_is_required_on_every_capability():
    """CLAUDE.md rule 8: per-learner, per-cohort or shared, with no default."""
    import dataclasses

    scope_field = next(f for f in dataclasses.fields(CapabilitySpec) if f.name == "scope")
    assert scope_field.default is dataclasses.MISSING
    assert scope_field.default_factory is dataclasses.MISSING

    with pytest.raises(ValueError, match="no default"):
        CapabilitySpec(
            name="x",
            version="1.0.0",
            chart=ChartRef(name="c", version="1.0.0", repository="https://example.invalid"),
            scope=None,
            verify=HookSpec(image=PROBE_IMAGE, command=("true",)),
        )


def test_scope_values_are_the_three_the_contract_names():
    assert {s.value for s in Scope} == {"per-learner", "per-cohort", "shared"}
