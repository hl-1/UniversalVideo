from __future__ import annotations

import argparse
import getpass
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import set_key
from pydantic import ValidationError
from backend.modules.accounts import service as accounts
from backend.modules.music import library


def read_password(username: str) -> str:
    print("Password requirements: 8-128 characters; no required character types.")
    while True:
        password = getpass.getpass("New admin password (input hidden): ")
        try:
            accounts.Credentials(username=username, password=password)
        except ValidationError:
            print("Password must contain 8-128 characters. Please try again.")
            continue
        if password != getpass.getpass("Repeat password: "):
            print("Passwords do not match. Please try again.")
            continue
        return password


def main():
    parser = argparse.ArgumentParser(description="Set the Flyway-created administrator password locally.")
    parser.add_argument("--username", default="admin")
    args = parser.parse_args()
    if not accounts.environment("VIDEODREAM_JWT_SECRET"):
        set_key(str(accounts.LOCAL_CONFIG_PATH), "VIDEODREAM_JWT_SECRET", secrets.token_urlsafe(48))
    with library.database() as cursor:
        cursor.execute("SELECT id,username FROM users WHERE id=1")
        user = cursor.fetchone()
    if not user:
        parser.error("Administrator is missing. Run scripts/database.py migrate first.")
    print("Administrator: " + user["username"])
    if args.username != user["username"]:
        parser.error("The initialized administrator is " + user["username"])
    password = read_password(args.username)
    accounts.redis_client().ping()
    with library.database() as cursor:
        cursor.execute("SELECT password_hash FROM users WHERE id=1 FOR UPDATE")
        first_initialization = cursor.fetchone()["password_hash"] == "!uninitialized"
        if first_initialization:
            cursor.execute("UPDATE system_settings SET config=JSON_SET(config,'$.registration_enabled',CAST('true' AS JSON)) WHERE id=1")
        cursor.execute("UPDATE users SET password_hash=%s,must_change_password=FALSE,auth_version=auth_version+1 WHERE id=1",
                       (accounts.passwords.hash(password),))
    accounts.revoke_all(1)
    accounts.audit("admin_initialize", "admin", actor=1, ip="local")
    print("Administrator password set. Open http://127.0.0.1:5174/")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\nPassword input cancelled.")
        sys.exit(1)
