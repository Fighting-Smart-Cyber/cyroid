"""The digest-pinning gate must actually fail an unpinned reference.

The check this replaces could not have failed anything. It extracted matches with `grep -o` using a
pattern that stopped at the tag, then looked for `@sha256` in the extracted text -- so a correctly
pinned `image: postgres:16@sha256:...` came back as `image: postgres:16`, counted as unpinned. The
ratchet's baseline was counting pinned and unpinned references alike, and pinning something could not
lower it.

That is the failure mode worth a test: a gate whose number does not mean what it says. Each case below
is a reference shape the repository actually contains, checked against a tree built for the purpose
rather than against whatever happens to be in the working copy.
"""

import importlib.util
import pathlib

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "check-image-pinning.py"

DIGEST = "sha256:" + "a" * 64


def _checker():
    spec = importlib.util.spec_from_file_location("image_pinning", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _tree(tmp_path: pathlib.Path, **files: str) -> pathlib.Path:
    for name, body in files.items():
        p = tmp_path / name.replace("__", "/")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return tmp_path


def _run(root: pathlib.Path) -> int:
    return _checker().main(["--root", str(root)])


def test_an_unpinned_reference_fails(tmp_path):
    root = _tree(tmp_path, **{"docker-compose.yml": "services:\n  db:\n    image: postgres:16\n"})
    assert _run(root) == 1


def test_a_pinned_reference_passes(tmp_path):
    """The case the old check got wrong: pinned, and it must not be counted as unpinned."""
    root = _tree(
        tmp_path, **{"docker-compose.yml": f"services:\n  db:\n    image: postgres:16@{DIGEST}\n"}
    )
    assert _run(root) == 0


def test_a_pinned_dockerfile_from_passes(tmp_path):
    root = _tree(tmp_path, Dockerfile=f"FROM node:20-alpine@{DIGEST} AS builder\nRUN true\n")
    assert _run(root) == 0


def test_an_unpinned_dockerfile_from_fails(tmp_path):
    root = _tree(tmp_path, Dockerfile="FROM node:20-alpine AS builder\nRUN true\n")
    assert _run(root) == 1


def test_an_image_this_repository_builds_is_exempt(tmp_path):
    """pg-* images exist in no registry, so there is no digest they could carry."""
    root = _tree(
        tmp_path, **{"docker-compose.dev.yml": "services:\n  api:\n    image: pg-api:latest\n"}
    )
    assert _run(root) == 0


def test_a_similarly_named_registry_image_is_not_exempt(tmp_path):
    """`pg-api` is exempt; `someone-else/pg-api` is a pull and is not."""
    root = _tree(
        tmp_path,
        **{"docker-compose.yml": "services:\n  api:\n    image: ghcr.io/other/pg-api:latest\n"},
    )
    assert _run(root) == 1


def test_a_variable_tag_is_exempt(tmp_path):
    root = _tree(
        tmp_path,
        **{
            "docker-compose.prod.yml": "services:\n  api:\n    image: ghcr.io/x/y:${VERSION:-latest}\n"
        },
    )
    assert _run(root) == 0


def test_a_variable_base_image_in_a_dockerfile_is_exempt(tmp_path):
    """backend/Dockerfile's ARG-driven base, which the Iron Bank manifests set."""
    root = _tree(
        tmp_path, Dockerfile="ARG BASE_IMAGE=x\nARG BASE_TAG=y\nFROM ${BASE_IMAGE}:${BASE_TAG}\n"
    )
    assert _run(root) == 0


def test_scratch_is_not_an_image_reference(tmp_path):
    root = _tree(tmp_path, Dockerfile="FROM scratch\nCOPY x /x\n")
    assert _run(root) == 0


def test_the_repository_itself_passes():
    """Enforcement, against the real tree. This is the assertion that makes it a gate."""
    assert _run(REPO_ROOT) == 0, (
        "An image reference in this repository names a tag and no digest. Run "
        "`python3 scripts/check-image-pinning.py` for the list."
    )
