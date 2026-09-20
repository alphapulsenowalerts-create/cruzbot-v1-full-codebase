#!/usr/bin/env python3
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / ".telegram_setup.json"
CHAT = "6949546419"
HTML = """<!DOCTYPE html>
<html><head><meta charset='utf-8'><title>CruzBot Telegram Token</title>
<style>
body{font-family:system-ui,sans-serif;max-width:420px;margin:40px auto;padding:0 16px}
label{display:block;margin:12px 0 4px;font-weight:600}
input{width:100%;padding:10px;font-size:16px;box-sizing:border-box}
button{margin-top:16px;padding:12px 16px;font-size:16px;width:100%}
.hint{color:#555;font-size:14px}
</style></head><body>
<h1>Replace Telegram token</h1>
<p class="hint">Paste the NEW BotFather token only. Chat id stays 6949546419.</p>
<form method="POST" action="/save">
<label for="token">New bot token</label>
<input id="token" name="token" type="password" autocomplete="off" required placeholder="123456:ABC...">
<input type="hidden" name="chat_id" value="6949546419">
<button type="submit">Save &amp; test</button>
</form>
</body></html>""".replace("6949546419", CHAT)
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        return
    def do_GET(self):
        b = HTML.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        qs = parse_qs(self.rfile.read(n).decode())
        token = (qs.get("token") or [""])[0].strip()
        chat_id = (qs.get("chat_id") or [CHAT])[0].strip() or CHAT
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps({"token": token, "chat_id": chat_id}))
        msg = b"<html><body><h2>Saved.</h2><p>Close this tab.</p></body></html>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(msg)))
        self.end_headers()
        self.wfile.write(msg)
if __name__ == "__main__":
    HTTPServer(("127.0.0.1", 8765), H).serve_forever()
