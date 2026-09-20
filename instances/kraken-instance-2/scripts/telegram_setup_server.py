#!/usr/bin/env python3
"""Local-only Telegram credential catcher. Writes to data/.telegram_setup.json then exits."""
from __future__ import annotations
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".telegram_setup.json"
HOST, PORT = "127.0.0.1", 8765

HTML = """<!DOCTYPE html>
<html><head><meta charset='utf-8'><title>CruzBot Telegram Setup</title>
<style>
body{font-family:system-ui,sans-serif;max-width:420px;margin:40px auto;padding:0 16px}
label{display:block;margin:12px 0 4px;font-weight:600}
input{width:100%;padding:10px;font-size:16px;box-sizing:border-box}
button{margin-top:16px;padding:12px 16px;font-size:16px;width:100%}
.hint{color:#555;font-size:14px;margin-top:8px}
</style></head><body>
<h1>CruzBot Telegram</h1>
<p class="hint">Paste BotFather token + your numeric chat id. Saved only on this computer.</p>
<form method="POST" action="/save">
<label for="token">Bot token</label>
<input id="token" name="token" type="password" autocomplete="off" required placeholder="123456:ABC...">
<label for="chat_id">Chat id</label>
<input id="chat_id" name="chat_id" type="text" autocomplete="off" required placeholder="123456789">
<button type="submit">Save &amp; test</button>
</form>
</body></html>
"""

class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        return

    def do_GET(self):
        body = HTML.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(n).decode("utf-8", "replace")
        qs = parse_qs(raw)
        token = (qs.get("token") or [""])[0].strip()
        chat_id = (qs.get("chat_id") or [""])[0].strip()
        OUT.write_text(json.dumps({"token": token, "chat_id": chat_id}))
        ok = bool(token and chat_id)
        msg = b"<html><body><h2>Saved.</h2><p>You can close this tab. CruzBot will finish setup.</p></body></html>" if ok else b"<html><body><h2>Missing fields</h2></body></html>"
        self.send_response(200 if ok else 400)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(msg)))
        self.end_headers()
        self.wfile.write(msg)

if __name__ == "__main__":
    if OUT.exists():
        OUT.unlink()
    httpd = HTTPServer((HOST, PORT), H)
    print(f"listening on http://{HOST}:{PORT}", flush=True)
    httpd.handle_request()  # one GET
    httpd.handle_request()  # one POST
    print("done", flush=True)
