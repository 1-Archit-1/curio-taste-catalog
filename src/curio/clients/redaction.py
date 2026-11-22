"""Keep API keys out of HTTP client logs (ADR 0002).

httpx logs every request URL at INFO, and TMDB v3 keys travel as an `api_key` query parameter.
httpcore logs request headers at DEBUG. `install_log_redaction` scrubs the first and silences
the second.
"""

import logging
import re

_SECRET_PARAM = re.compile(r"(?i)\b(api_key|key|access_token)=[^&\s\"']+")
_BEARER = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]+")
_HEADER = re.compile(
    r"(?i)\(b?['\"](authorization|x-goog-api-key|x-api-key)['\"],\s*b?['\"][^'\"]*['\"]\)"
)


def redact(text: str) -> str:
    text = _SECRET_PARAM.sub(r"\1=[REDACTED]", text)
    text = _BEARER.sub(r"\1 [REDACTED]", text)
    return _HEADER.sub(r"('\1', '[REDACTED]')", text)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.getMessage())
        record.args = None
        return True


def install_log_redaction() -> None:
    """Idempotent. Call once when a client that carries credentials is created."""
    httpx_logger = logging.getLogger("httpx")
    if not any(isinstance(f, RedactingFilter) for f in httpx_logger.filters):
        httpx_logger.addFilter(RedactingFilter())
    # httpcore's DEBUG output includes raw request headers.
    logging.getLogger("httpcore").setLevel(logging.WARNING)
