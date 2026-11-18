"""One-time Google OAuth login. Standard library only; no browser cookie access.

Run on the Mac. Open the printed URL in Chrome, approve consent, then import
the private output into Vault. The callback is loopback-only, uses state + PKCE,
and verifies that Google returned the exact requested Gmail account.
"""

import argparse
import base64
import hashlib
import json
import os
import secrets
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SCOPES = ["https://mail.google.com/"]


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client-secret", type=Path, required=True)
    parser.add_argument("--email", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    client = json.loads(args.client_secret.read_text())["installed"]
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    redirect = f"http://127.0.0.1:{args.port}/callback"
    outcome = {}
    opener = urllib.request.build_opener(NoRedirect())

    def api(url, data=None, token=None):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode() if data else None, headers=headers)
        with opener.open(req, timeout=30) as response:
            return json.load(response)

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Callback URLs contain an authorization code.

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            if parsed.path != "/callback" or not secrets.compare_digest(query.get("state", [""])[0], state):
                self.send_error(400, "Invalid OAuth state")
                return
            try:
                if "error" in query or "code" not in query:
                    raise ValueError("Google authorization was not granted")
                token = api("https://oauth2.googleapis.com/token", {
                    "code": query["code"][0], "client_id": client["client_id"],
                    "client_secret": client["client_secret"], "redirect_uri": redirect,
                    "grant_type": "authorization_code", "code_verifier": verifier,
                })
                granted = token.get("scope", "").split()
                if not set(SCOPES).issubset(granted) or not token.get("refresh_token"):
                    raise ValueError("Offline access or required mail scope was not granted")
                profile = api("https://gmail.googleapis.com/gmail/v1/users/me/profile", token=token["access_token"])
                if profile["emailAddress"].lower() != args.email.lower():
                    raise ValueError("Selected account does not match requested email")
                bundle = json.loads(args.output.read_text()) if args.output.exists() else {"accounts": {}}
                bundle["accounts"][args.email] = {
                    "type": "authorized_user", "client_id": client["client_id"],
                    "client_secret": client["client_secret"], "refresh_token": token["refresh_token"],
                    "token_uri": "https://oauth2.googleapis.com/token", "scopes": granted,
                }
                args.output.parent.mkdir(parents=True, exist_ok=True)
                fd, temporary = tempfile.mkstemp(dir=args.output.parent)
                with os.fdopen(fd, "w") as target:
                    json.dump(bundle, target)
                os.replace(temporary, args.output)
                outcome["ok"] = True
                text = "Gmail connected to Hermes. You can close this tab."
            except urllib.error.HTTPError as error:
                outcome["error"] = f"Google returned HTTP {error.code}; no credentials saved."
                text = outcome["error"]
            except Exception as error:
                outcome["error"] = str(error) if isinstance(error, ValueError) else "OAuth exchange failed; no credentials saved."
                text = outcome["error"]
            self.send_response(200 if outcome.get("ok") else 400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(text.encode())

    server = HTTPServer(("127.0.0.1", args.port), Callback)
    server.timeout = 1
    params = {
        "client_id": client["client_id"], "redirect_uri": redirect, "response_type": "code",
        "scope": " ".join(SCOPES), "access_type": "offline", "prompt": "consent",
        "login_hint": args.email, "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    }
    print("https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params), flush=True)
    deadline = time.monotonic() + 1800
    while not outcome and time.monotonic() < deadline:
        server.handle_request()
    server.server_close()
    if not outcome.get("ok"):
        raise SystemExit(outcome.get("error", "Authorization timed out"))
    print(f"Connected {args.email}; private credentials saved to {args.output}")


if __name__ == "__main__":
    main()
