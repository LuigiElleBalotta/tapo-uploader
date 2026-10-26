"""Cameras of the multi-account mode: ports, the context handed to the workers, the connection test and the pages."""
import dataclasses
import logging
import random
import socket
import subprocess
from dataclasses import dataclass

from flask import abort, flash, redirect, render_template, request, url_for

from camera_context import CameraContext

from .guide import build_guide
from .security import UnsafeHost, assert_public_host, checked_host

log = logging.getLogger("tapo.web")
STREAMS = ("stream1", "stream2")


class NoFreePorts(Exception):
    pass


def assign_ports(taken, start, end, rng=None):
    """Two unused external ports in [start, end], at random and never adjacent (not guessable, not sequential)."""
    rng = rng or random.SystemRandom()
    free = [port for port in range(start, end + 1) if port not in taken]
    rng.shuffle(free)
    for first in free:
        others = [port for port in free if abs(port - first) > 1]
        if others:
            return first, rng.choice(others)
    raise NoFreePorts("no free external ports left in the configured range")


def context_for(camera, vault, settings, youtube=None):
    """The CameraContext the recording code needs, built from a database row (secrets decrypted here only)."""
    return CameraContext(
        name=f"camera-{camera['id']}",
        host=camera["host"],
        port=camera["port"],
        user=vault.decrypt(camera["username_enc"]),
        password=vault.decrypt(camera["password_enc"]),
        stream=camera["stream"],
        onvif_host=camera["host"],
        onvif_port=camera["onvif_port"],
        data_dir=settings.data_dir / "users" / str(camera["user_id"]) / f"camera-{camera['id']}",
        upload_at=settings.upload_at,
        youtube=youtube,
    )


def safe_context(camera, vault, settings, youtube=None, resolver=socket.getaddrinfo):
    """Like context_for, but the host is resolved and checked now and the context connects to that exact IP,
    so the name cannot point somewhere private between the check and the connection. Raises UnsafeHost."""
    ip = checked_host(camera["host"], settings.allow_private_hosts, resolver)
    return dataclasses.replace(context_for(camera, vault, settings, youtube), host=ip, onvif_host=ip)


@dataclass
class ProbeResult:
    tcp: bool
    rtsp: bool
    onvif: bool
    detail: str

    @property
    def ok(self):
        return self.tcp and self.rtsp and self.onvif


def probe_camera(ctx, runner=subprocess.run, connect=socket.create_connection, onvif=None):
    """Check, one stage at a time, that the server can reach the camera: the RTSP port, the video stream, ONVIF."""
    notes = []
    tcp = rtsp = onvif_ok = False
    try:
        with connect((ctx.host, ctx.port), timeout=5):
            tcp = True
    except OSError as err:
        notes.append(f"The video port {ctx.port} does not answer ({type(err).__name__}). Check the port forwarding rule.")
    if tcp:
        try:
            done = runner(["ffprobe", "-v", "error", "-rtsp_transport", "tcp", "-show_entries", "stream=codec_name",
                           "-of", "default=nw=1", ctx.rtsp_url()],
                          capture_output=True, text=True, errors="replace", timeout=25)
            rtsp = done.returncode == 0 and "codec_name" in done.stdout
            if not rtsp:
                notes.append("The video stream could not be read: " + (done.stderr or "no details").strip()[-200:])
        except subprocess.TimeoutExpired:
            notes.append("Reading the video stream timed out.")
        except OSError as err:
            notes.append(f"ffprobe could not run on the server ({type(err).__name__}).")
    if onvif is None:
        import tapo_motion
        onvif = tapo_motion.subscribe
    try:
        onvif(ctx)
        onvif_ok = True
    except Exception as err:
        notes.append(f"Motion events (ONVIF, port {ctx.onvif_port}) failed: {str(err)[-200:]}")
    detail = "; ".join(notes) if notes else "Everything answers."
    return ProbeResult(tcp, rtsp, onvif_ok, ctx.scrub(detail))


def register(app):
    state = app.extensions["tapo"]
    settings, db, vault = state["settings"], state["db"], state["vault"]
    state.setdefault("probe", probe_camera)

    def me():
        return app.current_user()

    def owned(camera_id):
        camera = db.camera_for_user(me()["id"], camera_id)
        if camera is None:
            abort(404)
        return camera

    def read_form(require_password):
        form = request.form
        name, host = form.get("name", "").strip(), form.get("host", "").strip()
        user, password = form.get("camera_user", "").strip(), form.get("camera_password", "")
        stream = form.get("stream", "stream1")
        if not 1 <= len(name) <= 60:
            return None, "Give the camera a name of up to 60 characters."
        if stream not in STREAMS:
            return None, "Choose stream1 or stream2."
        if not user or (require_password and not password):
            return None, "Enter the camera account username and password (created in the Tapo app)."
        try:
            assert_public_host(host, settings.allow_private_hosts, state["resolver"])
        except UnsafeHost as err:
            return None, f"That address is not allowed: {err}."
        return {"name": name, "host": host, "user": user, "password": password, "stream": stream}, None

    def form_page(error=None, status=200, camera=None):
        return render_template("camera_form.html", error=error, camera=camera), status

    @app.get("/cameras")
    @app.login_required
    def cameras_index():
        return redirect("/")

    @app.get("/cameras/new")
    @app.login_required
    def camera_new():
        return form_page()

    @app.post("/cameras/new")
    @app.login_required
    def camera_create():
        user = me()
        if len(db.cameras_for_user(user["id"])) >= settings.max_cameras_per_user:
            return form_page(f"You can have at most {settings.max_cameras_per_user} camera(s).", 400)
        if db.camera_count() >= settings.max_cameras_total:
            return form_page("This server cannot take more cameras right now. Ask the administrator.", 400)
        data, error = read_form(require_password=True)
        if error:
            return form_page(error, 400)
        try:
            rtsp_port, onvif_port = assign_ports(db.camera_ports(), *settings.port_range)
        except NoFreePorts:
            return form_page("No free ports are left on this server. Ask the administrator.", 400)
        camera_id = db.add_camera(user["id"], data["name"], data["host"], rtsp_port, onvif_port, data["stream"],
                                  vault.encrypt(data["user"]), vault.encrypt(data["password"]))
        return redirect(f"/cameras/{camera_id}/guide")

    @app.get("/cameras/<int:camera_id>")
    @app.login_required
    def camera_view(camera_id):
        return render_template("camera_view.html", camera=owned(camera_id), result=None)

    @app.post("/cameras/<int:camera_id>")
    @app.login_required
    def camera_update(camera_id):
        camera = owned(camera_id)
        data, error = read_form(require_password=False)
        if error:
            return render_template("camera_view.html", camera=camera, result=None, error=error), 400
        fields = {"name": data["name"], "host": data["host"], "stream": data["stream"],
                  "username_enc": vault.encrypt(data["user"]), "enabled": 0, "status": "changed"}
        if data["password"]:
            fields["password_enc"] = vault.encrypt(data["password"])
        db.set_camera_status(camera_id, **fields)  # changed settings must pass the connection test again
        flash("Saved. Run the connection test again before recording starts.", "info")
        return redirect(f"/cameras/{camera_id}")

    @app.get("/cameras/<int:camera_id>/guide")
    @app.login_required
    def camera_guide(camera_id):
        camera = owned(camera_id)
        guide = build_guide(settings, camera["host"], camera["port"], camera["onvif_port"])
        return render_template("camera_guide.html", camera=camera, guide=guide)

    @app.post("/cameras/<int:camera_id>/test")
    @app.login_required
    def camera_test(camera_id):
        camera = owned(camera_id)
        try:
            ctx = safe_context(camera, vault, settings, resolver=state["resolver"])
            result = state["probe"](ctx)
        except UnsafeHost as err:
            result = ProbeResult(False, False, False, f"That address is not allowed: {err}.")
        if result.ok:
            db.set_camera_status(camera_id, enabled=1, status="ready", last_error=None)
        else:
            db.set_camera_status(camera_id, enabled=0, status="unreachable", last_error=result.detail[:300])
        return render_template("camera_view.html", camera=db.camera_for_user(me()["id"], camera_id), result=result)

    @app.post("/cameras/<int:camera_id>/delete")
    @app.login_required
    def camera_delete(camera_id):
        owned(camera_id)
        db.delete_camera(me()["id"], camera_id)
        flash("Camera deleted.", "info")
        return redirect("/")
