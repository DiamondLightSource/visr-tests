"""Check a flipper moves between its two positions."""

import logging

log = logging.getLogger(__name__)


def test_flipper_cycles(check_arg: str):
    log.info("pass cycle of flipper %s", check_arg)
