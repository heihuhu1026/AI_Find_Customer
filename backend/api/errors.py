"""Shared helpers for turning unexpected exceptions into safe API responses.

Several endpoints catch a broad ``Exception`` and return ``str(exc)`` to the
client. For connection-test endpoints (SMTP / IMAP / Feishu) that message is
what makes the feature debuggable, so it is deliberately kept — but absolute
filesystem paths are redacted (they leak server layout) and the text is
length-capped. The full exception is always logged server-side so nothing is
lost, and an ``error_id`` lets a user quote a failure without pasting internals.
"""

from __future__ import annotations

import logging
import re
import uuid

logger = logging.getLogger(__name__)

_MAX_DETAIL_CHARS = 500

# Windows absolute paths (C:\... or C:/...) and POSIX paths with at least two
# segments (/var/log/app.log). A bare "/etc" or a URL host like
# "https://smtp.example.com" is intentionally left intact — hostnames are
# exactly what the user needs in order to debug a failing connection.
_ABS_PATH_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/][^\s\"']*|/(?:[\w.~+-]+/)+[\w.~+-]*")


def safe_error_detail(exc: BaseException, *, context: str = "") -> str:
    """Log ``exc`` in full and return a client-safe ``detail`` string.

    Use for broad ``except Exception`` handlers. Do **not** use for deliberate
    business-rule errors (e.g. ``ValueError`` raised to signal "SMTP not tested
    yet") — those messages are authored for the user and should pass through.
    """
    error_id = uuid.uuid4().hex[:8]
    logger.exception(
        "[api] %s failed (error_id=%s): %s",
        context or "request",
        error_id,
        exc,
    )
    message = str(exc).strip() or type(exc).__name__
    message = _ABS_PATH_RE.sub("<redacted-path>", message)
    if len(message) > _MAX_DETAIL_CHARS:
        message = message[:_MAX_DETAIL_CHARS] + "..."
    return f"{message} (error_id={error_id})"
