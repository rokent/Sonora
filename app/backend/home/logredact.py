"""Keep secrets carried in URLs out of uvicorn's access log.

A display link (``/auth/display?token=...``), a guest link
(``/join?token=...``) and the sign-in callback (``/auth/callback?code=...&
state=...``) carry their secret in the query string, and uvicorn logs every
request line as it came. This filter rewrites the logged path before any
handler sees it: ``token=`` anywhere, and on ``/auth/`` paths also ``t=``,
``code=`` and ``state=``, become ``<name>=REDACTED``.

Installed from ``home/install.py`` so ``main.py`` stays untouched; uvicorn
has configured its loggers by the time the app is imported, and
``logging.config`` keeps a logger's filters when it is configured again.
"""

from __future__ import annotations

import logging
import re

ACCESS_LOGGER = "uvicorn.access"
REDACTED = "REDACTED"

_ANYWHERE = re.compile(r"(?<=[?&;])(token)=[^&;#\s\"]*", re.IGNORECASE)
_ON_AUTH = re.compile(r"(?<=[?&;])(token|t|code|state)=[^&;#\s\"]*", re.IGNORECASE)


def redact(text: str) -> str:
    """``text`` (a request target, or a whole log line) with its URL secrets
    replaced."""
    if "=" not in text:
        return text
    path = text.split("?", 1)[0]
    pattern = _ON_AUTH if "/auth/" in path else _ANYWHERE
    return pattern.sub(lambda m: f"{m.group(1)}={REDACTED}", text)


class RedactSecrets(logging.Filter):
    """Rewrites the string arguments (uvicorn passes the path as one) and a
    message logged without arguments. Never drops a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if isinstance(record.args, tuple) and record.args:
                record.args = tuple(redact(a) if isinstance(a, str) else a for a in record.args)
            elif not record.args and isinstance(record.msg, str):
                record.msg = redact(record.msg)
        except Exception:                      # noqa: BLE001 - logging must not fail
            pass
        return True


def install() -> None:
    """Add the filter to uvicorn's access logger, once."""
    logger = logging.getLogger(ACCESS_LOGGER)
    if not any(isinstance(f, RedactSecrets) for f in logger.filters):
        logger.addFilter(RedactSecrets())
