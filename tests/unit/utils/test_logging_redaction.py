"""Credentials cannot cross North's final logging boundary."""

from __future__ import annotations

import json
import logging
import sys
from io import StringIO

from utils.logging import _JSONFormatter, _redact_existing_handlers, _RedactingFormatter
from utils.secrets import REDACTED, redact, redact_for_logging


def test_telegram_bot_token_is_removed_from_exception_url() -> None:
    text = "POST https://api.telegram.org/bot123456:ABC-def_ghi/sendMessage returned 401"

    cleaned = redact(text)

    assert "123456:ABC-def_ghi" not in cleaned
    assert "https://api.telegram.org/bot[redacted]/sendMessage" in cleaned


def test_auth_headers_query_parameters_and_url_passwords_are_removed() -> None:
    text = (
        "Authorization: Bearer x "
        "https://alice:hunter2@example.test/data?view=full&api_key=top-secret&limit=10"
    )

    cleaned = redact(text)

    assert "Bearer x" not in cleaned
    assert "hunter2" not in cleaned
    assert "top-secret" not in cleaned
    assert "view=full" in cleaned
    assert "limit=10" in cleaned


def test_json_assignments_and_private_keys_are_removed() -> None:
    text = (
        '{"client_secret": "not-for-logs", "region": "west"}\n'
        "-----BEGIN PRIVATE KEY-----\nprivate-material\n-----END PRIVATE KEY-----"
    )

    cleaned = redact(text)

    assert "not-for-logs" not in cleaned
    assert "private-material" not in cleaned
    assert "west" in cleaned


def test_structured_values_use_secret_field_names_without_hiding_metrics() -> None:
    cleaned = redact_for_logging(
        {
            "headers": {"authorization": "short", "accept": "application/json"},
            "tokens_in": 123,
            "pinned_model": "gpt-test",
        }
    )

    assert cleaned["headers"]["authorization"] == REDACTED
    assert cleaned["headers"]["accept"] == "application/json"
    assert cleaned["tokens_in"] == 123
    assert cleaned["pinned_model"] == "gpt-test"


def test_json_formatter_redacts_message_extra_fields_and_traceback() -> None:
    try:
        raise RuntimeError("request failed at https://api.telegram.org/bot123:token-value/getMe")
    except RuntimeError:
        exc_info = sys.exc_info()

    record = logging.LogRecord(
        name="httpx",
        level=logging.ERROR,
        pathname=__file__,
        lineno=1,
        msg="request Authorization: Bearer header-secret",
        args=(),
        exc_info=exc_info,
    )
    record.request = {"api_key": "tiny", "path": "/health"}

    payload = json.loads(_JSONFormatter().format(record))

    rendered = json.dumps(payload)
    assert "header-secret" not in rendered
    assert "token-value" not in rendered
    assert "tiny" not in rendered
    assert payload["request"]["path"] == "/health"


def test_third_party_formatter_is_redacted_after_rendering() -> None:
    formatter = _RedactingFormatter(logging.Formatter("%(levelname)s %(message)s"))
    record = logging.LogRecord(
        "httpx",
        logging.ERROR,
        __file__,
        1,
        "GET %s",
        ("https://api.telegram.org/bot123:token-value/getMe",),
        None,
    )

    rendered = formatter.format(record)

    assert "token-value" not in rendered
    assert "GET https://api.telegram.org/bot[redacted]/getMe" in rendered


def test_existing_third_party_handlers_are_wrapped() -> None:
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("north.tests.secret-redaction")
    logger.handlers = [handler]
    logger.propagate = False
    try:
        _redact_existing_handlers()
        logger.error("password=hunter2")
    finally:
        logger.handlers = []
        logger.propagate = True

    assert "hunter2" not in stream.getvalue()
    assert REDACTED in stream.getvalue()
