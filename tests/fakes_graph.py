"""An in-process stand-in for Microsoft's login and Graph (app folder) endpoints, enough for the OneDrive storage."""
import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = re.compile(r"^/v1\.0/me/drive/special/approot(?::(?P<path>/.*?):)?(?P<tail>/[^:]*)?$")


class FakeGraph:
    def __init__(self, refresh_token="refresh-1", rotate=False, client_id="cid", client_secret="csecret"):
        self.refresh_token, self.rotate = refresh_token, rotate
        self.client_id, self.client_secret = client_id, client_secret
        self.mode = "ok"            # "revoked": the token endpoint refuses; "expire": every access token is rejected once
        self.access = 0
        self.rejected = False
        self.files, self.dirs = {}, {""}            # path under the app folder -> bytes ; folder paths ('' = the app folder)
        self.sessions = {}                           # upload id -> {"path", "size", "data"}
        self.requests = []                           # (method, path, has Authorization header)
        self.token_calls = []
        self.session_chunks = []                     # (start, end, total)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def body(self):
                length = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(length) if length else b""

            def reply(self, status, payload=b"", headers=None):
                if isinstance(payload, (dict, list)):
                    payload = json.dumps(payload).encode()
                    headers = {"Content-Type": "application/json", **(headers or {})}
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def handle_any(self):
                path = unquote(urlsplit(self.path).path)
                outer.requests.append((self.command, path, "Authorization" in self.headers))
                if path.endswith("/oauth2/v2.0/token"):
                    return outer.token(self)
                if path.startswith("/upload/") or path.startswith("/dl/"):
                    return outer.direct(self, path)
                if self.headers.get("Authorization") != f"Bearer access-{outer.access}" or (outer.mode == "expire" and not outer.rejected):
                    outer.rejected = True
                    self.body()
                    return self.reply(401, {"error": {"code": "InvalidAuthenticationToken"}})
                outer.graph(self, path)

            do_GET = do_POST = do_PUT = do_DELETE = handle_any

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def base(self):
        return f"http://127.0.0.1:{self.port}"

    @property
    def graph_url(self):
        return f"{self.base}/v1.0"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    # ----- login -----

    def token(self, h):
        form = {k: v[0] for k, v in parse_qs(h.body().decode()).items()}
        self.token_calls.append(form)
        if (self.mode == "revoked" or form.get("grant_type") != "refresh_token" or form.get("refresh_token") != self.refresh_token
                or form.get("client_id") != self.client_id or form.get("client_secret") != self.client_secret):
            return h.reply(400, {"error": "invalid_grant", "error_description": "AADSTS70000 secret detail"})
        self.access += 1
        answer = {"access_token": f"access-{self.access}", "expires_in": 3600, "token_type": "Bearer", "scope": form.get("scope", "")}
        if self.rotate:
            self.refresh_token = f"refresh-{self.access + 1}"
            answer["refresh_token"] = self.refresh_token
        h.reply(200, answer)

    # ----- graph -----

    def graph(self, h, path):
        match = ROOT.match(path)
        if not match:
            return h.reply(404, {"error": {"code": "itemNotFound"}})
        folder, tail = (match["path"] or "").strip("/"), match["tail"] or ""
        if tail == "" and h.command == "GET":
            if folder in self.dirs:
                return h.reply(200, {"name": folder.rsplit("/", 1)[-1] or "Apps", "folder": {}})
            if folder in self.files:
                return h.reply(200, self.item(folder))
            return h.reply(404, {"error": {"code": "itemNotFound"}})
        if tail == "/children" and h.command == "GET":
            if folder not in self.dirs:
                return h.reply(404, {"error": {"code": "itemNotFound"}})
            prefix = folder + "/" if folder else ""
            rows = [self.item(p) for p in sorted(self.dirs) if p and p.startswith(prefix) and "/" not in p[len(prefix):]]
            rows += [self.item(p) for p in sorted(self.files) if p.startswith(prefix) and "/" not in p[len(prefix):]]
            half = len(rows) // 2 if len(rows) > 1 else len(rows)
            if "page2" in urlsplit(h.path).query:
                return h.reply(200, {"value": rows[half:]})
            answer = {"value": rows[:half]}
            if half < len(rows):
                answer["@odata.nextLink"] = f"{self.graph_url}/me/drive/special/approot:/{folder}:/children?page2=1" if folder else \
                    f"{self.graph_url}/me/drive/special/approot/children?page2=1"
            return h.reply(200, answer)
        if tail == "/children" and h.command == "POST":
            body = json.loads(h.body() or b"{}")
            target = f"{folder}/{body['name']}" if folder else body["name"]
            if folder not in self.dirs:
                return h.reply(404, {"error": {"code": "itemNotFound"}})
            if target in self.dirs or target in self.files:
                return h.reply(409, {"error": {"code": "nameAlreadyExists"}})
            self.dirs.add(target)
            return h.reply(201, {"name": body["name"], "folder": {}})
        if tail == "/createUploadSession" and h.command == "POST":
            h.body()
            parent = folder.rsplit("/", 1)[0] if "/" in folder else ""
            if parent not in self.dirs:
                return h.reply(404, {"error": {"code": "itemNotFound"}})
            upload_id = f"u{len(self.sessions) + 1}"
            self.sessions[upload_id] = {"path": folder, "data": b"", "size": None}
            return h.reply(200, {"uploadUrl": f"{self.base}/upload/{upload_id}"})
        if tail == "/content" and h.command == "PUT":
            data = h.body()
            parent = folder.rsplit("/", 1)[0] if "/" in folder else ""
            if parent not in self.dirs:
                return h.reply(404, {"error": {"code": "itemNotFound"}})
            self.files[folder] = data
            return h.reply(201, self.item(folder))
        if tail == "/content" and h.command == "GET":
            if folder not in self.files:
                return h.reply(404, {"error": {"code": "itemNotFound"}})
            return h.reply(302, headers={"Location": f"{self.base}/dl/{folder}"})
        if tail == "" and h.command == "DELETE":
            h.body()
            if self.files.pop(folder, None) is None and folder not in self.dirs:
                return h.reply(404, {"error": {"code": "itemNotFound"}})
            self.dirs.discard(folder)
            return h.reply(204)
        h.reply(400, {"error": {"code": "invalidRequest"}})

    def item(self, path):
        name = path.rsplit("/", 1)[-1]
        if path in self.dirs:
            return {"name": name, "folder": {"childCount": 0}, "webUrl": f"https://onedrive.example/{name}"}
        return {"name": name, "size": len(self.files[path]), "lastModifiedDateTime": "2027-01-15T08:30:00Z",
                "file": {}, "webUrl": f"https://onedrive.example/{name}"}

    def direct(self, h, path):
        if path.startswith("/dl/"):
            data = self.files.get(path[4:])
            h.body()
            if data is None:
                return h.reply(404)
            spec = h.headers.get("Range")
            if spec and spec.startswith("bytes="):
                first, _, last = spec[6:].partition("-")
                start = int(first)
                end = min(int(last), len(data) - 1) if last else len(data) - 1
                if start >= len(data):
                    return h.reply(416)
                return h.reply(206, data[start:end + 1], {"Content-Range": f"bytes {start}-{end}/{len(data)}"})
            return h.reply(200, data)
        session = self.sessions.get(path[len("/upload/"):])
        if session is None:
            h.body()
            return h.reply(404)
        if h.command == "DELETE":
            self.sessions.pop(path[len("/upload/"):])
            return h.reply(204)
        match = re.match(r"^bytes (\d+)-(\d+)/(\d+)$", h.headers.get("Content-Range", ""))
        data = h.body()
        if self.mode == "break_upload":
            return h.reply(500, {"error": {"code": "generalException"}})
        if not match or int(match[1]) != len(session["data"]) or int(match[2]) - int(match[1]) + 1 != len(data):
            return h.reply(416, {"error": {"code": "invalidRange"}})
        self.session_chunks.append((int(match[1]), int(match[2]), int(match[3])))
        session["data"] += data
        if len(session["data"]) < int(match[3]):
            return h.reply(202, {"nextExpectedRanges": [f"{len(session['data'])}-"]})
        self.files[session["path"]] = session["data"]
        self.sessions.pop(path[len("/upload/"):])
        h.reply(201, self.item(session["path"]))
