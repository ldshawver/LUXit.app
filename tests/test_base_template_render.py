import os

from flask import render_template
from flask_login import UserMixin, login_user

from app import create_app


class DummyUser(UserMixin):
    def __init__(self, user_id="1"):
        self.id = user_id
        self.username = "dummy"
        self.email = "dummy@example.com"

    def get_default_company(self):
        return None

    def get_companies_safe(self):
        return []


def test_base_template_renders_for_authenticated_user():
    os.environ["SESSION_SECRET"] = "test-secret"
    app = create_app()
    app.config["SECRET_KEY"] = "test-secret"
    assert "user.profile" in app.view_functions
    assert "user.change_password" in app.view_functions

    with app.test_request_context("/"):
        login_user(DummyUser())
        rendered = render_template("base.html", current_company=None, user_companies=[])

    assert "dummy" in rendered
