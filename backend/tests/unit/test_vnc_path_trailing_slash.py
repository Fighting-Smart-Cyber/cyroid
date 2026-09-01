"""The VNC path must work when used as-is.

noVNC references its assets relatively. Served from /vnc/<id>, they resolve
against /vnc/ and every one 404s: the page renders unstyled and reports
"noVNC encountered an error". Served from /vnc/<id>/ they resolve correctly.

The frontend used to paper over this by appending its own slash, so the UI
worked while every other consumer of the API got a broken URL.
"""

import inspect
import pathlib
import re


def test_api_returns_a_usable_path():
    from proving_ground.api import vms

    src = inspect.getsource(vms)
    bare = re.findall(r'"path": f"/vnc/\{vm\.id\}"(?!/)', src)
    assert not bare, (
        "vnc-info returned /vnc/<id> without a trailing slash. Used as-is that "
        "breaks every relative asset noVNC loads."
    )


def test_frontend_does_not_add_a_second_slash():
    """With the API fixed, the old client-side slash would produce '//'."""
    root = pathlib.Path(__file__).resolve().parents[2].parent
    tsx = root / "frontend" / "src" / "components" / "console" / "VncConsole.tsx"
    if not tsx.exists():
        return  # frontend not present in this context
    body = tsx.read_text(encoding="utf-8")
    assert "${data.path}/?autoconnect" not in body, (
        "The frontend appends its own slash. Now that the API supplies one, "
        "this yields /vnc/<id>//?autoconnect=... with a doubled separator."
    )
    assert "${data.path}?autoconnect" in body, "Expected the frontend to use data.path as-is"
