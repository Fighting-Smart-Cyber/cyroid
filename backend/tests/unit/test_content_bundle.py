"""The git-native content bundle: diffable, reviewable, idempotent (PG-104)."""

import pytest
import yaml

from proving_ground.content.bundle import (
    ASSET_MANIFEST,
    BODY_FILE,
    CONTENT_FILE,
    SCHEMA_VERSION,
    WALKTHROUGH_FILE,
    BundleAsset,
    BundleError,
    ContentBundle,
    read_bundle,
    sha256_of,
    slug_for,
)
from proving_ground.content.importer import (
    CONFLICT,
    CREATE,
    UNCHANGED,
    UPDATE,
    plan_import,
)

BODY = "# Lab Guide\n\nStep one: log in.\n"


def a_bundle(**overrides) -> ContentBundle:
    defaults = dict(
        slug="lab-guide",
        title="Lab Guide",
        description="How to run the lab.",
        content_type="student_guide",
        version="1.2",
        organization="Example Org",
        tags=["networking", "tier-1"],
        body_markdown=BODY,
    )
    defaults.update(overrides)
    return ContentBundle(**defaults)


# --- AC 1: a diffable, reviewable bundle ---------------------------------


def test_exporting_the_same_content_twice_is_byte_identical():
    """The old JSON export stamped exported_at, so git saw a change every time."""
    assert a_bundle().files() == a_bundle().files()
    assert a_bundle().fingerprint() == a_bundle().fingerprint()

    rendered = a_bundle().files()[CONTENT_FILE].decode()
    assert "exported_at" not in rendered
    assert "created_at" not in rendered and "updated_at" not in rendered


def test_markdown_is_stored_as_markdown_not_embedded_in_json():
    """A one-word edit should be a one-line diff, not a rewritten JSON string."""
    body = a_bundle().files()[BODY_FILE].decode()
    assert body == BODY
    assert body.startswith("# Lab Guide")
    assert "\\n" not in body


def test_metadata_key_order_is_fixed_so_diffs_never_reorder():
    rendered = a_bundle().files()[CONTENT_FILE].decode()
    keys = [line.split(":")[0] for line in rendered.splitlines() if not line.startswith(" ")]
    assert keys[:5] == ["schema_version", "id", "title", "description", "content_type"]


def test_reordering_tags_is_not_a_change():
    """Tags come out of a JSON column in arbitrary order."""
    one = a_bundle(tags=["tier-1", "networking"])
    two = a_bundle(tags=["networking", "tier-1"])
    assert one.files() == two.files()


def test_line_endings_are_normalised_and_a_final_newline_guaranteed():
    crlf = a_bundle(body_markdown="line one\r\nline two")
    lf = a_bundle(body_markdown="line one\nline two\n")
    assert crlf.files()[BODY_FILE] == lf.files()[BODY_FILE]
    assert crlf.files()[BODY_FILE].endswith(b"\n")


def test_an_empty_body_produces_an_empty_file_not_a_stray_newline():
    assert a_bundle(body_markdown="").files()[BODY_FILE] == b""


def test_walkthrough_is_a_separate_reviewable_file():
    walkthrough = {"phases": [{"name": "Recon", "steps": []}]}
    files = a_bundle(walkthrough=walkthrough).files()
    assert yaml.safe_load(files[WALKTHROUGH_FILE]) == walkthrough

    assert WALKTHROUGH_FILE not in a_bundle().files()


def test_assets_are_listed_with_hashes_and_carried_verbatim():
    data = b"\x89PNG\r\n\x1a\nnot really a png"
    bundle = a_bundle(
        assets=[
            BundleAsset("diagram.png", "image/png", len(data), sha256_of(data), data),
        ]
    )
    files = bundle.files()

    assert files["assets/diagram.png"] == data
    manifest = yaml.safe_load(files[ASSET_MANIFEST])
    assert manifest["assets"] == [
        {
            "filename": "diagram.png",
            "mime_type": "image/png",
            "size": len(data),
            "sha256": sha256_of(data),
        }
    ]


def test_changing_an_asset_changes_the_bundle_fingerprint():
    """A changed image must not look like an unchanged bundle."""
    first = b"one"
    second = b"two"
    one = a_bundle(
        assets=[BundleAsset("a.bin", "application/octet-stream", 3, sha256_of(first), first)]
    )
    two = a_bundle(
        assets=[BundleAsset("a.bin", "application/octet-stream", 3, sha256_of(second), second)]
    )
    assert one.fingerprint() != two.fingerprint()


# --- reading a bundle back -----------------------------------------------


def test_a_bundle_round_trips():
    original = a_bundle(
        walkthrough={"phases": [{"name": "Recon"}]},
        assets=[BundleAsset("a.txt", "text/plain", 2, sha256_of(b"hi"), b"hi")],
    )
    parsed = read_bundle(original.files())

    assert parsed.title == original.title
    assert parsed.tags == sorted(original.tags)
    assert parsed.body_markdown == original.body_markdown
    assert parsed.walkthrough == original.walkthrough
    assert [a.filename for a in parsed.assets] == ["a.txt"]
    assert parsed.assets[0].data == b"hi"
    assert parsed.files() == original.files()


def test_a_bundle_without_content_yaml_is_refused():
    with pytest.raises(BundleError, match=CONTENT_FILE):
        read_bundle({BODY_FILE: b"# orphan\n"})


def test_a_bundle_without_a_title_is_refused():
    with pytest.raises(BundleError, match="no title"):
        read_bundle({CONTENT_FILE: b"schema_version: 1\nid: x\n"})


def test_a_newer_schema_version_is_refused_rather_than_guessed_at():
    files = a_bundle().files()
    meta = yaml.safe_load(files[CONTENT_FILE])
    meta["schema_version"] = SCHEMA_VERSION + 1
    files[CONTENT_FILE] = yaml.safe_dump(meta).encode()

    with pytest.raises(BundleError, match="schema version"):
        read_bundle(files)


def test_an_asset_that_does_not_match_its_hash_is_refused():
    """Otherwise a truncated transfer imports content nobody reviewed."""
    data = b"the real bytes"
    files = a_bundle(
        assets=[BundleAsset("a.bin", "application/octet-stream", len(data), sha256_of(data), data)]
    ).files()
    files["assets/a.bin"] = b"tampered"

    with pytest.raises(BundleError, match="sha256"):
        read_bundle(files)


def test_slugs_are_stable_and_filesystem_safe():
    assert slug_for("Lab Guide") == "lab-guide"
    assert slug_for("  LOGC2: Intro / Part 1  ") == "logc2-intro-part-1"
    assert slug_for("") == "untitled"
    assert slug_for("!!!") == "untitled"


# --- AC 2: idempotent import that reports conflicts -----------------------


def test_importing_something_new_creates_it():
    decision = plan_import(a_bundle(), None)
    assert decision.action == CREATE
    assert decision.writes


def test_importing_the_same_bundle_again_does_nothing():
    """Idempotency: GitOps re-runs the same import and must converge."""
    decision = plan_import(a_bundle(), a_bundle())
    assert decision.action == UNCHANGED
    assert not decision.writes
    assert decision.conflicts == []


def test_a_differing_bundle_is_reported_as_a_conflict_and_writes_nothing():
    local = a_bundle(body_markdown="# Lab Guide\n\nLocally edited.\n", version="1.3")
    decision = plan_import(a_bundle(), local)

    assert decision.action == CONFLICT
    assert not decision.writes
    fields = {c.field for c in decision.conflicts}
    assert fields == {"version", "body_markdown"}
    assert "overwrite" in decision.reason


def test_overwrite_updates_and_still_lists_what_it_replaced():
    local = a_bundle(title="Lab Guide", description="Old description.")
    decision = plan_import(a_bundle(), local, overwrite=True)

    assert decision.action == UPDATE
    assert decision.writes
    assert [c.field for c in decision.conflicts] == ["description"]
    assert decision.conflicts[0].local == "Old description."
    assert decision.conflicts[0].incoming == "How to run the lab."


def test_conflicts_describe_body_and_assets_without_dumping_them():
    local = a_bundle(
        body_markdown="one\ntwo\nthree\n",
        assets=[BundleAsset("a.bin", "application/octet-stream", 1, sha256_of(b"x"), b"x")],
    )
    incoming = a_bundle(
        body_markdown="one\n",
        assets=[BundleAsset("a.bin", "application/octet-stream", 1, sha256_of(b"y"), b"y")],
    )
    decision = plan_import(incoming, local)

    by_field = {c.field: c for c in decision.conflicts}
    assert by_field["body_markdown"].summary == "differs (3 lines -> 1 lines)"
    assert by_field["body_markdown"].local is None  # not dumped into the report
    assert by_field["assets/a.bin"].summary == "contents differ"


def test_conflicts_name_assets_added_and_removed():
    local = a_bundle(
        assets=[BundleAsset("gone.bin", "application/octet-stream", 1, sha256_of(b"x"), b"x")]
    )
    incoming = a_bundle(
        assets=[BundleAsset("new.bin", "application/octet-stream", 1, sha256_of(b"y"), b"y")]
    )

    summaries = {c.field: c.summary for c in plan_import(incoming, local).conflicts}
    assert summaries["assets/gone.bin"] == "present locally, absent in bundle"
    assert summaries["assets/new.bin"] == "new in bundle"


def test_a_conflict_describes_itself_readably():
    local = a_bundle(version="1.0")
    conflict = plan_import(a_bundle(), local).conflicts[0]
    assert conflict.describe() == "version: '1.0' -> '1.2'"
