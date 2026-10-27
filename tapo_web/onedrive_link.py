"""Connecting a user's OneDrive with Microsoft's sign-in (authorization code + PKCE).

The access is limited to the app's own folder in the user's OneDrive (`Files.ReadWrite.AppFolder`). Connecting is also
choosing OneDrive as the destination; the destination used until then is kept as an earlier source.
"""
import base64
import hashlib
import hmac
import json
import logging
import secrets
from urllib.parse import quote, urlencode

from flask import abort, flash, redirect, request, session, url_for

from tapo_storage.base import StorageError
from tapo_storage.onedrive import SCOPE, OneDriveStorage

from .sources import remember
from .storage import Resolved, resolve

log = logging.getLogger("tapo.web")


def _exchange(settings, code, redirect_uri, verifier):
    storage, drive = OneDriveStorage.with_code(settings.ms_client_id, settings.ms_client_secret, code, redirect_uri, verifier,
                                               settings.ms_tenant, graph_url=settings.ms_graph_url,
                                               login_url=settings.ms_login_url)
    return storage.refresh_token, drive


def register(app):
    state = app.extensions["tapo"]
    settings, db, vault = state["settings"], state["db"], state["vault"]
    state.setdefault("onedrive_exchange", _exchange)

    def uid():
        return app.current_user()["id"]

    def redirect_uri():
        if settings.public_url:
            return f"{settings.public_url}/storage/onedrive/callback"
        return url_for("onedrive_callback", _external=True)

    @app.post("/storage/onedrive/connect")
    @app.login_required
    def onedrive_connect():
        if not settings.allow_own_storage:
            abort(403)
        if not settings.onedrive_enabled:
            return state["storage_show"](503, "OneDrive is not set up on this server. Ask the administrator.")
        if "onedrive" not in settings.storage_allowed:
            return state["storage_show"](400, "That destination is not available on this server.")
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        session["od_state"], session["od_verifier"] = secrets.token_urlsafe(24), verifier
        query = urlencode({"client_id": settings.ms_client_id, "response_type": "code", "redirect_uri": redirect_uri(),
                           "response_mode": "query", "scope": SCOPE, "state": session["od_state"],
                           "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "select_account"},
                          quote_via=quote)
        return redirect(f"{settings.ms_login_url}/{quote(settings.ms_tenant, safe='')}/oauth2/v2.0/authorize?{query}")

    @app.get("/storage/onedrive/callback")
    @app.login_required
    def onedrive_callback():
        expected = session.pop("od_state", None)
        verifier = session.pop("od_verifier", None)
        sent = request.args.get("state", "")
        if not expected or not verifier or not sent or not hmac.compare_digest(sent, expected):
            return state["storage_show"](400, "The OneDrive request expired or was not started here. Please try again.")
        if request.args.get("error") or not request.args.get("code"):
            return state["storage_show"](400, "Microsoft did not allow the access.")
        if not settings.allow_own_storage or not settings.onedrive_enabled:
            abort(403)
        try:
            token, drive = state["onedrive_exchange"](settings, request.args["code"], redirect_uri(), verifier)
        except StorageError as err:
            log.warning("OneDrive link failed: %s", err)
            return state["storage_show"](502, "Could not complete the link with Microsoft. Try again.")
        except Exception as err:  # the network, a malformed answer...
            log.warning("OneDrive link failed: %s", type(err).__name__)
            return state["storage_show"](502, "Could not complete the link with Microsoft. Try again.")
        config = {"refresh_token": token, "account": drive}
        before = resolve(settings, db, vault, uid())
        if before.signature() != Resolved("onedrive", config, "own").signature():
            remember(db, vault, uid(), before)
        db.save_storage_config(uid(), "onedrive", vault.encrypt(json.dumps(config)))
        flash("OneDrive connected. Your recordings go to the app folder in your OneDrive.", "success")
        return redirect("/storage")
