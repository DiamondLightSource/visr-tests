import os
import subprocess
import sys
from pathlib import Path

from visr_tests import __version__


def test_cli_version():
    cmd = [sys.executable, "-m", "visr_tests", "--version"]
    assert subprocess.check_output(cmd).decode().strip() == __version__


def run_cli(tmp_path: Path, *args: str, **env: str) -> subprocess.CompletedProcess:
    keep = {
        k: v
        for k, v in os.environ.items()
        if k
        not in {
            "BLUEAPI_URL",
            "TILED_URL",
            "TOKEN_URL",
            "CLIENT_ID",
            "CLIENT_SECRET",
            "INSTRUMENT_SESSION",
            "SCAN_TIMEOUT",
        }
    }
    return subprocess.run(
        [sys.executable, "-m", "visr_tests", "run", *args],
        env={**keep, "REPORT_DIR": str(tmp_path), **env},
        capture_output=True,
        text=True,
    )


def reports(tmp_path: Path) -> str:
    return "".join(path.read_text() for path in tmp_path.rglob("*.xml"))


def test_scan_without_instrument_session_fails_clearly(tmp_path: Path):
    result = run_cli(tmp_path, "experiments/spectroscopy")
    assert result.returncode == 1
    assert "ConfigError: INSTRUMENT_SESSION not set" in result.stdout
    assert "INSTRUMENT_SESSION not set" in reports(tmp_path)


def test_scan_without_credentials_fails_clearly(tmp_path: Path):
    result = run_cli(tmp_path, "experiments/spectroscopy", INSTRUMENT_SESSION="cm1-1")
    assert result.returncode == 1
    assert "ConfigError: CLIENT_ID and CLIENT_SECRET not set" in result.stdout


def test_secret_not_in_output_or_reports(tmp_path: Path):
    secret = "s3cret-client-value"
    result = run_cli(
        tmp_path,
        "experiments/spectroscopy",
        INSTRUMENT_SESSION="cm1-1",
        CLIENT_ID="visr-tests",
        CLIENT_SECRET=secret,
        TOKEN_URL="http://127.0.0.1:9/token",  # refused
    )
    assert result.returncode == 1
    assert "token endpoint http://127.0.0.1:9/token unreachable" in result.stdout
    assert secret not in result.stdout + result.stderr + reports(tmp_path)
