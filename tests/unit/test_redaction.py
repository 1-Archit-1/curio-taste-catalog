import logging

from curio.clients.redaction import AccessLogRedactingFilter, install_access_log_redaction


def test_access_log_filter_hides_oauth_callback_parameters():
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        (
            "127.0.0.1:1",
            "GET",
            "/api/simkl/callback?state=abc_DEF-1&iss=https%3A%2F%2Fsimkl.com&code=c0de",
            "1.1",
            303,
        ),
        None,
    )
    assert AccessLogRedactingFilter().filter(record)
    line = record.getMessage()
    assert "abc_DEF-1" not in line and "c0de" not in line
    assert "state=[REDACTED]" in line and "code=[REDACTED]" in line
    assert "iss=https%3A%2F%2Fsimkl.com" in line  # the issuer is public


def test_install_is_idempotent():
    install_access_log_redaction()
    install_access_log_redaction()
    filters = logging.getLogger("uvicorn.access").filters
    assert sum(isinstance(f, AccessLogRedactingFilter) for f in filters) == 1
