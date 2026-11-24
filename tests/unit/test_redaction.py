import logging

from uvicorn.logging import AccessFormatter

from curio.clients.redaction import AccessLogRedactingFilter, install_access_log_redaction


def _access_record(path: str) -> logging.LogRecord:
    # The shape uvicorn logs: (client, method, path, http_version, status).
    return logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1", "GET", path, "1.1", 303),
        None,
    )


def test_access_log_filter_hides_oauth_callback_parameters_and_keeps_uvicorns_format():
    record = _access_record(
        "/api/simkl/callback?state=abc_DEF-1&iss=https%3A%2F%2Fsimkl.com&code=c0de"
    )
    assert AccessLogRedactingFilter().filter(record)
    # uvicorn's own formatter unpacks the args, so they must still be a 5-tuple.
    line = AccessFormatter("%(client_addr)s - %(request_line)s %(status_code)s").format(record)
    assert "abc_DEF-1" not in line and "c0de" not in line
    assert "state=[REDACTED]" in line and "code=[REDACTED]" in line
    assert "iss=https%3A%2F%2Fsimkl.com" in line  # the issuer is public
    assert "303" in line


def test_access_log_filter_leaves_other_requests_alone():
    record = _access_record("/api/sources?status=pending&code_x=1")
    AccessLogRedactingFilter().filter(record)
    assert record.args is not None and record.args[2] == "/api/sources?status=pending&code_x=1"


def test_install_is_idempotent():
    install_access_log_redaction()
    install_access_log_redaction()
    filters = logging.getLogger("uvicorn.access").filters
    assert sum(isinstance(f, AccessLogRedactingFilter) for f in filters) == 1
