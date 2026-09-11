"""Unit tests for broker gRPC error extraction and reject classification."""

from grpc import StatusCode
from t_tech.invest.exceptions import RequestError
from t_tech.invest.logging import Metadata

from tinvest_mcp.errors import (
    broker_error_meta,
    format_broker_error,
    is_definitive_broker_reject,
)


def make_error(code=StatusCode.INVALID_ARGUMENT, details="30099", message="Цена вне лимитов"):
    return RequestError(
        code,
        details,
        Metadata(
            tracking_id="abc123",
            ratelimit_limit="200, 200;w=60",
            ratelimit_remaining=17,
            ratelimit_reset=42,
            message=message,
        ),
    )


def test_meta_extracts_every_diagnostic_field():
    meta = broker_error_meta(make_error())
    assert meta["error_type"] == "RequestError"
    assert meta["grpc_code"] == "INVALID_ARGUMENT"
    assert meta["broker_code"] == "30099"
    assert meta["broker_message"] == "Цена вне лимитов"
    assert meta["tracking_id"] == "abc123"
    assert meta["ratelimit_remaining"] == 17


def test_meta_degrades_gracefully_on_plain_exception():
    meta = broker_error_meta(RuntimeError("boom"))
    assert meta == {"error_type": "RuntimeError"}


def test_format_is_single_actionable_line():
    line = format_broker_error(broker_error_meta(make_error()))
    assert line == "INVALID_ARGUMENT 30099: Цена вне лимитов [tracking_id=abc123]"


def test_definitive_reject_classification():
    assert is_definitive_broker_reject(make_error(StatusCode.INVALID_ARGUMENT)) is True
    assert is_definitive_broker_reject(make_error(StatusCode.RESOURCE_EXHAUSTED)) is True
    assert is_definitive_broker_reject(make_error(StatusCode.UNAVAILABLE)) is False
    assert is_definitive_broker_reject(make_error(StatusCode.DEADLINE_EXCEEDED)) is False
    assert is_definitive_broker_reject(RuntimeError("net down")) is False
