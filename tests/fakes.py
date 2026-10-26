"""An in-process WebDAV server for tests: MKCOL, PUT, MOVE, DELETE, PROPFIND (Depth 1) and GET with Range."""
import base64
import threading
from email.utils import formatdate
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, unquote, urlsplit
from xml.sax.saxutils import escape


class FakeDAV:
    def __init__(self, mode="ok", user="u", password="p", base="/dav"):
        self.mode, self.user, self.password, self.base = mode, user, password, base
        self.files = {}          # decoded path -> bytes
        self.dirs = {base}       # decoded paths
        self.requests = []       # (method, raw path, headers dict)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args):
                pass

            def handle_one(self):
                length = int(self.headers.get("Content-Length") or 0)
                outer.requests.append((self.command, self.path, dict(self.headers)))
                token = base64.b64encode(f"{outer.user}:{outer.password}".encode()).decode()
                if self.headers.get("Authorization") != f"Basic {token}":
                    self.read(length)
                    return self.reply(401)
                path = unquote(urlsplit(self.path).path)
                getattr(outer, "do_" + self.command)(self, path, length)

            def read(self, length):
                return self.rfile.read(length) if length else b""

            def reply(self, status, body=b"", headers=None):
                self.send_response(status)
                for key, value in (headers or {}).items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_MKCOL = do_PUT = do_MOVE = do_DELETE = do_PROPFIND = do_GET = handle_one

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}{self.base}"

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def parent_exists(self, path):
        return path.rsplit("/", 1)[0] in self.dirs

    def do_MKCOL(self, h, path, length):
        h.read(length)
        if path in self.dirs:
            return h.reply(405)
        if not self.parent_exists(path):
            return h.reply(409)
        self.dirs.add(path)
        h.reply(201)

    def do_PUT(self, h, path, length):
        if self.mode == "redirect":
            h.read(length)
            return h.reply(301, headers={"Location": "http://127.0.0.1:1/elsewhere"})
        if self.mode == "drop_on_put":
            h.read(length // 2)
            h.close_connection = True
            h.connection.close()
            return
        body = h.read(length)
        if not self.parent_exists(path):
            return h.reply(409)
        existed = path in self.files
        self.files[path] = body
        h.reply(204 if existed else 201)

    def do_MOVE(self, h, path, length):
        h.read(length)
        target = unquote(urlsplit(h.headers["Destination"]).path)
        if path not in self.files:
            return h.reply(404)
        existed = target in self.files
        self.files[target] = self.files.pop(path)
        h.reply(204 if existed else 201)

    def do_DELETE(self, h, path, length):
        h.read(length)
        if self.files.pop(path, None) is not None or path in self.dirs:
            self.dirs.discard(path)
            return h.reply(204)
        h.reply(404)

    def do_PROPFIND(self, h, path, length):
        h.read(length)
        if path not in self.dirs:
            return h.reply(404)
        rows = [self.row(path, None)]
        prefix = path.rstrip("/") + "/"
        for directory in sorted(self.dirs):
            if directory != path and directory.startswith(prefix) and "/" not in directory[len(prefix):]:
                rows.append(self.row(directory + "/", None))
        for file, data in sorted(self.files.items()):
            if file.startswith(prefix) and "/" not in file[len(prefix):]:
                rows.append(self.row(file, len(data)))
        xml = '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">' + "".join(rows) + "</d:multistatus>"
        h.reply(207, xml.encode(), {"Content-Type": "application/xml"})

    def row(self, path, size):
        kind = "<d:resourcetype><d:collection/></d:resourcetype>" if size is None else "<d:resourcetype/>"
        length = "" if size is None else f"<d:getcontentlength>{size}</d:getcontentlength>"
        return (f"<d:response><d:href>{escape(quote(path))}</d:href><d:propstat><d:prop>{kind}{length}"
                f"<d:getlastmodified>{formatdate(1_800_000_000, usegmt=True)}</d:getlastmodified></d:prop>"
                f"<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")

    def do_GET(self, h, path, length):
        h.read(length)
        data = self.files.get(path)
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
        h.reply(200, data)
