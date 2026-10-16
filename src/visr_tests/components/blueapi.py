"""Check blueapi is up, through its public URL (BLUEAPI_URL)."""

from visr_tests.services import Blueapi


def test_blueapi_healthz(blueapi: Blueapi):
    status = blueapi.healthz()
    assert status == 200, f"blueapi /healthz returned HTTP {status}"
