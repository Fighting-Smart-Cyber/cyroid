"""ASD STIG APSC-DV-001680: the application must enforce a minimum 15-character password length.

The boundary tests below are the easy half. The half that matters is
test_every_password_accepting_endpoint_applies_the_policy: the requirement was already "enforced"
before this, in one of the three endpoints that set a password, which is indistinguishable from not
being enforced at all - an account created with a 4-character password satisfies nobody's policy just
because changing it later would have been checked. A fourth endpoint that calls get_password_hash
without the policy would reintroduce exactly that, so it is caught here rather than in an assessment.
"""

import ast
import pathlib

import pytest

from proving_ground.utils.security import PASSWORD_MIN_LENGTH, password_policy_error

API_DIR = pathlib.Path(__file__).resolve().parents[2] / "proving_ground" / "api"


def test_the_minimum_is_the_one_the_stig_requires():
    assert PASSWORD_MIN_LENGTH == 15


@pytest.mark.parametrize("password", ["", "a", "x" * 8, "x" * 14])
def test_passwords_shorter_than_the_minimum_are_refused(password):
    error = password_policy_error(password)
    assert error is not None
    assert "15" in error


def test_a_missing_password_is_refused_rather_than_crashing():
    assert password_policy_error(None) is not None


@pytest.mark.parametrize("password", ["x" * 15, "x" * 64, "correct horse battery staple"])
def test_passwords_at_or_above_the_minimum_are_accepted(password):
    assert password_policy_error(password) is None


def _functions_calling(tree, name):
    """Names of the functions in `tree` whose body mentions a call to `name`."""
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            if (
                isinstance(inner, ast.Call)
                and isinstance(inner.func, ast.Name)
                and inner.func.id == name
            ):
                found.add(node.name)
    return found


def test_every_password_accepting_endpoint_applies_the_policy():
    """Any function that hashes a password must also have checked it against the policy."""
    offenders = []
    checked = 0
    for path in sorted(API_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        hashes = _functions_calling(tree, "get_password_hash")
        guards = _functions_calling(tree, "password_policy_error")
        for func in sorted(hashes):
            checked += 1
            if func not in guards:
                offenders.append("%s::%s" % (path.name, func))

    # If this drops to zero the test has stopped testing anything: it would pass just as happily
    # against an API with no password handling at all, which is not the situation being guarded.
    assert checked >= 3, (
        "expected at least the three known password-setting endpoints, found %d - has the API moved?"
        % checked
    )
    assert not offenders, (
        "these functions hash a password without checking password_policy_error first, so they "
        "bypass the %d-character minimum: %s" % (PASSWORD_MIN_LENGTH, ", ".join(offenders))
    )
