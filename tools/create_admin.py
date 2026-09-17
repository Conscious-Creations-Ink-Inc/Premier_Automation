r"""Create the first /ui account, or change an existing one's password.

    .venv\Scripts\python.exe tools\create_admin.py
    .venv\Scripts\python.exe tools\create_admin.py --username alice
    .venv\Scripts\python.exe tools\create_admin.py --username alice --reset-password

The password is read with `getpass`, which does not echo it and does not put it in the terminal
scrollback. **There is deliberately no `--password` flag**: an argument lands in shell history, in
the process list while it runs, and in any terminal recording -- and CLAUDE.md s3 forbids a
credential reaching logs or output. If you need this unattended, pipe the password on stdin.

Writes only to `settings.AUTH_DB_PATH` (`state/auth.sqlite3`). It never opens the pipeline store,
so it cannot touch mail, records or receipts.
"""
import argparse
import getpass
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from api import auth, auth_store                                    # noqa: E402
from config import settings                                         # noqa: E402

MIN_LENGTH = 12
"""Long enough to matter, short enough that nobody writes it on a sticky note. This app is reachable
by anyone who can reach the port, and the only account on it is an administrator."""


def _read_new_password() -> str:
    """Ask twice, never echo, and refuse the ones that are not worth storing."""
    if not sys.stdin.isatty():
        password = sys.stdin.readline().strip()          # piped, for an unattended setup
        if len(password) < MIN_LENGTH:
            raise SystemExit(f"password must be at least {MIN_LENGTH} characters")
        return password
    while True:
        password = getpass.getpass("New password: ")
        if len(password) < MIN_LENGTH:
            print(f"  too short - at least {MIN_LENGTH} characters, please.")
            continue
        if password != getpass.getpass("Repeat password: "):
            print("  those did not match.")
            continue
        return password


def main() -> int:
    parser = argparse.ArgumentParser(description="Create or update a Premier Receiver login.")
    parser.add_argument("--username", help="defaults to asking")
    parser.add_argument("--reset-password", action="store_true",
                        help="set a new password for an account that already exists")
    args = parser.parse_args()

    conn = auth_store.get_connection()
    try:
        username = (args.username or input("Username: ")).strip()
        if not username:
            raise SystemExit("a username is required")

        existing = auth_store.find_user(conn, username)
        if existing and not args.reset_password:
            # Not an overwrite. Silently replacing a password because the command was run twice is
            # indistinguishable from an account takeover.
            raise SystemExit(
                f"{username!r} already exists. Re-run with --reset-password to change it.")
        if args.reset_password and not existing:
            raise SystemExit(f"{username!r} does not exist, so there is no password to reset.")

        password = _read_new_password()
        digest = auth.hash_password(password)
        del password                                     # not kept a line longer than needed
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if existing:
            auth_store.set_password(conn, username=username, password_hash=digest)
            # Anyone still holding a cookie signed with the old key is signed out. A password
            # changed because it may have leaked has not been changed at all if the session it was
            # leaked from keeps working.
            auth_store.rotate_session_secret(conn)
            auth.forget_cached_secret()
            print(f"Password updated for {username!r}. Existing sessions are signed out.")
        else:
            auth_store.create_user(conn, username=username, password_hash=digest, now=now)
            print(f"Created {username!r}.")
        print(f"Store: {settings.AUTH_DB_PATH}")
        print(f"Accounts now: {auth_store.user_count(conn)}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
