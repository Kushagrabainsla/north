"""Which errors are a missing resource that comes back, rather than a failure (#34)."""

from __future__ import annotations

import pytest

from inference.exceptions import (
    AllModelsRateLimitedError,
    PaymentRequiredError,
    PinnedModelUnavailableError,
    ProviderUnavailableError,
    missing_resource,
)


def _wrapped(inner: BaseException) -> Exception:
    try:
        try:
            raise inner
        except BaseException as exc:
            raise RuntimeError("agent failed") from exc
    except RuntimeError as outer:
        return outer


@pytest.mark.parametrize(
    "error",
    [
        AllModelsRateLimitedError("all cooling down"),
        PaymentRequiredError("some-model", "openrouter"),
        ProviderUnavailableError("503"),
    ],
)
def test_no_model_is_a_missing_model(error: Exception) -> None:
    assert missing_resource(error) == "a model"
    assert missing_resource(_wrapped(error)) == "a model", "found through wrapping too"


def test_a_dropped_connection_is_a_missing_network() -> None:
    assert missing_resource(ConnectionError("network is unreachable")) == "the network"

    class ConnectError(Exception):
        """Named like httpx's, which this module does not import."""

    assert missing_resource(ConnectError("connect failed")) == "the network"


@pytest.mark.parametrize("error", [ValueError("bad output"), PinnedModelUnavailableError("no such model")])
def test_a_real_failure_or_a_setting_to_fix_is_not_a_missing_resource(error: Exception) -> None:
    assert missing_resource(error) == ""
