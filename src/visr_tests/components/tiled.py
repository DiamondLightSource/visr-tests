"""Check tiled is up, readable anonymously and serves the ViSR /binned route."""

from visr_tests.services import Tiled

# no run has this name, so /binned must reject it
NO_SUCH_RUN = "visr-tests-no-such-run"


def test_tiled_healthz(tiled: Tiled):
    status = tiled.healthz()
    assert status == 200, f"tiled /healthz returned HTTP {status}"


def test_tiled_anonymous_read(tiled: Tiled):
    assert tiled.count() >= 0


def test_tiled_binned_route(tiled: Tiled):
    """422 for an unknown run shows that the dls_tiled.visr router is mounted."""
    status = tiled.binned_status(NO_SUCH_RUN)
    assert status == 422, (
        f"tiled /binned returned HTTP {status} for an unknown run, expected 422"
    )
