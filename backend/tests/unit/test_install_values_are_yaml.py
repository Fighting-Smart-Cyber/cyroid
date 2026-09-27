"""`scripts/install-k8s.sh` must emit YAML that parses, for every combination of its flags.

This exists because a combination that did not parse shipped. Two adjacent command substitutions
were used to emit two optional keys:

    $([ -n "$A" ] && printf '    secretName: "%s"\\n' "$A")$([ -n "$B" ] && printf ...)

and `$(...)` strips trailing newlines -- including the `\\n` that was supposed to separate them. With
both set, the result was

    secretName: "pg-tls-letsencrypt"    appsSecretName: "pg-tls"

on one line, and `kubectl apply` refused the whole HelmRelease with "did not find expected key".

The fix was easy; the reason it reached a cluster is the interesting part. The change had been
checked with `helm template --set ingress.tls.appsSecretName=...`, which exercises the chart and
never runs the script, so the only broken component was the one not under test. So this drives the
script itself, and asserts on the parsed structure rather than on the text -- a test comparing
strings would have to be rewritten every time a key is added, which is how a test stops being run.
"""

import itertools
import subprocess
import pathlib

import pytest
import yaml

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "install-k8s.sh"

needs_repo = pytest.mark.skipif(not SCRIPT.exists(), reason="repo root not present")

# The optional flags that write into the ingress.tls block, which is where they collided.
OPTIONAL = {
    "TLS_SECRET_NAME": ("pg-tls-letsencrypt", ("ingress", "tls", "secretName")),
    "APPS_TLS_SECRET_NAME": ("pg-tls", ("ingress", "tls", "appsSecretName")),
    "DATA_ACCESS_MODE": ("ReadWriteMany", ("data", "accessMode")),
    "DATA_STORAGE_CLASS": ("azurefile-csi", ("data", "storageClassName")),
}


def _values(**overrides) -> dict:
    """Run the script's own values_yaml() and parse what it printed.

    Sourced rather than reimplemented: a copy of the function here would pass while the script
    was broken, which is precisely the failure this file is about.
    """
    env = {var: "" for var in OPTIONAL}
    env.update(overrides)
    assignments = "\n".join(f'{k}="{v}"' for k, v in env.items())
    script = f"""
        HOST=h.example.org
        APPS_HOST=apps.example.org
        TAG=t
        EXTRA_IPS='[]'
        {assignments}
        {_function_source()}
        values_yaml
    """
    out = subprocess.run(
        ["sh", "-c", script], capture_output=True, text=True, check=True, cwd=REPO_ROOT
    ).stdout
    return yaml.safe_load(out)


def _function_source() -> str:
    text = SCRIPT.read_text()
    start = text.index("values_yaml() {")
    end = text.index("\n}\n", start) + len("\n}\n")
    return text[start:end]


def _dig(d: dict, path: tuple):
    for key in path:
        if not isinstance(d, dict) or key not in d:
            return None
        d = d[key]
    return d


@needs_repo
@pytest.mark.parametrize(
    "chosen",
    [
        combo
        for n in range(len(OPTIONAL) + 1)
        for combo in itertools.combinations(sorted(OPTIONAL), n)
    ],
    ids=lambda c: "+".join(c) or "none",
)
def test_every_combination_of_optional_flags_parses(chosen):
    """All 16 combinations. The one that shipped broken was two of them set together."""
    values = _values(**{var: OPTIONAL[var][0] for var in chosen})
    assert isinstance(values, dict)
    for var in chosen:
        want, path = OPTIONAL[var]
        assert _dig(values, path) == want, f"{var} did not survive as {'.'.join(path)}"
    for var in set(OPTIONAL) - set(chosen):
        _, path = OPTIONAL[var]
        assert _dig(values, path) is None, f"{'.'.join(path)} appeared without {var} being set"


@needs_repo
def test_the_two_tls_keys_land_on_separate_lines():
    """The specific regression, asserted on the text as well.

    The structural test above already fails when they collide, because the result does not parse.
    This one names the thing that went wrong, so a failure says which mistake was repeated rather
    than only that some YAML is malformed.
    """
    script = f"""
        HOST=h.example.org
        APPS_HOST=apps.example.org
        TAG=t
        EXTRA_IPS='[]'
        TLS_SECRET_NAME=pg-tls-letsencrypt
        APPS_TLS_SECRET_NAME=pg-tls
        DATA_ACCESS_MODE=""
        DATA_STORAGE_CLASS=""
        {_function_source()}
        values_yaml
    """
    out = subprocess.run(
        ["sh", "-c", script], capture_output=True, text=True, check=True, cwd=REPO_ROOT
    ).stdout
    carrying = [ln for ln in out.splitlines() if "secretname" in ln.lower()]
    assert len(carrying) == 2, f"expected one line each, got {carrying!r}"
