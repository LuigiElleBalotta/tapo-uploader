"""The multi-account web app: server-rendered pages, sessions, CSRF protection and local login."""
import hmac
import secrets
from functools import wraps
from urllib.parse import urlsplit

from flask import Flask, abort, flash, g, redirect, render_template, request, session, url_for

from .auth_local import LoginLimiter, verify_password
from .config import Settings
from .crypto import Vault
from .db import Database


def create_app(settings: Settings, db: Database = None):
    app = Flask(__name__)
    app.config.update(
        SECRET_KEY=settings.secret_key,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=settings.public_url.startswith("https://"),
        MAX_CONTENT_LENGTH=1024 * 1024,
    )
    if db is None:
        db = Database(settings.data_dir / "tapo.db")
        db.migrate()
    app.extensions["tapo"] = {"settings": settings, "db": db, "vault": Vault(settings.encryption_key),
                              "limiter": LoginLimiter()}

    # ----- helpers available to every page -----

    def current_user():
        if "user" not in g:
            user_id = session.get("user_id")
            user = db.user_by_id(user_id) if user_id else None
            g.user = user if user and user["active"] else None
        return g.user

    def csrf_token():
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return session["csrf"]

    app.jinja_env.globals.update(csrf_token=csrf_token, current_user=current_user, settings=settings)

    @app.before_request
    def protect_forms():
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
            expected = session.get("csrf", "")
            # both must be non-empty: two empty strings compare equal and would let a bare POST through
            if not sent or not expected or not hmac.compare_digest(sent, expected):
                abort(400, "invalid or missing CSRF token")

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Content-Security-Policy", "default-src 'self'; img-src 'self' https://i.ytimg.com data:")
        return response

    def login_required(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            if current_user() is None:
                return redirect(url_for("login", next=request.full_path.rstrip("?")))
            return view(*args, **kwargs)
        return wrapper

    def admin_required(view):
        @wraps(view)
        @login_required
        def wrapper(*args, **kwargs):
            if current_user()["role"] != "admin":
                abort(403)
            return view(*args, **kwargs)
        return wrapper

    app.login_required, app.admin_required = login_required, admin_required

    def safe_next(target):
        """Only a path on this site: never send someone to another host after login."""
        if not target:
            return "/"
        parts = urlsplit(target)
        if parts.scheme or parts.netloc or not target.startswith("/") or target.startswith("//"):
            return "/"
        return target

    def sign_in(user):
        session.clear()
        session["user_id"] = user["id"]
        csrf_token()

    app.sign_in, app.safe_next = sign_in, safe_next

    # ----- routes -----

    @app.get("/login")
    def login():
        if current_user():
            return redirect("/")
        return render_template("login.html", next=request.args.get("next", ""))

    @app.post("/login")
    def login_post():
        if not settings.local_login:
            abort(404)
        username = request.form.get("username", "").strip()
        key = (request.remote_addr, username.lower())
        limiter = app.extensions["tapo"]["limiter"]
        if limiter.blocked(key):
            return render_template("login.html", error="Too many attempts. Wait a minute and try again.",
                                   next=request.form.get("next", "")), 429
        user = db.user_by_username("local", username)
        if user and user["active"] and verify_password(request.form.get("password", ""), user["password_hash"]):
            limiter.reset(key)
            sign_in(user)
            return redirect(safe_next(request.form.get("next", "")))
        if not user:
            verify_password(request.form.get("password", ""), None)  # same cost as a real check
        limiter.fail(key)
        return render_template("login.html", error="Wrong username or password.",
                               next=request.form.get("next", "")), 401

    @app.post("/logout")
    def logout():
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    @login_required
    def home():
        return render_template("home.html")

    from .auth_nextcloud import register as register_nextcloud
    register_nextcloud(app)

    return app
