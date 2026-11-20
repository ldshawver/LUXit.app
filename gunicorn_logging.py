"""Gunicorn logger that keeps bearer-link secrets out of the access log.

Not active by default. Enable with
    gunicorn --logger-class gunicorn_logging.RedactingGunicornLogger ...
Every string access-log atom (request line, path, query, referer and each
{header}i / {environ}e value) passes through diagnostics_service.redact_path, so
/auth/reset-password/<token> is logged as /auth/reset-password/[REDACTED_SECRET].
"""
from gunicorn.glogging import Logger

from diagnostics_service import redact_path


class RedactingGunicornLogger(Logger):
    def atoms(self, resp, req, environ, request_time):
        atoms = super().atoms(resp, req, environ, request_time)
        return {k: redact_path(v) if isinstance(v, str) else v for k, v in atoms.items()}
