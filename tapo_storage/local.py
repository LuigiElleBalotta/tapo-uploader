"""A folder on the machine running the server."""
import os
import re
import shutil
from datetime import datetime
from pathlib import Path

from .base import CHUNK, DAY_RE, Storage, StorageError, StoredItem, day_folder, safe_filename, sidecar_name


def _when(path):
    return datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")


class LocalStorage(Storage):
    kind = "local"

    def __init__(self, root: Path, prefix: str):
        self.root, self.prefix = Path(root), prefix
        self._day_re = re.compile(rf"^{re.escape(prefix)} ({DAY_RE.pattern})$")

    def _folder(self, day):
        return self.root / day_folder(self.prefix, day)

    def check(self):
        probe = self.root / ".tapo-probe"
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            probe.write_text("ok")
            probe.unlink()
        except OSError as err:
            raise StorageError(f"cannot write to {self.root}: {err.strerror or err}") from err

    def put(self, path, day, title, description=""):
        name = safe_filename(title)
        folder = self._folder(day)
        part, final = folder / f"{name}.part", folder / name
        try:
            folder.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, part)
            os.replace(part, final)
            if description:
                (folder / sidecar_name(name)).write_text(description, encoding="utf-8")
        except OSError as err:
            part.unlink(missing_ok=True)
            raise StorageError(f"could not save {name}: {err.strerror or err}") from err
        return StoredItem(name, day, final.stat().st_size, _when(final), None)

    def days(self, limit=10):
        if not self.root.is_dir():
            return []
        found = [m.group(1) for entry in self.root.iterdir() if entry.is_dir() and (m := self._day_re.match(entry.name))]
        return sorted(found, reverse=True)[:limit]

    def list_day(self, day):
        folder = self._folder(day)
        if not folder.is_dir():
            return []
        return [StoredItem(p.name, day, p.stat().st_size, _when(p), None) for p in sorted(folder.glob("*.mp4"))]

    def open(self, day, name, start=0, end=None):
        path = self._folder(day) / name
        if name != Path(name).name or name in ("", ".", "..") or not path.is_file():
            raise StorageError("no such recording")
        total = path.stat().st_size
        last = total - 1 if end is None else min(end, total - 1)
        handle = open(path, "rb")

        def chunks():
            with handle:
                handle.seek(start)
                left = last - start + 1
                while left > 0:
                    data = handle.read(min(CHUNK, left))
                    if not data:
                        break
                    left -= len(data)
                    yield data

        return chunks(), total
