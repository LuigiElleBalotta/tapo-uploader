"""OneDrive through Microsoft Graph, limited to the app's own folder (`Files.ReadWrite.AppFolder`).

The app never sees the rest of the user's OneDrive. Uploads use an upload session, so a file appears only when the
last chunk has arrived. The login header goes to Graph only: upload and download addresses are pre-authorised
and are called without it. Redirects are never followed blindly: a download redirect is read and called once.
"""
import http.client
import json
import os
import re
import socket
import ssl
import time
from urllib.parse import quote, urlencode, urlsplit

from .base import CHUNK, DAY_RE, Storage, StorageError, StoredItem, day_folder, safe_filename, sidecar_name

GRAPH_URL = "https://graph.microsoft.com/v1.0"
LOGIN_URL = "https://login.microsoftonline.com"
SCOPE = "Files.ReadWrite.AppFolder offline_access"
UPLOAD_CHUNK = 10 * 1024 * 1024        # Graph wants a multiple of 320 KiB
_ANOTHER_LOGIN = "Microsoft no longer accepts the saved OneDrive access: connect OneDrive again"


def _open_source(path):
    return open(path, "rb")


def _when(text):
    return (text or "")[:16].replace("T", " ")


class OneDriveStorage(Storage):
    kind = "onedrive"

    def __init__(self, client_id, client_secret, refresh_token, prefix, folder="", tenant="common", on_token=None,
                 graph_url=GRAPH_URL, login_url=LOGIN_URL, chunk=UPLOAD_CHUNK, timeout=60):
        if not (client_id and client_secret and refresh_token):
            raise StorageError("OneDrive is not connected")
        segments = [seg for seg in (folder or "").split("/") if seg]
        if any(seg in (".", "..") for seg in segments):
            raise StorageError("the OneDrive folder cannot contain '.' or '..'")
        self.client_id, self.client_secret, self.refresh_token = client_id, client_secret, refresh_token
        self.prefix, self.folder, self.tenant, self.on_token = prefix, segments, tenant or "common", on_token
        self.graph_url, self.login_url = graph_url.rstrip("/"), login_url.rstrip("/")
        self.chunk, self.timeout = chunk, timeout
        self._access, self._expires = None, 0.0
        self._day_re = re.compile(rf"^{re.escape(prefix)} ({DAY_RE.pattern})$")

    # ----- http -----

    def _send(self, method, url, body=None, headers=None):
        """One request to `url`; returns (connection, response). Nothing is followed."""
        parts = urlsplit(url)
        if parts.scheme != urlsplit(self.graph_url).scheme or not parts.hostname:
            raise StorageError("Microsoft sent an address that cannot be used")
        port = parts.port or (443 if parts.scheme == "https" else 80)
        # addresses Microsoft sends can hold raw spaces; '%' stays safe so already-encoded parts are not encoded twice
        target = quote(parts.path, safe="/%:@!$&'()*+,;=~-._") + (f"?{parts.query}" if parts.query else "")
        try:
            if parts.scheme == "https":
                conn = http.client.HTTPSConnection(parts.hostname, port, timeout=self.timeout,
                                                   context=ssl.create_default_context())
            else:
                conn = http.client.HTTPConnection(parts.hostname, port, timeout=self.timeout)
            try:
                conn.request(method, target, body=body, headers=headers or {})
                return conn, conn.getresponse()
            except BaseException:
                conn.close()
                raise
        except (OSError, http.client.HTTPException, socket.timeout) as err:
            raise StorageError(f"cannot reach OneDrive: {err}") from err

    def _read(self, method, url, body=None, headers=None):
        conn, response = self._send(method, url, body, headers)
        try:
            data = response.read()
        except (OSError, http.client.HTTPException) as err:
            raise StorageError(f"cannot reach OneDrive: {err}") from err
        finally:
            conn.close()
        return response.status, dict(response.getheaders()), data

    # ----- login -----

    def _token(self, force=False):
        if self._access and not force and time.time() < self._expires - 60:
            return self._access
        form = urlencode({"client_id": self.client_id, "client_secret": self.client_secret,
                          "grant_type": "refresh_token", "refresh_token": self.refresh_token, "scope": SCOPE}).encode()
        status, _, data = self._read("POST", f"{self.login_url}/{quote(self.tenant, safe='')}/oauth2/v2.0/token", form,
                                     {"Content-Type": "application/x-www-form-urlencoded", "Content-Length": str(len(form))})
        try:
            answer = json.loads(data or b"{}")
        except ValueError:
            answer = {}
        if status in (400, 401) or answer.get("error") == "invalid_grant":
            raise StorageError(_ANOTHER_LOGIN)   # Microsoft's own text is not shown: it can carry details of the app
        if status != 200 or not answer.get("access_token"):
            raise StorageError(f"Microsoft's login answered HTTP {status}")
        self._access = answer["access_token"]
        self._expires = time.time() + int(answer.get("expires_in") or 300)
        new = answer.get("refresh_token")
        if new and new != self.refresh_token:
            self.refresh_token = new
            if self.on_token is not None:
                self.on_token(new)
        return self._access

    def _graph(self, method, path, ok, body=None, headers=None):
        """A Graph call with the access token; a refused token is renewed once. Returns (status, headers, data)."""
        payload = None if body is None else (body if isinstance(body, bytes) else json.dumps(body).encode())
        for attempt in (0, 1):
            head = {"Authorization": f"Bearer {self._token(force=attempt == 1)}", **(headers or {})}
            if payload is not None:
                head.setdefault("Content-Type", "application/json")
                head["Content-Length"] = str(len(payload))
            status, response_headers, data = self._read(method, self.graph_url + path, payload, head)
            if status != 401:
                break
        if status == 401:
            raise StorageError(_ANOTHER_LOGIN)
        if status not in ok:
            raise StorageError(f"OneDrive answered HTTP {status} to {method}")
        return status, response_headers, data

    # ----- paths (always under the app folder) -----

    @staticmethod
    def _item(*segments):
        if not segments:
            return "/me/drive/special/approot"
        return "/me/drive/special/approot:" + "".join("/" + quote(seg, safe="") for seg in segments) + ":"

    def _mkdirs(self, segments):
        self._graph("GET", self._item(), ok={200})          # opening the app folder creates it the first time
        for end in range(1, len(segments) + 1):
            parent = self._item(*segments[:end - 1]) + "/children"
            self._graph("POST", parent, ok={201, 409},
                        body={"name": segments[end - 1], "folder": {}, "@microsoft.graph.conflictBehavior": "fail"})

    # ----- Storage -----

    def connect(self):
        self._token()

    def check(self):
        self._mkdirs(self.folder)
        probe = self._item(*self.folder, ".tapo-probe")
        self._graph("PUT", probe + "/content", ok={200, 201}, body=b"ok", headers={"Content-Type": "application/octet-stream"})
        self._graph("DELETE", probe, ok={204, 404})

    def _upload(self, path, name, segments):
        """Send the file; returns (size, the driveItem Graph answered with)."""
        size = os.path.getsize(path)
        target = self._item(*segments, name)
        if size == 0:   # an upload session cannot carry an empty file
            _, _, data = self._graph("PUT", target + "/content", ok={200, 201}, body=b"",
                                     headers={"Content-Type": "application/octet-stream"})
            return size, json.loads(data or b"{}")
        _, _, data = self._graph("POST", target + "/createUploadSession", ok={200},
                                 body={"item": {"@microsoft.graph.conflictBehavior": "replace", "name": name}})
        upload_url = json.loads(data or b"{}").get("uploadUrl")
        if not upload_url:
            raise StorageError("OneDrive did not give an upload address")
        try:
            with _open_source(path) as handle:
                sent, last = 0, {}
                while sent < size:
                    block = handle.read(min(self.chunk, size - sent))
                    status, _, answer = self._read("PUT", upload_url, block, {
                        "Content-Length": str(len(block)), "Content-Range": f"bytes {sent}-{sent + len(block) - 1}/{size}"})
                    if status not in (200, 201, 202):
                        raise StorageError(f"OneDrive answered HTTP {status} while uploading")
                    sent += len(block)
                    if status in (200, 201):
                        last = json.loads(answer or b"{}")
            if not last:
                raise StorageError("OneDrive did not confirm the upload")
            return size, last
        except StorageError:
            try:
                self._read("DELETE", upload_url)        # cancel: Microsoft throws the half-sent file away
            except StorageError:
                pass
            raise
        except OSError as err:
            raise StorageError(f"cannot read {path}: {err.strerror or err}") from err

    def put(self, path, day, title, description=""):
        name = safe_filename(title)
        segments = [*self.folder, day_folder(self.prefix, day)]
        self._mkdirs(segments)
        size, item = self._upload(path, name, segments)
        if description:
            self._graph("PUT", self._item(*segments, sidecar_name(name)) + "/content", ok={200, 201},
                        body=description.encode("utf-8"), headers={"Content-Type": "text/plain; charset=utf-8"})
        return StoredItem(name, day, size, _when(item.get("lastModifiedDateTime")), item.get("webUrl"))

    def _children(self, *segments):
        path = self._item(*segments) + "/children?$top=200&$select=name,size,lastModifiedDateTime,webUrl,folder,file"
        found = []
        while path:
            try:
                _, _, data = self._graph("GET", path, ok={200})
            except StorageError as err:
                if "HTTP 404" in str(err):
                    return []
                raise
            answer = json.loads(data or b"{}")
            found += answer.get("value", [])
            link = answer.get("@odata.nextLink")
            path = link[len(self.graph_url):] if link and link.startswith(self.graph_url) else None
        return found

    def days(self, limit=10):
        names = [m.group(1) for e in self._children(*self.folder) if "folder" in e and (m := self._day_re.match(e["name"]))]
        return sorted(names, reverse=True)[:limit]

    def list_day(self, day):
        items = [StoredItem(e["name"], day, e.get("size"), _when(e.get("lastModifiedDateTime")), e.get("webUrl"))
                 for e in self._children(*self.folder, day_folder(self.prefix, day))
                 if "file" in e and e["name"].endswith(".mp4")]
        return sorted(items, key=lambda item: item.name)

    def open(self, day, name, start=0, end=None):
        if name != name.rsplit("/", 1)[-1] or "\\" in name or name in ("", ".", ".."):
            raise StorageError("no such recording")
        _, headers, _ = self._graph("GET", self._item(*self.folder, day_folder(self.prefix, day), name) + "/content", ok={302})
        location = next((v for k, v in headers.items() if k.lower() == "location"), None)
        if not location:
            raise StorageError("OneDrive did not give a download address")
        conn, response = self._send("GET", location, headers={"Range": f"bytes={start}-{'' if end is None else end}"})
        if response.status not in (200, 206):
            conn.close()
            raise StorageError(f"OneDrive answered HTTP {response.status} to the download")
        if response.status == 206:
            total, skip = int(response.getheader("Content-Range", "/0").rsplit("/", 1)[-1]), 0
        else:  # the server ignored the Range header
            total, skip = int(response.getheader("Content-Length") or 0), start
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
                raise StorageError(f"the OneDrive download broke off: {err}") from err
            finally:
                conn.close()

        return chunks(), total
