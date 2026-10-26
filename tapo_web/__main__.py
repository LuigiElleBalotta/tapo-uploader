"""Command line: `python -m tapo_web create-admin` and `python -m tapo_web run`."""
import argparse
import getpass
import logging
import os
import sys

from .auth_local import hash_password
from .config import Settings, SettingsError
from .db import Database


def create_admin(db, username, password):
    """Create a local administrator. Returns its id, or None if that username already exists."""
    if db.user_by_username("local", username):
        return None
    return db.add_user("local", username, username, "admin", hash_password(password))


def _open(environ):
    settings = Settings.from_env(environ)
    db = Database(settings.data_dir / "tapo.db")
    db.migrate()
    return settings, db


def main(argv=None, environ=None):
    environ = os.environ if environ is None else environ
    parser = argparse.ArgumentParser(prog="python -m tapo_web")
    commands = parser.add_subparsers(dest="command", required=True)
    admin = commands.add_parser("create-admin", help="create a local administrator")
    admin.add_argument("--username", default=environ.get("ADMIN_USERNAME"))
    admin.add_argument("--password", default=environ.get("ADMIN_PASSWORD"))
    commands.add_parser("run", help="start the web app")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        settings, db = _open(environ)
    except SettingsError as err:
        sys.exit(f"Configuration error: {err}")

    if args.command == "create-admin":
        username = args.username or input("Username: ").strip()
        password = args.password or getpass.getpass("Password: ")
        try:
            created = create_admin(db, username, password)
        except ValueError as err:
            sys.exit(str(err))
        sys.exit("That username already exists." if created is None else f"Administrator {username} created.")

    # run: the first start can create the administrator from the environment
    if not db.list_users() and environ.get("ADMIN_USERNAME") and environ.get("ADMIN_PASSWORD"):
        create_admin(db, environ["ADMIN_USERNAME"], environ["ADMIN_PASSWORD"])
    from waitress import serve

    from .app import create_app
    serve(create_app(settings, db), host=environ.get("HOST", "0.0.0.0"), port=int(environ.get("PORT", "8080")))


if __name__ == "__main__":
    main()
