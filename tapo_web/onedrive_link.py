"""Connecting a user's OneDrive with Microsoft's sign-in (authorization code + PKCE).

Each person brings their own Microsoft (Azure) app: its ID, secret and, if needed, tenant, which are kept encrypted
with their account. A server may also offer one shared app (MS_CLIENT_ID / MS_CLIENT_SECRET) for people who type
nothing. The access is limited to the app's own folder in the user's OneDrive (`Files.ReadWrite.AppFolder`).
Connecting is also choosing OneDrive as the destination; the destination used until then is kept as a source.
"""
import base64
import hashlib
import hmac
import json
import logging
import re
import secrets
from urllib.parse import quote, urlencode

from flask import abort, flash, redirect, request, session, url_for

from tapo_storage.base import StorageError
from tapo_storage.onedrive import SCOPE, OneDriveStorage

from .crypto import VaultError
from .sources import remember
from .storage import Resolved, resolve

log = logging.getLogger("tapo.web")
CLIENT_ID_RE = re.compile(r"^[0-9A-Za-z._-]{8,64}$")
TENANT_RE = re.compile(r"^(common|consumers|organizations|[0-9a-fA-F-]{36}|[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)+)$")
MAX_SECRET = 200


def _exchange(settings, code, redirect_uri, verifier, app):
    storage, drive = OneDriveStorage.with_code(app["client_id"], app["client_secret"], code, redirect_uri, verifier,
                                               app["tenant"], graph_url=settings.ms_graph_url,
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

    def saved_app():
        """The Microsoft app the person already saved with a connected OneDrive, or {}."""
        row = db.storage_config(uid())
        if not row or row["kind"] != "onedrive" or not row["config_enc"]:
            return {}
        try:
            config = json.loads(vault.decrypt(row["config_enc"]))
        except (VaultError, ValueError):
            return {}
        return {k: config[k] for k in ("client_id", "client_secret", "tenant") if config.get(k)}

    def chosen_app():
        """(app dict, None) from the form, the saved connection or the server; or (None, message)."""
        saved = saved_app()
        typed_id = request.form.get("client_id", "").strip()
        typed_secret = request.form.get("client_secret", "").strip()
        typed_tenant = request.form.get("tenant", "").strip()
        if typed_tenant and not TENANT_RE.fullmatch(typed_tenant):
            return None, "That tenant is not valid: use common, consumers, organizations, or the directory ID."
        if typed_id or typed_secret:
            client_id = typed_id or saved.get("client_id", "")
            client_secret = typed_secret or (saved.get("client_secret", "") if client_id == saved.get("client_id") else "")
            if not CLIENT_ID_RE.fullmatch(client_id):
                return None, "Enter the Application (client) ID of your Microsoft app."
            if not client_secret or len(client_secret) > MAX_SECRET:
                return None, "Enter the client secret of your Microsoft app (its Value, not its ID)."
            return {"client_id": client_id, "client_secret": client_secret, "own": True,
                    "tenant": typed_tenant or saved.get("tenant") or "common"}, None
        if saved.get("client_id") and saved.get("client_secret"):
            return {**saved, "own": True, "tenant": typed_tenant or saved.get("tenant") or "common"}, None
        if settings.onedrive_enabled:
            return {"client_id": settings.ms_client_id, "client_secret": settings.ms_client_secret, "own": False,
                    "tenant": typed_tenant or settings.ms_tenant}, None
        return None, "Enter the Application (client) ID and the client secret of your own Microsoft app (see the setup guide)."

    @app.post("/storage/onedrive/connect")
    @app.login_required
    def onedrive_connect():
        if not settings.allow_own_storage:
            abort(403)
        if "onedrive" not in settings.storage_allowed:
            return state["storage_show"](400, "That destination is not available on this server.")
        chosen, problem = chosen_app()
        if problem:
            return state["storage_show"](400, problem)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        session["od_state"], session["od_verifier"] = secrets.token_urlsafe(24), verifier
        session["od_app"] = vault.encrypt(json.dumps(chosen))     # encrypted: the cookie must not show the secret
        query = urlencode({"client_id": chosen["client_id"], "response_type": "code", "redirect_uri": redirect_uri(),
                           "response_mode": "query", "scope": SCOPE, "state": session["od_state"],
                           "code_challenge": challenge, "code_challenge_method": "S256", "prompt": "select_account"},
                          quote_via=quote)
        return redirect(f"{settings.ms_login_url}/{quote(chosen['tenant'], safe='')}/oauth2/v2.0/authorize?{query}")

    @app.get("/storage/onedrive/callback")
    @app.login_required
    def onedrive_callback():
        expected = session.pop("od_state", None)
        verifier = session.pop("od_verifier", None)
        sealed = session.pop("od_app", None)
        sent = request.args.get("state", "")
        if not expected or not verifier or not sealed or not sent or not hmac.compare_digest(sent, expected):
            return state["storage_show"](400, "The OneDrive request expired or was not started here. Please try again.")
        if request.args.get("error") or not request.args.get("code"):
            return state["storage_show"](400, "Microsoft did not allow the access.")
        if not settings.allow_own_storage:
            abort(403)
        try:
            chosen = json.loads(vault.decrypt(sealed))
        except (VaultError, ValueError):
            return state["storage_show"](400, "The OneDrive request expired or was not started here. Please try again.")
        try:
            token, drive = state["onedrive_exchange"](settings, request.args["code"], redirect_uri(), verifier, chosen)
        except StorageError as err:
            log.warning("OneDrive link failed: %s", err)
            return state["storage_show"](502, "Could not complete the link with Microsoft. Check the app's ID, secret and "
                                              "redirect address, then try again.")
        except Exception as err:  # the network, a malformed answer...
            log.warning("OneDrive link failed: %s", type(err).__name__)
            return state["storage_show"](502, "Could not complete the link with Microsoft. Try again.")
        config = {"refresh_token": token, "account": drive}
        if chosen.get("own"):
            config.update(client_id=chosen["client_id"], client_secret=chosen["client_secret"], tenant=chosen["tenant"])
        before = resolve(settings, db, vault, uid())
        if before.signature() != Resolved("onedrive", config, "own").signature():
            remember(db, vault, uid(), before)
        db.save_storage_config(uid(), "onedrive", vault.encrypt(json.dumps(config)))
        flash("OneDrive connected. Your recordings go to the app folder in your OneDrive.", "success")
        return redirect("/storage")
