"""The PV checks, run through the CLI against real CA and PVA servers on loopback.

The CA server is a pythonSoftIOC in its own process (ca_server.py); the PVA
server is a p4p server in this one. The checks find both only through
EPICS_CA_NAME_SERVERS and EPICS_PVA_NAME_SERVERS, as they find the gateway in
the cluster, with every automatic address list off.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from p4p.nt import NTScalar
from p4p.server import Server
from p4p.server.thread import SharedPV

from visr_tests.services import PV_TIMEOUT, Config, ConfigError, ioc_names

LOOPBACK = "127.0.0.1"
TIMEOUT = 1.0  # PV_TIMEOUT for the checks, so a missing PV fails quickly


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp:
        udp.bind((LOOPBACK, 0))
        return udp.getsockname()[1]


def without_epics(environ: dict[str, str]) -> dict[str, str]:
    """``environ`` without EPICS_* or check settings, so nothing real is reached."""
    return {
        k: v
        for k, v in environ.items()
        if not k.startswith("EPICS_") and k not in {"PV_TIMEOUT", "IOC_NAMES"}
    }


@pytest.fixture(scope="module")
def ca_port() -> Iterator[int]:
    port = free_port()
    env = without_epics(dict(os.environ)) | {
        "EPICS_CA_SERVER_PORT": str(port),
        "EPICS_CAS_INTF_ADDR_LIST": LOOPBACK,
        "EPICS_CA_AUTO_ADDR_LIST": "NO",
        "EPICS_CAS_AUTO_BEACON_ADDR_LIST": "NO",
        "EPICS_CAS_BEACON_ADDR_LIST": LOOPBACK,
        # pythonSoftIOC also serves PVA: keep it off the real ports
        "EPICS_PVAS_INTF_ADDR_LIST": LOOPBACK,
        "EPICS_PVAS_SERVER_PORT": str(free_port()),
        "EPICS_PVAS_BROADCAST_PORT": str(free_port()),
        "EPICS_PVAS_AUTO_BEACON_ADDR_LIST": "NO",
    }
    server = subprocess.Popen(
        [sys.executable, str(Path(__file__).parent / "ca_server.py")],
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    assert server.stdin and server.stdout
    try:
        for line in server.stdout:
            if line.strip() == "READY":
                break
        else:
            raise RuntimeError("CA server exited before it was ready")
        yield port
    finally:
        server.stdin.close()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
        server.stdout.close()


@pytest.fixture(scope="module")
def pva_port() -> Iterator[int]:
    active = SharedPV(nt=NTScalar("?"), initial=True)
    with Server(
        providers=[{"BL01C-EA-PANDA-01:Pcap:Active": active}],
        conf={
            "EPICS_PVAS_INTF_ADDR_LIST": LOOPBACK,
            "EPICS_PVAS_SERVER_PORT": "0",
            "EPICS_PVAS_BROADCAST_PORT": str(free_port()),
            "EPICS_PVAS_AUTO_BEACON_ADDR_LIST": "NO",
        },
        useenv=False,
    ) as server:
        yield int(server.conf()["EPICS_PVAS_SERVER_PORT"])


def run(
    tmp_path: Path, ca_port: int, pva_port: int, *args: str, **env: str
) -> tuple[subprocess.CompletedProcess, float]:
    """Run the CLI against the local servers: its result and how long it took."""
    start = time.monotonic()
    result = subprocess.run(
        [sys.executable, "-m", "visr_tests", "run", *args],
        env=without_epics(dict(os.environ))
        | {
            "REPORT_DIR": str(tmp_path),
            "PV_TIMEOUT": str(TIMEOUT),
            "EPICS_CA_NAME_SERVERS": f"{LOOPBACK}:{ca_port}",
            "EPICS_CA_AUTO_ADDR_LIST": "NO",
            "EPICS_CA_ADDR_LIST": "",
            "EPICS_PVA_NAME_SERVERS": f"{LOOPBACK}:{pva_port}",
            "EPICS_PVA_AUTO_ADDR_LIST": "NO",
            "EPICS_PVA_ADDR_LIST": "",
            **env,
        },
        capture_output=True,
        text=True,
    )
    return result, time.monotonic() - start


@pytest.fixture
def cli(tmp_path: Path, ca_port: int, pva_port: int):
    def cli(*args: str, **env: str) -> tuple[subprocess.CompletedProcess, float]:
        return run(tmp_path, ca_port, pva_port, *args, **env)

    return cli


# --- configuration


def test_pv_timeout_from_env():
    assert Config.from_env({}).pv_timeout == PV_TIMEOUT == 5.0
    assert Config.from_env({"PV_TIMEOUT": "2.5"}).pv_timeout == 2.5


@pytest.mark.parametrize("timeout", ["soon", "0"])
def test_bad_pv_timeout(timeout: str):
    with pytest.raises(ConfigError, match="PV_TIMEOUT"):
        Config.from_env({"PV_TIMEOUT": timeout})


def test_ioc_names():
    assert ioc_names({}) == []
    assert ioc_names({"IOC_NAMES": " a,b  c,\nd "}) == ["a", "b", "c", "d"]


# --- healthy


def test_healthy_devices_pass(cli):
    result, _ = cli(
        "components/flipper",
        "BL01C-EA-FLIP-01",
        "components/align",
        "BL01C-EA-ALIGN-01",
        "components/panda",
        "BL01C-EA-PANDA-01",
        "components/iocs",
        IOC_NAMES="bl01c-di-dcam-01",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count(" PASSED") == 5


# --- failures


def test_missing_ca_pv_fails_within_timeout(cli):
    result, took = cli("components/align", "BL01C-EA-ALIGN-99")
    assert result.returncode == 1
    assert (
        "BL01C-EA-ALIGN-99:Pitch:Acceleration_RBV did not answer over CA within "
        "1 s (2 attempts)" in result.stdout
    )
    assert "retrying" in result.stdout
    assert took < 2 * TIMEOUT + 15


def test_missing_pva_pv_fails_within_timeout(cli):
    result, took = cli("components/panda", "BL01C-MO-PPANDA-99")
    assert result.returncode == 1
    assert (
        "BL01C-MO-PPANDA-99:Pcap:Active did not answer over PVA within 1 s "
        "(2 attempts)" in result.stdout
    )
    assert took < 2 * TIMEOUT + 15


def test_missing_ioc_fails(cli):
    result, _ = cli("components/iocs", IOC_NAMES="bl01c-di-dcam-01,bl01c-no-such-ioc")
    assert result.returncode == 1
    assert "test_ioc_uptime[bl01c-di-dcam-01] PASSED" in result.stdout
    assert "BL01C-NO-SUCH-IOC:UPTIME did not answer over CA" in result.stdout


def test_flipper_position_out_of_range_fails(cli):
    result, _ = cli("components/flipper", "BL01C-EA-FLIP-02")
    assert result.returncode == 1
    assert "test_flipper_position_connects[BL01C-EA-FLIP-02] PASSED" in result.stdout
    assert "BL01C-EA-FLIP-02:Position_RBV is 7, expected 0 (False) or 1 (True)" in (
        result.stdout
    )


def test_flipper_position_invalid_alarm_fails(cli):
    result, _ = cli("components/flipper", "BL01C-EA-FLIP-03")
    assert result.returncode == 1
    assert "BL01C-EA-FLIP-03:Position_RBV is in INVALID alarm" in result.stdout


def test_iocs_without_ioc_names_fails_clearly(cli):
    result, _ = cli("components/iocs")
    assert result.returncode == 1
    assert "ConfigError: IOC_NAMES not set" in result.stdout


def test_bad_pv_timeout_fails_clearly(cli):
    result, _ = cli("components/align", "BL01C-EA-ALIGN-01", PV_TIMEOUT="soon")
    assert result.returncode == 1
    assert "ConfigError: PV_TIMEOUT must be a number of seconds" in result.stdout
