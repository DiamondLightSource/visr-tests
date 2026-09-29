"""Fixtures shared by every check."""

import json
import os

import pytest

CHECK_ARGS = "VISR_CHECK_ARGS"


def arguments() -> list[str]:
    """The arguments the check was given on the command line."""
    return json.loads(os.environ.get(CHECK_ARGS, "[]"))


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Run any test wanting check_arg once per argument the check was given."""
    if "check_arg" in metafunc.fixturenames and arguments():
        metafunc.parametrize("check_arg", arguments(), scope="session")


@pytest.fixture(scope="session")
def check_arg() -> str:
    """Only reached when the check needs an argument but was given none."""
    raise AssertionError(
        "this check needs at least one argument, e.g. "
        "`visr-tests run components/flipper BL01C-MO-FLIP-01`"
    )
