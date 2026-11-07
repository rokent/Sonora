"""URL secrets never reach uvicorn's access log."""

from __future__ import annotations

import logging

from backend import main  # noqa: F401 - importing the app installs the filter
from backend.home import logredact
from backend.home.logredact import RedactSecrets, redact


def test_redacts_tokens_anywhere():
    assert redact("/auth/display?token=abc123") == "/auth/display?token=REDACTED"
    assert redact("/join?token=s3cr3t&x=1") == "/join?token=REDACTED&x=1"
    assert redact("/api/x?a=1&token=zz") == "/api/x?a=1&token=REDACTED"


def test_redacts_auth_query_values_only_on_auth_paths():
    assert redact("/auth/callback?code=c0de&state=st4te") == "/auth/callback?code=REDACTED&state=REDACTED"
    assert redact("/auth/x?t=abc") == "/auth/x?t=REDACTED"
    assert redact("/api/search?t=abc&state=on") == "/api/search?t=abc&state=on"
    # under an ingress prefix too
    assert redact("/api/hassio_ingress/xyz/auth/display?token=a") == "/api/hassio_ingress/xyz/auth/display?token=REDACTED"


def test_leaves_other_paths_alone():
    assert redact("/api/zones") == "/api/zones"
    assert redact("/api/x?tokens=1") == "/api/x?tokens=1"


def _record(*args):
    return logging.LogRecord(logredact.ACCESS_LOGGER, logging.INFO, __file__, 1,
                             '%s - "%s %s HTTP/%s" %d', args, None)


def test_filter_rewrites_uvicorn_access_record():
    rec = _record("1.2.3.4:5", "GET", "/auth/display?token=abc", "1.1", 302)
    assert RedactSecrets().filter(rec) is True
    assert rec.args == ("1.2.3.4:5", "GET", "/auth/display?token=REDACTED", "1.1", 302)
    assert "abc" not in rec.getMessage()


def test_filter_rewrites_a_plain_message():
    rec = logging.LogRecord(logredact.ACCESS_LOGGER, logging.INFO, __file__, 1,
                            "GET /join?token=abc", None, None)
    RedactSecrets().filter(rec)
    assert rec.getMessage() == "GET /join?token=REDACTED"


def test_installed_once_on_the_access_logger():
    logredact.install()
    logger = logging.getLogger(logredact.ACCESS_LOGGER)
    assert sum(isinstance(f, RedactSecrets) for f in logger.filters) == 1


def test_access_log_output_is_redacted(caplog):
    logger = logging.getLogger(logredact.ACCESS_LOGGER)
    with caplog.at_level(logging.INFO, logger=logredact.ACCESS_LOGGER):
        logger.info('%s - "%s %s HTTP/%s" %d', "1.2.3.4:5", "GET", "/auth/display?token=leak", "1.1", 302)
    assert "leak" not in caplog.text and "token=REDACTED" in caplog.text
