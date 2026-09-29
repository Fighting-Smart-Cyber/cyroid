"""A multipart form POST still parses after a `python-multipart` bump.

`requirements.txt` has carried a note on this pin through two security bumps now: the library
renamed its module from `multipart` to `python_multipart` and kept a compatibility shim, and the
FastAPI version pinned here reaches it by the old name. So the risk in bumping it is that the
shim stops resolving.

Measured, by blocking the import and defining a form route: FastAPI raises
`RuntimeError: Form data requires "python-multipart" to be installed` from `check_file_field`
during `add_api_route` -- at route DEFINITION time, not on a request. So the failure is not "form
POSTs start returning 500", it is "the API does not start", and the message it dies with names a
missing dependency rather than the version conflict that actually caused it. Anything in this file
that constructs the app is therefore already the check.

That was verified by hand each time, which is a check that exists only as long as whoever bumps it
reads the comment. This does it in CI instead.

Deliberately a minimal app rather than one of the real upload routes: what is under test is the
contract between FastAPI and this library, and a real route would drag in auth, a database session
and a storage backend, any of which could fail for its own reasons and send someone hunting the
wrong thing.
"""

from __future__ import annotations

import io

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.testclient import TestClient


def _app() -> FastAPI:
    app = FastAPI()

    @app.post("/form")
    def take_form(name: str = Form(...), count: int = Form(...)):
        return {"name": name, "count": count}

    @app.post("/upload")
    def take_upload(note: str = Form(...), payload: UploadFile = File(...)):
        return {"note": note, "filename": payload.filename, "body": payload.file.read().decode()}

    return app


def test_the_compatibility_shim_resolves():
    """FastAPI checks for the module by name before it will accept a form at all."""
    import multipart  # the old name, which is what this FastAPI imports

    assert multipart is not None


def test_a_plain_form_post_round_trips():
    client = TestClient(_app())
    r = client.post("/form", data={"name": "convoy", "count": "17"})
    assert r.status_code == 200, r.text
    assert r.json() == {"name": "convoy", "count": 17}


def test_a_file_upload_round_trips():
    """The shape the platform actually uses: a file beside a form field."""
    client = TestClient(_app())
    r = client.post(
        "/upload",
        data={"note": "blueprint export"},
        files={"payload": ("range.json", io.BytesIO(b'{"ok":true}'), "application/json")},
    )
    assert r.status_code == 200, r.text
    assert r.json() == {
        "note": "blueprint export",
        "filename": "range.json",
        "body": '{"ok":true}',
    }


def test_a_missing_required_field_is_a_422_not_a_500():
    """The failure mode worth pinning: a bad form is bad input, never a server error."""
    client = TestClient(_app())
    r = client.post("/form", data={"name": "convoy"})
    assert r.status_code == 422, r.text
