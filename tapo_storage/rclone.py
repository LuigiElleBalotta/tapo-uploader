"""Any rclone remote (Mega, S3-compatible, Dropbox, OneDrive, ...) through the `rclone` binary.

Every call is an argument list, never a shell string. The remote and its credentials live in an rclone config
file the server administrator controls: users cannot pick this destination.
"""
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

from .base import CHUNK, DAY_RE, Storage, StorageError, StoredItem, day_folder, safe_filename, sidecar_name

REMOTE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")
NOT_FOUND = 3  # rclone's exit code for "directory not found"


class RcloneStorage(Storage):
    kind = "rclone"

    def __init__(self, remote, config, prefix, folder="", run=subprocess.run, popen=subprocess.Popen, binary="rclone"):
        if not REMOTE_RE.match(remote or ""):
            raise StorageError("the rclone remote name may only contain letters, digits, '.', '_' and '-'")
        segments = [seg for seg in (folder or "").split("/") if seg]
        if any(seg in (".", "..") for seg in segments):
            raise StorageError("the rclone folder cannot contain '.' or '..'")
        self.remote, self.config, self.prefix = remote, Path(config) if config else None, prefix
        self.folder, self.run, self.popen, self.binary = segments, run, popen, binary
        self._day_re = re.compile(rf"^{re.escape(prefix)} ({DAY_RE.pattern})$")

    # ----- rclone -----

    def _command(self, *args):
        return [self.binary, *(["--config", str(self.config)] if self.config else []), *args]

    def _target(self, *segments):
        return f"{self.remote}:" + "/".join([*self.folder, *segments])

    @staticmethod
    def _first_line(text):
        lines = (text or "").strip().splitlines()
        return lines[0] if lines else "no details"

    def _rclone(self, *args, ok=(0,), timeout=None):
        try:
            done = self.run(self._command(*args), capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError as err:
            raise StorageError("rclone is not installed on the server") from err
        except subprocess.TimeoutExpired as err:
            raise StorageError("rclone took too long and was stopped") from err
        if done.returncode not in ok:
            raise StorageError(f"rclone failed: {self._first_line(done.stderr)}")
        return done

    # ----- Storage -----

    def check(self):
        try:
            self._rclone("lsd", f"{self.remote}:", timeout=120)
        except StorageError as err:
            if "not installed" in str(err):
                raise
            raise StorageError(f"the rclone remote '{self.remote}' is not usable: "
                               f"{str(err).removeprefix('rclone failed: ')}") from err

    def put(self, path, day, title, description=""):
        name = safe_filename(title)
        folder = day_folder(self.prefix, day)
        part, final = self._target(folder, name + ".part"), self._target(folder, name)
        try:
            size = os.path.getsize(path)
        except OSError as err:
            raise StorageError(f"cannot read {path}: {err.strerror or err}") from err
        try:
            self._rclone("copyto", str(path), part)
            self._rclone("moveto", part, final)
        except StorageError:
            try:
                self._rclone("deletefile", part, ok=(0, NOT_FOUND), timeout=120)
            except StorageError:
                pass
            raise
        if description:
            with tempfile.TemporaryDirectory() as tmp:
                note = Path(tmp) / "chapters.txt"
                note.write_text(description, encoding="utf-8")
                self._rclone("copyto", str(note), self._target(folder, sidecar_name(name)))
        return StoredItem(name, day, size, "", None)

    def _lsjson(self, *segments):
        done = self._rclone("lsjson", self._target(*segments), ok=(0, NOT_FOUND), timeout=300)
        if done.returncode == NOT_FOUND:
            return []
        try:
            return json.loads(done.stdout or "[]")
        except ValueError as err:
            raise StorageError("rclone sent an answer that cannot be read") from err

    @staticmethod
    def _when(text):
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).strftime("%Y-%m-%d %H:%M")
        except (AttributeError, ValueError):
            return ""

    def days(self, limit=10):
        found = [m.group(1) for e in self._lsjson() if e.get("IsDir") and (m := self._day_re.match(e.get("Name", "")))]
        return sorted(found, reverse=True)[:limit]

    def list_day(self, day):
        items = [StoredItem(e["Name"], day, e.get("Size"), self._when(e.get("ModTime")), None)
                 for e in self._lsjson(day_folder(self.prefix, day))
                 if not e.get("IsDir") and e.get("Name", "").endswith(".mp4")]
        return sorted(items, key=lambda item: item.name)

    def open(self, day, name, start=0, end=None):
        if name != Path(name).name or "\\" in name or name in ("", ".", ".."):
            raise StorageError("no such recording")
        match = [item for item in self.list_day(day) if item.name == name]
        if not match or match[0].size is None:
            raise StorageError("no such recording")
        total = match[0].size
        last = total - 1 if end is None else min(end, total - 1)
        command = self._command("cat", "--offset", str(start), "--count", str(last - start + 1),
                                self._target(day_folder(self.prefix, day), name))
        try:
            proc = self.popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except FileNotFoundError as err:
            raise StorageError("rclone is not installed on the server") from err

        def chunks():
            try:
                while True:
                    data = proc.stdout.read(CHUNK)
                    if not data:
                        break
                    yield data
            finally:
                proc.stdout.close()
                proc.kill()
                proc.wait()

        return chunks(), total
