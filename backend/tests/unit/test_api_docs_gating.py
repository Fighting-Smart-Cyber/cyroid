"""Interactive API docs must not be served on a non-debug deployment.

/docs, /redoc and /openapi.json are unauthenticated by nature: anyone who can
reach the host gets the full API surface, including the destructive range
endpoints. That is fine on a developer machine and not fine on an
internet-reachable one.
"""

import inspect


def test_docs_are_gated_on_debug():
    from proving_ground import main

    src = inspect.getsource(main)
    for opt in ("docs_url", "redoc_url", "openapi_url"):
        line = next(ln for ln in src.splitlines() if ln.strip().startswith(f"{opt}="))
        assert "settings.debug" in line, (
            f"{opt} must be gated on settings.debug; unconditional docs expose the "
            "whole API surface to anyone who can reach the host."
        )


def test_production_example_disables_debug():
    """The gate only helps if real deployments actually set DEBUG=false."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[2].parent
    prod = root / ".env.prod.example"
    if not prod.exists():
        return  # repo root not mounted in this context
    assert "DEBUG=false" in prod.read_text(), (
        ".env.prod.example must set DEBUG=false, otherwise the docs gate never "
        "engages on a production deployment."
    )
