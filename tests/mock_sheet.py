"""A stand-in for the Apps Script web app: same protocol, same POST-then-redirect dance as Google."""
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import re


class MockSheet:
    def __init__(self, grid, key="k", sheets=None):
        self.grid = grid  # list of rows (lists of str), row 1 = grid[0]
        self.sheets = {"": grid, "Sheet1": grid, **(sheets or {})}  # other sheets of the table by name
        self.key = key
        self.pending = {}
        self.writes = []
        self.colors = {}  # row -> {"bg": [...], "fg": "#…"}, as the connector reports them
        self.version = 2
        self.requests = 0
        self.fail_gets = self.fail_posts = 0  # answer that many next requests with Google's 404 page
        server = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, obj, code=200):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _not_found(self):
                body = "<html><body>Не удалось открыть файл.</body></html>".encode("utf-8")
                self.send_response(404)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                u = urllib.parse.urlparse(self.path)
                q = {k: v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
                if u.path == "/echo":
                    return self._send(server.pending.pop(q.get("id"), {"ok": False, "error": "expired"}))
                server.requests += 1
                if server.fail_gets > 0:
                    server.fail_gets -= 1
                    return self._not_found()
                self._send(server.handle(q))

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                payload = json.loads(self.rfile.read(n) or b"{}")
                server.requests += 1
                if server.fail_posts > 0:
                    server.fail_posts -= 1
                    return self._not_found()
                token = str(len(server.pending) + len(server.writes) + 1)
                server.pending[token] = server.handle(payload)
                self.send_response(302)  # Google answers POSTs with a redirect to the result
                self.send_header("Location", f"/echo?id={token}")
                self.send_header("Content-Length", "0")
                self.end_headers()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/exec"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()

    def cell(self, row, col, sheet=""):
        grid = self.sheets[sheet]
        line = grid[row - 1] if row - 1 < len(grid) else []
        return line[col - 1] if col - 1 < len(line) else ""

    def set(self, row, col, value, sheet=""):
        grid = self.sheets[sheet]
        while len(grid) < row:
            grid.append([])
        line = grid[row - 1]
        while len(line) < col:
            line.append("")
        line[col - 1] = value

    def handle(self, p):
        if p.get("key") != self.key:
            return {"ok": False, "error": "wrong key"}
        link, cuts, note = int(p.get("link_col", 3)), int(p.get("cuts_col", 4)), int(p.get("note_col", 5))
        sheet = p.get("sheet") or ""
        if sheet not in self.sheets:
            return {"ok": False, "error": "no sheet: " + sheet}
        if p.get("action") == "ping":
            return {"ok": True, "version": self.version, "spreadsheet": "Mock", "sheet": "Sheet1", "last_row": len(self.grid)}
        if p.get("action") == "rows":
            rows = []
            for i in range(1, len(self.grid) + 1):
                m = re.search(r"(?:[?&]v=|youtu\.be/)([A-Za-z0-9_-]{11})", self.cell(i, link))
                if m:
                    rows.append({"row": i, "link": self.cell(i, link), "id": m.group(1),
                                 "cuts": self.cell(i, cuts), "note": self.cell(i, note)})
                    if self.version >= 2:
                        rows[-1].update(self.colors.get(i, {"bg": [], "fg": "#000000"}))
            return {"ok": True, "sheet": "Sheet1", "rows": rows} | ({"version": self.version} if self.version >= 2 else {})
        if p.get("action") == "write":
            for u in p.get("updates", []):
                self.writes.append(u)
                if u.get("cuts") is not None:
                    self.set(int(u["row"]), cuts, u["cuts"], sheet)
                if u.get("note") is not None:
                    self.set(int(u["row"]), note, u["note"], sheet)
            return {"ok": True, "written": len(p.get("updates", []))}
        return {"ok": False, "error": "unknown action"}
