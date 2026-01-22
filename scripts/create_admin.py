"""Create or reset an admin user (local-only)."""
import getpass
import os

from werkzeug.security import generate_password_hash

from app import create_app
from lux.extensions import db
from lux.models.user import User


def main() -> None:
    app = create_app()
    with app.app_context():
        username = input("Admin username: ").strip()
        email = input("Admin email: ").strip().lower()
        password = getpass.getpass("Admin password: ")

        if not username or not email or not password:
            raise SystemExit("Username, email, and password are required.")

        user = User.query.filter((User.username == username) | (User.email == email)).first()
        if not user:
            user = User(username=username, email=email)
            db.session.add(user)

        user.password_hash = generate_password_hash(password)
        user.is_admin = True
        db.session.commit()
        print("Admin user ready.")


if __name__ == "__main__":
    if os.environ.get("ALLOW_ADMIN_CREATE") != "true":
        raise SystemExit("Set ALLOW_ADMIN_CREATE=true to run this script.")
    main()
