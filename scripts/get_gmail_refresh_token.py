from __future__ import annotations

import json
import os
from wsgiref.simple_server import make_server

from google_auth_oauthlib.flow import InstalledAppFlow


SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
]


def main() -> None:
    # OAuth libraries require HTTPS by default. Localhost is safe for this
    # one-time desktop OAuth exchange, so allow the local callback explicitly.
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

    client_id = os.getenv("GMAIL_CLIENT_ID")
    client_secret = os.getenv("GMAIL_CLIENT_SECRET")

    if not client_id or not client_secret:
        raise SystemExit(
            "Set GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET first, then run this script again."
        )

    client_config = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }

    flow = InstalledAppFlow.from_client_config(client_config, SCOPES)

    result = {}

    def app(environ, start_response):
        query = environ.get("QUERY_STRING", "")
        url = f"{flow.redirect_uri}?{query}"
        flow.fetch_token(authorization_response=url)
        result["credentials"] = flow.credentials
        start_response("200 OK", [("Content-Type", "text/plain; charset=utf-8")])
        return [b"Authorization complete. You can close this window and return to Codex."]

    with make_server("localhost", 0, app) as server:
        port = server.server_port
        flow.redirect_uri = f"http://localhost:{port}/"
        auth_url, _ = flow.authorization_url(prompt="consent", access_type="offline")
        print("\nOpen this URL in your browser and approve access:\n", flush=True)
        print(auth_url, flush=True)
        print("\nWaiting for Google authorization...\n", flush=True)
        server.handle_request()

    credentials = result["credentials"]

    print("\nAdd these values as GitHub Actions secrets:\n")
    print(f"GMAIL_CLIENT_ID={client_id}")
    print(f"GMAIL_CLIENT_SECRET={client_secret}")
    print(f"GMAIL_REFRESH_TOKEN={credentials.refresh_token}")
    print("\nToken details:")
    print(json.dumps({"scopes": credentials.scopes}, indent=2))


if __name__ == "__main__":
    main()
