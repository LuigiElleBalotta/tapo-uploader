"""Linking each user's own YouTube channel with Google's OAuth web flow.

The token is kept encrypted in the database. `youtube_for_user` is what the workers and the pages use to
get an authorised YouTube client for one user; it refreshes the token when needed and raises `LinkRevoked`
when Google no longer accepts it, so the caller can pause uploads and keep the clips.
"""
import hmac
import json
import logging
import secrets

from flask import abort, flash, redirect, render_template, request, session, url_for
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from .crypto import VaultError

log = logging.getLogger("tapo.web")
SCOPES = ["https://www.googleapis.com/auth/youtube"]


class YoutubeNotConfigured(Exception):
    pass


class LinkRevoked(Exception):
    pass


def build_flow(settings, redirect_uri, client_json=None, code_verifier=None):
    text = client_json or settings.google_client_json
    if not text:
        raise YoutubeNotConfigured("no Google client is configured")
    config = json.loads(text)
    if code_verifier:
        return Flow.from_client_config(config, scopes=SCOPES, redirect_uri=redirect_uri,
                                       code_verifier=code_verifier, autogenerate_code_verifier=False)
    return Flow.from_client_config(config, scopes=SCOPES, redirect_uri=redirect_uri, autogenerate_code_verifier=True)


def client_json_for(settings, vault, link):
    """The Google client to use for this user: their own one when the administrator allows it, else the shared one."""
    if link and link["client_source"] == "own" and link["client_secret_enc"] and settings.allow_own_google_client:
        return vault.decrypt(link["client_secret_enc"])
    if not settings.google_client_json:
        raise YoutubeNotConfigured("no Google client is configured")
    return settings.google_client_json


def credentials_for(vault, link):
    try:
        info = json.loads(vault.decrypt(link["token_enc"]))
    except (VaultError, ValueError) as err:
        raise LinkRevoked("the stored YouTube token cannot be read") from err
    return Credentials.from_authorized_user_info(info, SCOPES)


def _refresh(credentials):
    credentials.refresh(Request())


def _build_youtube(credentials):
    return build("youtube", "v3", credentials=credentials, cache_discovery=False)


def youtube_for_user(db, vault, settings, user_id, refresher=None, builder=None):
    """An authorised YouTube client for this user, or None if they have not linked a channel."""
    link = db.youtube_link(user_id)
    if not link:
        return None
    credentials = credentials_for(vault, link)
    if not credentials.valid:
        try:
            (refresher or _refresh)(credentials)
        except RefreshError as err:
            raise LinkRevoked("Google no longer accepts the saved token") from err
        db.update_youtube_token(user_id, vault.encrypt(credentials.to_json()))
    return (builder or _build_youtube)(credentials)


def _valid_client_json(text):
    try:
        config = json.loads(text)
        section = config.get("web") or config.get("installed")
        return bool(section and section.get("client_id") and section.get("client_secret"))
    except (ValueError, AttributeError):
        return False


def register(app):
    state = app.extensions["tapo"]
    settings, db, vault = state["settings"], state["db"], state["vault"]
    state.setdefault("flow_factory", build_flow)
    state.setdefault("youtube_builder", _build_youtube)

    def redirect_uri():
        if settings.public_url:
            return f"{settings.public_url}/youtube/callback"
        return url_for("youtube_callback", _external=True)

    def current_id():
        return app.current_user()["id"]

    def show(status=200, error=None):
        link = db.youtube_link(current_id())
        return render_template("youtube.html", link=link, error=error,
                               allow_own=settings.allow_own_google_client), status

    @app.get("/youtube")
    @app.login_required
    def youtube_page():
        return show()

    @app.post("/youtube/connect")
    @app.login_required
    def youtube_connect():
        client_json = request.form.get("client_json", "").strip()
        if client_json:
            if not settings.allow_own_google_client:
                abort(403)
            if not _valid_client_json(client_json):
                return show(400, "That is not a valid Google client JSON (it needs a client_id and a client_secret).")
        try:
            flow = state["flow_factory"](settings, redirect_uri(), client_json or None)
        except YoutubeNotConfigured:
            return show(503, "YouTube linking is not configured on this server. Ask the administrator.")
        url, oauth_state = flow.authorization_url(access_type="offline", prompt="consent",
                                                   include_granted_scopes="true")
        session["yt_state"] = oauth_state
        session["yt_verifier"] = flow.code_verifier
        session["yt_client_json"] = client_json or None
        return redirect(url)

    @app.get("/youtube/callback")
    @app.login_required
    def youtube_callback():
        expected = session.pop("yt_state", None)
        verifier = session.pop("yt_verifier", None)
        own_json = session.pop("yt_client_json", None)
        sent = request.args.get("state", "")
        if not expected or not sent or not hmac.compare_digest(sent, expected):
            return show(400, "The link request expired or was not started here. Please try again.")
        if request.args.get("error") or not request.args.get("code"):
            return show(400, "Google did not allow the access.")
        try:
            flow = state["flow_factory"](settings, redirect_uri(), own_json, verifier)
            flow.fetch_token(code=request.args["code"])
            credentials = flow.credentials
        except Exception as err:  # oauthlib and google-auth raise many different types
            log.warning("YouTube link failed: %s", type(err).__name__)
            return show(502, "Could not complete the link with Google. Try again.")
        if not credentials.refresh_token:
            return show(400, "Google did not give a long-lived access. Remove tapo-uploader at "
                             "myaccount.google.com/permissions and link again.")
        try:
            items = state["youtube_builder"](credentials).channels().list(part="snippet", mine=True).execute()["items"]
            title = items[0]["snippet"]["title"]
        except Exception as err:
            log.warning("YouTube channel lookup failed: %s", type(err).__name__)
            return show(400, "This Google account has no YouTube channel to upload to.")
        db.save_youtube_link(current_id(), vault.encrypt(credentials.to_json()), title,
                             "own" if own_json else "shared", vault.encrypt(own_json) if own_json else None)
        flash(f"Linked to the channel {title}.", "success")
        return redirect("/youtube")

    @app.post("/youtube/disconnect")
    @app.login_required
    def youtube_disconnect():
        db.delete_youtube_link(current_id())
        flash("YouTube disconnected. You can also remove the access at myaccount.google.com/permissions.", "info")
        return redirect("/youtube")
