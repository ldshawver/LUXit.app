"""Application entry point."""
import os

from flask import redirect, request

from lux import create_app as _create_app


def create_app():
    """Create the Flask app using the lux factory."""
    config_name = os.environ.get("FLASK_ENV")
    app = _create_app(config_name)

    @app.before_request
    def enforce_canonical_host():
        allowed_hosts = {"luxit.app", "www.luxit.app", "app.luxit.app", "api.luxit.app"}
        if app.testing:
            allowed_hosts.update({"localhost", "127.0.0.1"})

        host = (request.headers.get("X-Forwarded-Host") or request.host or "").split(":")[0].lower()
        if host and host not in allowed_hosts:
            canonical_host = os.environ.get("CANONICAL_HOST", "app.luxit.app")
            return redirect(f"https://{canonical_host}{request.full_path.rstrip('?')}", code=301)

    return app


if __name__ == "__main__":
    app = create_app()
    app.run()
