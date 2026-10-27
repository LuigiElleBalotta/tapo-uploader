"""The page where a user sees where their recordings go and, if the server allows it, changes it."""
import json
from urllib.parse import urlsplit

from flask import abort, flash, redirect, render_template, request

from tapo_storage.base import StorageError, make_storage

from .config import USER_STORAGE_KINDS
from .crypto import VaultError
from .security import UnsafeHost, assert_public_host, checked_host
from .sources import label_for, remember, remembered
from .storage import Resolved, resolve, server_default

LABELS = {"youtube": "YouTube", "onedrive": "OneDrive (your own, app folder only)", "webdav": "WebDAV (Nextcloud and similar)",
          "local": "a folder on the server", "rclone": "a cloud storage connected to the server (rclone)"}


def register(app):
    state = app.extensions["tapo"]
    settings, db, vault = state["settings"], state["db"], state["vault"]
    state.setdefault("storage_builder", make_storage)

    def uid():
        return app.current_user()["id"]

    def own_webdav():
        row = db.storage_config(uid())
        if not row or row["kind"] != "webdav" or not row["config_enc"]:
            return {}
        try:
            return json.loads(vault.decrypt(row["config_enc"]))
        except (VaultError, ValueError):
            return {}

    def own_onedrive():
        row = db.storage_config(uid())
        if not row or row["kind"] != "onedrive" or not row["config_enc"]:
            return {}
        try:
            return json.loads(vault.decrypt(row["config_enc"]))
        except (VaultError, ValueError):
            return {}

    def show(status=200, error=None, result=None, form=None):
        resolved = resolve(settings, db, vault, uid())
        row = db.storage_config(uid())
        own = own_webdav()
        kinds = [k for k in settings.storage_allowed if k in USER_STORAGE_KINDS and k != "onedrive"]   # OneDrive has its own button
        offer_onedrive = settings.allow_own_storage and "onedrive" in settings.storage_allowed
        values = form or {"kind": row["kind"] if row and resolved.source == "own" else "default",
                          "url": own.get("url", ""), "user": own.get("user", ""), "folder": own.get("folder", "")}
        return render_template("storage.html", resolved=resolved, label=LABELS[resolved.kind],
                               allow_own=settings.allow_own_storage and bool(kinds), kinds=kinds, labels=LABELS,
                               offer_onedrive=offer_onedrive,
                               onedrive_redirect=(settings.public_url or request.host_url.rstrip("/")) + "/storage/onedrive/callback", server_has_ms_app=settings.onedrive_enabled,
                               saved_ms_client_id=own_onedrive().get("client_id", ""), onedrive_now=resolved.kind == "onedrive" and resolved.source == "own",
                               values=values, has_password=bool(own.get("password")), error=error, result=result,
                               server_label=LABELS[settings.storage],
                               sources=[{"id": sid, "label": label_for(old)} for sid, old in remembered(db, vault, uid())
                                        if old.signature() != resolved.signature()]), status

    def keep_if_left(before, after):
        """The destination in use stays a source when the new one is a different place."""
        if before.signature() != after.signature():
            remember(db, vault, uid(), before)

    state["storage_show"] = show

    def webdav_config():
        """The WebDAV settings in the form, checked; (config, None) or (None, message)."""
        url = request.form.get("url", "").strip()
        parts = urlsplit(url)
        if parts.scheme not in ("https", "http") or not parts.hostname:
            return None, "The WebDAV address must start with https://"
        if parts.scheme == "http" and not settings.allow_private_hosts:
            return None, "Use an https:// address: the password would travel unencrypted."
        try:
            assert_public_host(parts.hostname, settings.allow_private_hosts, state["resolver"])
        except UnsafeHost as err:
            return None, f"That address is not allowed: {err}."
        config = {"url": url, "user": request.form.get("user", "").strip(),
                  "password": request.form.get("password", "") or own_webdav().get("password", ""),
                  "folder": request.form.get("folder", "").strip().strip("/")}
        try:
            make_storage("webdav", {**config, "prefix": "Tapo"}, None)  # only to validate the values
        except StorageError as err:
            return None, str(err)
        return config, None

    def form_values():
        return {"kind": request.form.get("kind", ""), "url": request.form.get("url", ""),
                "user": request.form.get("user", ""), "folder": request.form.get("folder", "")}

    @app.get("/storage")
    @app.login_required
    def storage_page():
        return show()

    @app.post("/storage")
    @app.login_required
    def storage_save():
        if not settings.allow_own_storage:
            abort(403)
        kind = request.form.get("kind", "")
        before = resolve(settings, db, vault, uid())
        if kind == "default":
            keep_if_left(before, server_default(settings))
            db.delete_storage_config(uid())
            flash("Your recordings go to the server's default destination.", "success")
            return redirect("/storage")
        if kind == "local" and "local" in settings.storage_allowed:
            keep_if_left(before, Resolved("local", {}, "own"))
            db.save_storage_config(uid(), "local", None)
            flash("Your recordings are kept on the server.", "success")
            return redirect("/storage")
        if kind == "onedrive":
            return show(400, "Use the Connect OneDrive button to link your OneDrive.", form=form_values())
        if kind not in USER_STORAGE_KINDS or kind not in settings.storage_allowed:
            return show(400, "That destination is not available on this server.", form=form_values())
        if kind == "youtube":
            keep_if_left(before, Resolved("youtube", {}, "own"))
            db.save_storage_config(uid(), "youtube", None)
            flash("Your recordings go to your YouTube channel.", "success")
            return redirect("/storage")
        config, problem = webdav_config()
        if problem:
            return show(400, problem, form=form_values())
        keep_if_left(before, Resolved("webdav", config, "own"))
        db.save_storage_config(uid(), "webdav", vault.encrypt(json.dumps(config)))
        flash("Saved. The recorder restarts with the new destination within a minute.", "success")
        return redirect("/storage")

    @app.post("/storage/sources/<int:source_id>/forget")
    @app.login_required
    def storage_forget_source(source_id):
        """Stop listing an earlier destination. Nothing is deleted from it."""
        if db.storage_source(uid(), source_id) is None:
            abort(404)
        db.delete_storage_source(uid(), source_id)
        flash("Removed from your list. The videos are still where they were; nothing was deleted.", "info")
        return redirect("/storage")

    @app.post("/storage/test")
    @app.login_required
    def storage_test():
        if not settings.allow_own_storage:
            abort(403)
        if request.form.get("kind") != "webdav" or "webdav" not in settings.storage_allowed:
            return show(400, "There is nothing to test for that choice.", form=form_values())
        config, problem = webdav_config()
        if problem:
            return show(400, problem, form=form_values())

        def host_check(host):
            return checked_host(host, settings.allow_private_hosts, state["resolver"])

        try:
            state["storage_builder"]("webdav", {**config, "prefix": "Tapo"}, None, host_check).check()
        except StorageError as err:
            return show(200, result=("danger", str(err)), form=form_values())
        return show(200, result=("success", "Works: the server could write to that folder."), form=form_values())
