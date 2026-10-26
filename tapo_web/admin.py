"""The administrator's page: users, every camera's status, the day's quota and the main settings (read only)."""
from flask import abort, flash, redirect, render_template, request

from .quota import QuotaLedger

ROLES = ("admin", "user")


def register(app):
    state = app.extensions["tapo"]
    settings, db = state["settings"], state["db"]
    state.setdefault("supervisor", None)
    ledger = QuotaLedger(db, settings.quota_daily)

    def target_user(user_id):
        user = db.user_by_id(user_id)
        if user is None:
            abort(404)
        return user

    def would_remove_last_admin(user):
        active_admins = [u for u in db.list_users() if u["role"] == "admin" and u["active"]]
        return user["role"] == "admin" and user["active"] and len(active_admins) <= 1

    def refuse(message, status=400):
        flash(message, "danger")
        return admin_page(status)

    def resync():
        if state["supervisor"]:
            state["supervisor"].sync()

    def admin_page(status=200):
        supervisor = state["supervisor"]
        users = db.list_users()
        cameras = db.all_cameras_with_owner()
        for camera in cameras:
            camera["running"] = bool(supervisor and supervisor.status(camera["id"])["running"])
        shown = {
            "Server public IP": settings.server_public_ip or "(not set)",
            "Public URL": settings.public_url or "(not set)",
            "Daily YouTube quota": settings.quota_daily,
            "Upload time": f"{settings.upload_at:%H:%M}, {settings.upload_stagger_minutes} min apart",
            "Cameras per user / total": f"{settings.max_cameras_per_user} / {settings.max_cameras_total}",
            "Data kept after deactivation": f"{settings.retention_days} days",
            "Nextcloud": settings.nextcloud_url if settings.nextcloud_enabled else "not configured",
            "Nextcloud groups": (f"admin: {settings.nextcloud_admin_group}, users: "
                                 f"{settings.nextcloud_user_group or '(none: only admins can sign in)'}"
                                 if settings.nextcloud_enabled else "-"),
            "Local accounts": "on" if settings.local_login else "off",
            "Google project": "configured" if settings.google_client_json else "not configured",
            "Own Google project per user": "allowed" if settings.allow_own_google_client else "not allowed",
            "Private camera addresses": "allowed" if settings.allow_private_hosts else "refused",
        }
        return render_template("admin.html", users=users, cameras=cameras, shown=shown,
                               quota_used=ledger.used(), quota_limit=settings.quota_daily), status

    @app.get("/admin")
    @app.admin_required
    def admin():
        return admin_page()

    @app.post("/admin/users/<int:user_id>/active")
    @app.admin_required
    def admin_set_active(user_id):
        user = target_user(user_id)
        value = request.form.get("active")
        if value not in ("0", "1"):
            return refuse("Invalid request.")
        if value == "0" and would_remove_last_admin(user):
            return refuse("You cannot deactivate the last administrator.")
        db.set_user_active(user_id, value == "1")
        resync()
        flash(f"{user['display_name']} is now {'active' if value == '1' else 'deactivated'}.", "success")
        return redirect("/admin")

    @app.post("/admin/users/<int:user_id>/role")
    @app.admin_required
    def admin_set_role(user_id):
        user = target_user(user_id)
        role = request.form.get("role")
        if role not in ROLES:
            return refuse("Invalid role.")
        if user["origin"] != "local":
            return refuse("The role of a Nextcloud user comes from their Nextcloud groups.")
        if role == "user" and would_remove_last_admin(user):
            return refuse("You cannot remove the last administrator.")
        db.set_user_role(user_id, role)
        flash(f"{user['display_name']} is now {role}.", "success")
        return redirect("/admin")
