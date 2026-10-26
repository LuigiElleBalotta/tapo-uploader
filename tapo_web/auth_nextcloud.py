"""Login with Nextcloud (its built-in OAuth2 provider); roles come from the user's Nextcloud groups.

Nextcloud is only asked who the user is and which groups they belong to. The service keeps no Nextcloud
token after the login, and the role is recomputed on every login.
"""
import hmac
import logging
import secrets
from urllib.parse import urlencode

import requests
from flask import current_app, redirect, render_template, request, session, url_for

log = logging.getLogger("tapo.web")
TIMEOUT = 10


class NextcloudError(Exception):
    pass


def role_from_groups(groups, admin_group, user_group):
    """'admin', 'user' or None (no access). Group names are compared exactly, case included."""
    groups = groups or []
    if admin_group and admin_group in groups:
        return "admin"
    if user_group and user_group in groups:
        return "user"
    return None


def exchange_code(settings, code, redirect_uri, http=requests):
    reply = http.post(f"{settings.nextcloud_url}/index.php/apps/oauth2/api/v1/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri,
        "client_id": settings.nextcloud_client_id, "client_secret": settings.nextcloud_client_secret,
    }, timeout=TIMEOUT)
    reply.raise_for_status()
    token = reply.json().get("access_token")
    if not token:
        raise NextcloudError("no access token in the reply")
    return token


def fetch_nextcloud_user(settings, access_token, http=requests):
    reply = http.get(f"{settings.nextcloud_url}/ocs/v2.php/cloud/user?format=json",
                     headers={"Authorization": f"Bearer {access_token}", "OCS-APIRequest": "true",
                              "Accept": "application/json"}, timeout=TIMEOUT)
    reply.raise_for_status()
    data = reply.json()["ocs"]["data"]
    return {"id": str(data["id"]), "display_name": data.get("displayname") or str(data["id"]),
            "groups": list(data.get("groups") or [])}


def register(app):
    state = app.extensions["tapo"]
    settings, db = state["settings"], state["db"]
    state.setdefault("http", requests)

    def redirect_uri():
        if settings.public_url:
            return f"{settings.public_url}/auth/nextcloud/callback"
        return url_for("nextcloud_callback", _external=True)

    def refuse(message, status):
        return render_template("login.html", error=message, next=""), status

    @app.get("/auth/nextcloud/start")
    def nextcloud_start():
        if not settings.nextcloud_enabled:
            from flask import abort
            abort(404)
        session["nc_state"] = secrets.token_urlsafe(32)
        session["nc_next"] = app.safe_next(request.args.get("next", ""))
        query = urlencode({"response_type": "code", "client_id": settings.nextcloud_client_id,
                           "redirect_uri": redirect_uri(), "state": session["nc_state"]})
        return redirect(f"{settings.nextcloud_url}/index.php/apps/oauth2/authorize?{query}")

    @app.get("/auth/nextcloud/callback")
    def nextcloud_callback():
        if not settings.nextcloud_enabled:
            from flask import abort
            abort(404)
        expected = session.pop("nc_state", None)
        sent = request.args.get("state", "")
        after = session.pop("nc_next", "/")
        if not expected or not sent or not hmac.compare_digest(sent, expected):
            return refuse("The login expired or was not started here. Please try again.", 400)
        if request.args.get("error") or not request.args.get("code"):
            return refuse("Nextcloud did not allow the login.", 400)
        try:
            http = state["http"]
            token = exchange_code(settings, request.args["code"], redirect_uri(), http)
            info = fetch_nextcloud_user(settings, token, http)
        except (requests.RequestException, NextcloudError, KeyError, ValueError) as err:
            log.warning("Nextcloud login failed: %s", type(err).__name__)
            return refuse("Could not complete the login with Nextcloud. Try again later.", 502)

        role = role_from_groups(info["groups"], settings.nextcloud_admin_group, settings.nextcloud_user_group)
        user = db.user_by_username("nextcloud", info["id"])
        if role is None:
            if user:
                db.set_user_active(user["id"], False)  # left the group: out at once, workers stop on the next sync
            return refuse("Your Nextcloud account is not allowed to use this service.", 403)
        if user is None:
            user = db.user_by_id(db.add_user("nextcloud", info["id"], info["display_name"], role))
        elif not user["active"]:
            return refuse("This account is disabled. Ask an administrator.", 403)
        else:
            db.update_user_profile(user["id"], info["display_name"], role)
            user = db.user_by_id(user["id"])
        app.sign_in(user)
        return redirect(app.safe_next(after))
