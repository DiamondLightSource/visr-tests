"""Interface for ``python -m visr_tests``."""

import json
import os
import sys
import tempfile
from argparse import ArgumentParser
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

import pytest

from . import __version__

__all__ = ["main"]

CHECKS = Path(__file__).parent
CHECK_ARGS = "VISR_CHECK_ARGS"


def available_checks() -> list[str]:
    """Names of every check module, e.g. ``components/flipper``."""
    return sorted(
        str(path.relative_to(CHECKS).with_suffix(""))
        for path in CHECKS.glob("*/*.py")
        if not path.stem.startswith("_")
    )


def parse_checks(tokens: Sequence[str]) -> list[tuple[str, list[str]]]:
    """Split tokens into (check, arguments) pairs, a check taking tokens after it."""
    checks: list[tuple[str, list[str]]] = []
    for token in tokens:
        if token in available_checks():
            checks.append((token, []))
        elif not checks:
            raise ValueError(
                f"{token!r} is not a check, expected one of {available_checks()}"
            )
        else:
            checks[-1][1].append(token)
    return checks


def run_checks(tokens: Sequence[str]) -> int:
    """Run each check in the order given, in its own pytest session."""
    try:
        checks = parse_checks(tokens)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2

    job = os.environ.get("JOB_NAME", "local")
    root = Path(os.environ.get("REPORT_DIR", Path(tempfile.gettempdir()) / "reports"))
    reports = root / datetime.now().strftime("%Y-%m-%dT%H%M%S") / job

    worst = 0
    for check, arguments in checks:
        os.environ[CHECK_ARGS] = json.dumps(arguments)
        result = pytest.main(
            [
                str(CHECKS / f"{check}.py"),
                "-v",
                "-p",
                "no:cacheprovider",
                "--asyncio-mode=auto",
                f"--confcutdir={CHECKS}",
                f"--junitxml={reports / check}.xml",
            ]
        )
        worst = max(worst, int(result))
    return worst


def main(args: Sequence[str] | None = None) -> None:
    """Argument parser for the CLI."""
    parser = ArgumentParser()
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=__version__,
    )
    subparsers = parser.add_subparsers(dest="command")
    run = subparsers.add_parser("run", help="run checks in the order given")
    run.add_argument(
        "checks",
        nargs="+",
        metavar="CHECK [ARGUMENT ...]",
        help=f"checks to run, one of {available_checks()}",
    )
    parsed = parser.parse_args(args)

    if parsed.command == "run":
        sys.exit(run_checks(parsed.checks))


if __name__ == "__main__":
    main()
