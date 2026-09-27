# backend/tests/unit/test_content_bundle_roundtrip.py
"""Packing, transport safety, and the library round trip (PG-104)."""

import asyncio
import gzip
import io
import tarfile
from uuid import uuid4

import pytest
from fastapi import HTTPException

from proving_ground.api.content import (
    export_content_bundle,
    import_content_bundle,
    preview_content_bundle,
)
from proving_ground.content.archive import ArchiveError, pack, unpack
from proving_ground.content.bundle import BundleAsset, read_bundle, sha256_of
from proving_ground.content.importer import CONFLICT, CREATE, UNCHANGED, UPDATE, plan_import
from proving_ground.content.store import apply_bundle, bundle_from_content, find_local
from proving_ground.models.content import Content, ContentAsset, ContentType

USER_ID = uuid4()
FILES = {"content.yaml": b"schema_version: 1\nid: x\ntitle: X\n", "body.md": b"# X\n"}


# --- transport ------------------------------------------------------------


def test_packing_the_same_bundle_twice_is_byte_identical():
    """tar and gzip both want to stamp the current time. Both are told not to."""
    assert pack(FILES, "x") == pack(FILES, "x")


def test_pack_unpack_round_trips_and_strips_the_root():
    assert unpack(pack(FILES, "lab-guide")) == FILES


def test_an_archive_with_an_absolute_path_is_refused():
    with pytest.raises(ArchiveError, match="absolute path"):
        unpack(_tar_with("/etc/passwd", b"nope"))


def test_an_archive_escaping_the_bundle_root_is_refused():
    with pytest.raises(ArchiveError, match="escapes"):
        unpack(_tar_with("bundle/../../../etc/passwd", b"nope"))


def test_an_archive_containing_a_symlink_is_refused():
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        info = tarfile.TarInfo("bundle/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        tar.addfile(info)
    with pytest.raises(ArchiveError, match="not a regular file"):
        unpack(_gz(raw.getvalue()))


def test_an_empty_archive_is_refused():
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w"):
        pass
    with pytest.raises(ArchiveError, match="empty"):
        unpack(_gz(raw.getvalue()))


def test_rubbish_is_refused_rather_than_crashing():
    with pytest.raises(ArchiveError, match="not a readable"):
        unpack(b"this is not a tarball")


# --- the library round trip ----------------------------------------------


@pytest.fixture
def content(db_session):
    row = Content(
        title="Lab Guide",
        description="How to run the lab.",
        content_type=ContentType.STUDENT_GUIDE,
        body_markdown="# Lab Guide\n\nStep one.\n",
        version="1.2",
        tags=["tier-1", "networking"],
        organization="Example Org",
        walkthrough_data={"phases": [{"name": "Recon"}]},
        created_by_id=USER_ID,
    )
    db_session.add(row)
    db_session.commit()
    return row


def test_content_exports_to_a_bundle_and_imports_back_identically(db_session, content):
    exported = bundle_from_content(content)

    # A clean library: importing must recreate it.
    db_session.query(Content).delete()
    db_session.commit()

    local, content_id = find_local(db_session, exported.slug)
    assert local is None and content_id is None

    decision = plan_import(exported, local)
    assert decision.action == CREATE

    created = apply_bundle(db_session, exported, decision, USER_ID)
    db_session.commit()

    assert created.title == "Lab Guide"
    assert created.content_type == ContentType.STUDENT_GUIDE
    assert created.walkthrough_data == {"phases": [{"name": "Recon"}]}
    assert created.tags == ["networking", "tier-1"]
    assert bundle_from_content(created).files() == exported.files()


def test_importing_the_same_bundle_again_writes_nothing(db_session, content):
    exported = bundle_from_content(content)
    local, content_id = find_local(db_session, exported.slug)

    decision = plan_import(exported, local, content_id=content_id)
    assert decision.action == UNCHANGED
    assert apply_bundle(db_session, exported, decision, USER_ID) is None
    assert db_session.query(Content).count() == 1


def test_a_locally_edited_copy_is_a_conflict_and_is_left_alone(db_session, content):
    exported = bundle_from_content(content)
    content.body_markdown = "# Lab Guide\n\nEdited locally.\n"
    db_session.commit()

    local, content_id = find_local(db_session, exported.slug)
    decision = plan_import(exported, local, content_id=content_id)

    assert decision.action == CONFLICT
    assert [c.field for c in decision.conflicts] == ["body_markdown"]
    assert apply_bundle(db_session, exported, decision, USER_ID) is None
    db_session.refresh(content)
    assert content.body_markdown == "# Lab Guide\n\nEdited locally.\n"


def test_overwrite_replaces_the_local_copy_in_place(db_session, content):
    exported = bundle_from_content(content)
    content.body_markdown = "# Lab Guide\n\nEdited locally.\n"
    db_session.commit()
    original_id = content.id

    local, content_id = find_local(db_session, exported.slug)
    decision = plan_import(exported, local, content_id=content_id, overwrite=True)
    assert decision.action == UPDATE

    updated = apply_bundle(db_session, exported, decision, USER_ID)
    db_session.commit()

    assert updated.id == original_id  # updated, not duplicated
    assert updated.body_markdown == "# Lab Guide\n\nStep one.\n"
    assert db_session.query(Content).count() == 1


def test_assets_survive_the_round_trip(db_session, content):
    data = b"\x89PNG fake"
    db_session.add(
        ContentAsset(
            content_id=content.id,
            filename="diagram.png",
            file_path="proving-ground-content/diagram.png",
            mime_type="image/png",
            file_size=len(data),
            sha256_hash=sha256_of(data),
        )
    )
    db_session.commit()
    db_session.refresh(content)

    exported = bundle_from_content(content, load_asset=lambda a: data)
    assert exported.assets[0].data == data

    saved = {}

    def save(content_id, filename, mime_type, payload):
        saved[filename] = payload
        return f"bucket/{filename}"

    db_session.query(ContentAsset).delete()
    db_session.query(Content).delete()
    db_session.commit()

    decision = plan_import(exported, None)
    created = apply_bundle(db_session, exported, decision, USER_ID, save_asset=save)
    db_session.commit()

    assert saved == {"diagram.png": data}
    rows = db_session.query(ContentAsset).filter(ContentAsset.content_id == created.id).all()
    assert [(r.filename, r.sha256_hash) for r in rows] == [("diagram.png", sha256_of(data))]


def test_an_export_without_object_storage_still_lists_its_assets(db_session, content):
    """A missing image must not fail the whole export."""
    db_session.add(
        ContentAsset(
            content_id=content.id,
            filename="diagram.png",
            file_path="proving-ground-content/diagram.png",
            mime_type="image/png",
            file_size=9,
            sha256_hash="abc123",
        )
    )
    db_session.commit()
    db_session.refresh(content)

    exported = bundle_from_content(content, load_asset=lambda a: None)
    assert [a.filename for a in exported.assets] == ["diagram.png"]
    assert exported.assets[0].data is None
    assert "assets/diagram.png" not in exported.files()  # nothing to write
    assert b"diagram.png" in exported.files()["assets.yaml"]


# --- the endpoints --------------------------------------------------------


class Upload:
    """The minimum of UploadFile these endpoints touch."""

    def __init__(self, payload: bytes):
        self._payload = payload

    async def read(self):
        return self._payload


class User:
    id = USER_ID
    username = "pg104"


def test_the_export_endpoint_returns_a_deterministic_archive(db_session, content):
    one = export_content_bundle(content.id, db_session, User())
    two = export_content_bundle(content.id, db_session, User())

    assert one.body == two.body
    assert one.headers["content-disposition"] == 'attachment; filename="lab-guide-bundle.tar.gz"'
    assert read_bundle(unpack(one.body)).title == "Lab Guide"


def test_the_export_endpoint_404s_for_unknown_content(db_session):
    with pytest.raises(HTTPException) as exc:
        export_content_bundle(uuid4(), db_session, User())
    assert exc.value.status_code == 404


def test_preview_reports_without_writing(db_session, content):
    archive = export_content_bundle(content.id, db_session, User()).body
    db_session.query(Content).delete()
    db_session.commit()

    decision = asyncio.run(preview_content_bundle(db_session, User(), Upload(archive)))
    assert decision.action == CREATE and decision.writes
    assert db_session.query(Content).count() == 0  # preview wrote nothing


def test_import_then_reimport_is_idempotent_through_the_endpoint(db_session, content):
    archive = export_content_bundle(content.id, db_session, User()).body
    db_session.query(Content).delete()
    db_session.commit()

    first = asyncio.run(import_content_bundle(db_session, User(), Upload(archive)))
    assert first.imported and first.decision.action == CREATE
    assert db_session.query(Content).count() == 1

    second = asyncio.run(import_content_bundle(db_session, User(), Upload(archive)))
    assert not second.imported
    assert second.decision.action == UNCHANGED
    assert db_session.query(Content).count() == 1


def test_import_reports_a_conflict_rather_than_clobbering(db_session, content):
    archive = export_content_bundle(content.id, db_session, User()).body
    content.description = "Locally rewritten."
    db_session.commit()

    result = asyncio.run(import_content_bundle(db_session, User(), Upload(archive)))
    assert not result.imported
    assert result.decision.action == CONFLICT
    assert result.decision.conflicts[0].description == (
        "description: 'Locally rewritten.' -> 'How to run the lab.'"
    )
    db_session.refresh(content)
    assert content.description == "Locally rewritten."


def test_import_with_overwrite_applies_it(db_session, content):
    archive = export_content_bundle(content.id, db_session, User()).body
    content.description = "Locally rewritten."
    db_session.commit()

    result = asyncio.run(import_content_bundle(db_session, User(), Upload(archive), overwrite=True))
    assert result.imported and result.decision.action == UPDATE
    db_session.refresh(content)
    assert content.description == "How to run the lab."


def test_a_corrupt_upload_is_a_400_not_a_500(db_session):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(preview_content_bundle(db_session, User(), Upload(b"not a tarball")))
    assert exc.value.status_code == 400


def _tar_with(name: str, payload: bytes) -> bytes:
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as tar:
        info = tarfile.TarInfo(name)
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    return _gz(raw.getvalue())


def _gz(payload: bytes) -> bytes:
    out = io.BytesIO()
    with gzip.GzipFile(fileobj=out, mode="wb", mtime=0) as gz:
        gz.write(payload)
    return out.getvalue()
