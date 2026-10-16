"""Fixtures shared by every check."""

import json
import os
from collections.abc import Callable, Iterator
from typing import NoReturn

import httpx
import pytest

from .services import (
    Blueapi,
    CheckError,
    Config,
    ConfigError,
    Tiled,
    fetch_token,
    ioc_names,
)

CHECK_ARGS = "VISR_CHECK_ARGS"
HTTP_TIMEOUT = 30.0


def arguments() -> list[str]:
    """The arguments the check was given on the command line."""
    return json.loads(os.environ.get(CHECK_ARGS, "[]"))


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    """Run any test wanting check_arg once per argument the check was given."""
    if "check_arg" in metafunc.fixturenames and arguments():
        metafunc.parametrize("check_arg", arguments(), scope="session")
    if "ioc" in metafunc.fixturenames and ioc_names():
        metafunc.parametrize("ioc", ioc_names(), scope="session")


@pytest.fixture(scope="session")
def check_arg() -> str:
    """Only reached when the check needs an argument but was given none."""
    raise AssertionError(
        "this check needs at least one argument, e.g. "
        "`visr-tests run components/flipper BL01C-EA-FLIP-01`"
    )


@pytest.fixture(scope="session")
def ioc() -> NoReturn:
    """Only reached when IOC_NAMES names no IOC."""
    _fail(
        ConfigError(
            "IOC_NAMES not set: set it to the IOCs to check, separated by commas "
            "or spaces, e.g. IOC_NAMES=bl01c-di-dcam-01,bl01c-mo-ioc-01"
        )
    )


def _fail(error: Exception) -> NoReturn:
    """Fail without a traceback: the message says everything.

    Call it after the ``except`` block, so pytest shows no exception chain.
    """
    pytest.fail(f"{type(error).__name__}: {error}", pytrace=False)


@pytest.fixture(scope="session")
def config() -> Config:
    """Service URLs and credentials, from the environment."""
    try:
        return Config.from_env()
    except ConfigError as error:
        failure = error
    _fail(failure)


@pytest.fixture(scope="session")
def pv_timeout(config: Config) -> float:
    """Seconds to wait for each PV read, from PV_TIMEOUT."""
    return config.pv_timeout


@pytest.fixture(scope="session")
def instrument_session(config: Config) -> str:
    """The instrument session to run plans in, from INSTRUMENT_SESSION."""
    try:
        return config.require_instrument_session()
    except ConfigError as error:
        failure = error
    _fail(failure)


@pytest.fixture(scope="session")
def http() -> Iterator[httpx.Client]:
    with httpx.Client(timeout=HTTP_TIMEOUT) as client:
        yield client


@pytest.fixture(scope="session")
def token_source(config: Config, http: httpx.Client) -> Callable[[], str]:
    """Fetches the bearer token on first call, then returns the same one."""
    cache: dict[str, str] = {}

    def token() -> str:
        if "token" not in cache:
            try:
                cache["token"] = fetch_token(http, config)
            except (ConfigError, CheckError) as error:
                failure = error
            else:
                return cache["token"]
            _fail(failure)
        return cache["token"]

    return token


@pytest.fixture(scope="session")
def token(token_source: Callable[[], str]) -> str:
    """A client_credentials bearer token for blueapi. Never log it."""
    return token_source()


@pytest.fixture(scope="session")
def blueapi(
    config: Config, http: httpx.Client, token_source: Callable[[], str]
) -> Blueapi:
    """blueapi; the token is fetched only for its first authenticated request."""
    return Blueapi(http, config.blueapi_url, token_source)


@pytest.fixture(scope="session")
def tiled(config: Config, http: httpx.Client) -> Tiled:
    return Tiled(http, config.tiled_url)
