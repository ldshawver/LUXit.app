"""
Reset or create the admin account on the VPS.

Usage (run from /root/lux-email-bot):
  python3 scripts/reset_admin.py

No environment flags required.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from werkzeug.security import generate_password_hash
from app import create_app
from extensions import db
from models import User


def main():
    app = create_app()
    with app.app_context():

        # Show existing admin accounts
        admins = User.query.filter_by(is_admin=True).all()
        if admins:
            print("\nExisting admin accounts:")
            for u in admins:
                print(f"  id={u.id}  username={u.username}  email={u.email}")
        else:
            print("\nNo admin accounts found — a new one will be created.")

        print()
        username = input("Username to reset/create: ").strip()
        email    = input("Email address: ").strip().lower()

        import getpass
        password = getpass.getpass("New password (min 8 chars): ")
        confirm  = getpass.getpass("Confirm password: ")

        if not username or not email or not password:
            print("ERROR: All fields are required.")
            sys.exit(1)
        if password != confirm:
            print("ERROR: Passwords do not match.")
            sys.exit(1)
        if len(password) < 8:
            print("ERROR: Password must be at least 8 characters.")
            sys.exit(1)

        user = User.query.filter(
            (User.username == username) | (User.email == email)
        ).first()

        if user:
            user.username      = username
            user.email         = email
            user.password_hash = generate_password_hash(password)
            user.is_admin      = True
            db.session.commit()
            print(f"\nPassword reset for user '{username}' (id={user.id}). You can now log in.")
        else:
            user = User(
                username=username,
                email=email,
                password_hash=generate_password_hash(password),
                is_admin=True,
            )
            db.session.add(user)
            db.session.commit()
            print(f"\nAdmin account created for '{username}' (id={user.id}). You can now log in.")


if __name__ == "__main__":
    main()
