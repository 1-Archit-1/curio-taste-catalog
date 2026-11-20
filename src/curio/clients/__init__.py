"""External HTTP clients (TMDB, LLM providers, Letterboxd, Simkl).

Installs a log-redaction filter on httpx/httpcore loggers so API keys in query
parameters or headers can never appear in application logs (ADR 0002).
"""

import logging
import re

_SENSITIVE_QUERY_RE = re.compile(
    r"([?&](?:api_key|key|access_token|token)=)[^&\s\"']+",
    re.IGNORECASE,
)
_SENSITIVE_HEADER_RE = re.compile(
    r"((?:authorization|x-api-key|x-goog-api-key)['\"]?\s*[:=]\s*['\"]?)"
    r"(?:Bearer\s+)?[^\s,\"'}\]]+",
    re.IGNORECASE,
)


def redact_secrets(text: str) -> str:
    """Replace sensitive query parameter values and auth header values with REDACTED."""
    text = _SENSITIVE_QUERY_RE.sub(r"\1REDACTED", text)
    text = _SENSITIVE_HEADER_RE.sub(r"\1REDACTED", text)
    return text


class _HttpSecretRedactionFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact_secrets(record.msg)
        if record.args:
            if isinstance(record.args, tuple):
                record.args = tuple(
                    redact_secrets(str(a)) if not isinstance(a, (int, float)) else a
                    for a in record.args
                )
            elif isinstance(record.args, dict):
                record.args = {
                    k: redact_secrets(str(v)) if not isinstance(v, (int, float)) else v
                    for k, v in record.args.items()
                }
        return True


def install_http_log_redaction() -> None:
    """Attach the secret-redaction filter to httpx and httpcore loggers (idempotent)."""
    for logger_name in ("httpx", "httpcore"):
        logger = logging.getLogger(logger_name)
        if not any(isinstance(f, _HttpSecretRedactionFilter) for f in logger.filters):
            logger.addFilter(_HttpSecretRedactionFilter())


install_http_log_redaction()
