"""Run from the authenticated server shell; passwords never appear in argv."""
import argparse
import getpass
import secrets
import time

from backend.main import database, email_address, initialize, password_hash, validate_password


def main():
    parser = argparse.ArgumentParser(description="Provision an admin or reset a user's password.")
    parser.add_argument("command", choices=["create-admin", "reset-password"])
    parser.add_argument("email")
    args = parser.parse_args()
    initialize()
    email = email_address(args.email)
    password = getpass.getpass("New password (at least 12 characters): ")
    validate_password(password)
    if password != getpass.getpass("Repeat new password: "):
        parser.error("Passwords did not match.")
    with database() as db:
        row = db.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if args.command == "reset-password":
            if not row:
                parser.error("Account not found.")
            db.execute("UPDATE users SET password=? WHERE id=?", (password_hash(password), row["id"]))
            db.execute("DELETE FROM sessions WHERE user_id=?", (row["id"],))
        else:
            if row:
                parser.error("Account already exists. This command only creates new admins.")
            db.execute("INSERT INTO users VALUES (?,?,?,?, 'admin',?)", (secrets.token_hex(16), email, "Administrator", password_hash(password), int(time.time())))
    print("Account updated. Existing sessions were revoked for password resets.")


if __name__ == "__main__":
    main()
