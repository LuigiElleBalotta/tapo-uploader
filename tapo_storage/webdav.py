"""WebDAV (Nextcloud, ownCloud, Synology, ...) over plain http.client.

Redirects are never followed: a PUT that is redirected would otherwise be re-sent somewhere nobody checked.
The connection goes to the address `host_check` returned, while the Host header keeps the original name.
"""
import base64
import http.client
import os
import re
import socket
import ssl
from datetime import timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, unquote, urlsplit
from xml.etree import ElementTree

from .base import CHUNK, DAY_RE, Storage, StorageError, StoredItem, day_folder, safe_filename, sidecar_name

DAV = "{DAV:}"
PROPFIND_BODY = ('<?xml version="1.0"?><propfind xmlns="DAV:"><prop><getcontentlength/><getlastmodified/>'
                 "<resourcetype/></prop></propfind>")


def _open_source(path):
    return open(path, "rb")


class WebDAVStorage(Storage):
    kind = "webdav"

    def __init__(self, url, user, password, prefix, folder="", host_check=None, timeout=60):
        parts = urlsplit(url or "")
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise StorageError("the WebDAV address must start with http:// or https://")
        segments = [seg for seg in (folder or "").split("/") if seg]
        if any(seg in (".", "..") for seg in segments):
            raise StorageError("the WebDAV folder cannot contain '.' or '..'")
        try:
            port = parts.port
        except ValueError as err:
            raise StorageError("the WebDAV address has an invalid port") from err
        self.scheme, self.host = parts.scheme, parts.hostname
        self.port = port or (443 if parts.scheme == "https" else 80)
        self.netloc = parts.netloc.rsplit("@", 1)[-1]
        self.base = parts.path.rstrip("/")   # already percent-encoded by whoever typed it
        self.folder, self.prefix = segments, prefix
        self.host_check, self.timeout = host_check, timeout
        self._auth = "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()
        self._day_re = re.compile(rf"^{re.escape(prefix)} ({DAY_RE.pattern})$")

    # ----- paths -----

    def _path(self, *segments):
        return self.base + "".join("/" + quote(seg, safe="") for seg in segments)

    def _url(self, path):
        return f"{self.scheme}://{self.netloc}{path}"

    # ----- http -----

    def _connect(self):
        address = self.host
        if self.host_check is not None:
            try:
                address = self.host_check(self.host)
            except StorageError:
                raise
            except Exception as err:
                raise StorageError(str(err)) from err
        sock = socket.create_connection((address, self.port), self.timeout)
        if self.scheme == "https":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=self.host)
        conn = http.client.HTTPConnection(self.host, self.port, timeout=self.timeout)
        conn.sock = sock
        return conn

    def _open(self, method, path, body=None, headers=None):
        """Send a request; returns (connection, response). Redirects and refused logins raise here."""
        headers = {"Authorization": self._auth, **(headers or {})}
        try:
            conn = self._connect()
            try:
                conn.request(method, path, body=body, headers=headers)
                response = conn.getresponse()
            except BaseException:
                conn.close()
                raise
        except StorageError:
            raise
        except (OSError, http.client.HTTPException) as err:
            raise StorageError(f"cannot reach the WebDAV server: {err}") from err
        if 300 <= response.status < 400:
            conn.close()
            raise StorageError(f"the server redirected the request (HTTP {response.status}): "
                               "use the final address, often https://")
        if response.status in (401, 403):
            conn.close()
            raise StorageError(f"the WebDAV server refused the login (HTTP {response.status}): "
                               "check the user and the password")
        return conn, response

    def _do(self, method, path, ok, body=None, headers=None):
        conn, response = self._open(method, path, body, headers)
        try:
            data = response.read()
        except (OSError, http.client.HTTPException) as err:
            raise StorageError(f"cannot reach the WebDAV server: {err}") from err
        finally:
            conn.close()
        if response.status not in ok:
            hint = " (a folder is missing?)" if response.status == 409 else ""
            raise StorageError(f"the WebDAV server answered HTTP {response.status} to {method}{hint}")
        return response.status, data

    def _mkcols(self, segments):
        # always asked, never remembered: the folders may have been removed in the cloud since the last upload
        for end in range(1, len(segments) + 1):
            self._do("MKCOL", self._path(*segments[:end]), ok={201, 405})

    # ----- Storage -----

    def check(self):
        self._mkcols(self.folder)
        probe = self._path(*self.folder, ".tapo-probe")
        self._do("PUT", probe, ok={200, 201, 204}, body=b"ok", headers={"Content-Length": "2"})
        self._do("DELETE", probe, ok={200, 204, 404})

    def put(self, path, day, title, description=""):
        name = safe_filename(title)
        segments = [*self.folder, day_folder(self.prefix, day)]
        self._mkcols(segments)
        part, final = self._path(*segments, name + ".part"), self._path(*segments, name)
        try:
            size = os.path.getsize(path)
            with _open_source(path) as handle:
                self._do("PUT", part, ok={200, 201, 204}, body=handle, headers={"Content-Length": str(size)})
            self._do("MOVE", part, ok={201, 204}, headers={"Destination": self._url(final), "Overwrite": "T"})
            if description:
                text = description.encode("utf-8")
                self._do("PUT", self._path(*segments, sidecar_name(name)), ok={200, 201, 204}, body=text,
                         headers={"Content-Length": str(len(text)), "Content-Type": "text/plain; charset=utf-8"})
        except StorageError:
            try:
                self._do("DELETE", part, ok={200, 204, 404})
            except StorageError:
                pass
            raise
        except OSError as err:
            raise StorageError(f"cannot read {path}: {err.strerror or err}") from err
        return StoredItem(name, day, size, "", self._url(final))

    def _propfind(self, path):
        status, data = self._do("PROPFIND", path, ok={207, 404}, body=PROPFIND_BODY,
                                headers={"Depth": "1", "Content-Type": "application/xml"})
        if status == 404:
            return []
        try:
            root = ElementTree.fromstring(data)
        except ElementTree.ParseError as err:
            raise StorageError("the WebDAV server sent an answer that cannot be read") from err
        own = unquote(path).rstrip("/")
        entries = []
        for response in root.iter(DAV + "response"):
            href = unquote(urlsplit(response.findtext(DAV + "href") or "").path)
            if href.rstrip("/") == own:
                continue
            size = response.findtext(f".//{DAV}getcontentlength")
            entries.append({
                "name": href.rstrip("/").rsplit("/", 1)[-1],
                "dir": response.find(f".//{DAV}resourcetype/{DAV}collection") is not None,
                "size": int(size) if size and size.isdigit() else None,
                "modified": response.findtext(f".//{DAV}getlastmodified"),
            })
        return entries

    @staticmethod
    def _when(text):
        try:
            return parsedate_to_datetime(text).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M")
        except (TypeError, ValueError):
            return ""

    def days(self, limit=10):
        found = [m.group(1) for e in self._propfind(self._path(*self.folder)) if e["dir"]
                 and (m := self._day_re.match(e["name"]))]
        return sorted(found, reverse=True)[:limit]

    def list_day(self, day):
        segments = [*self.folder, day_folder(self.prefix, day)]
        items = [StoredItem(e["name"], day, e["size"], self._when(e["modified"]),
                            self._url(self._path(*segments, e["name"])))
                 for e in self._propfind(self._path(*segments)) if not e["dir"] and e["name"].endswith(".mp4")]
        return sorted(items, key=lambda item: item.name)

    def open(self, day, name, start=0, end=None):
        if name != name.rsplit("/", 1)[-1] or "\\" in name or name in ("", ".", ".."):
            raise StorageError("no such recording")
        path = self._path(*self.folder, day_folder(self.prefix, day), name)
        spec = f"bytes={start}-{'' if end is None else end}"
        conn, response = self._open("GET", path, headers={"Range": spec})
        if response.status not in (200, 206):
            conn.close()
            raise StorageError(f"the WebDAV server answered HTTP {response.status} to GET")
        if response.status == 206:
            total = int(response.getheader("Content-Range", "/0").rsplit("/", 1)[-1])
            skip = 0
        else:  # the server ignored the Range header
            total = int(response.getheader("Content-Length") or 0)
            skip = start
        want = (total - 1 if end is None else min(end, total - 1)) - start + 1

        def chunks():
            left, to_skip = want, skip
            try:
                while left > 0:
                    data = response.read(CHUNK)
                    if not data:
                        break
                    if to_skip:
                        drop = min(to_skip, len(data))
                        data, to_skip = data[drop:], to_skip - drop
                    data = data[:left]
                    left -= len(data)
                    if data:
                        yield data
            except (OSError, http.client.HTTPException) as err:
                raise StorageError(f"the WebDAV download broke off: {err}") from err
            finally:
                conn.close()

        return chunks(), total
