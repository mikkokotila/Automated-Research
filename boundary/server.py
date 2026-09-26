"""Narrow authenticated HTTP interface on the private worker network."""
import argparse
import hmac
import json
import os
from pathlib import Path
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
import httpx
from .gateway import Gateway
from .ledger import Ledger
from .policy import ALLOWED_MODEL, MAX_BODY_BYTES, BoundaryError, PolicyBlocked

SOURCES = {
    "/v1/sources/openalex": ("https://api.openalex.org/works", {"search", "per-page", "mailto", "filter"}),
    "/v1/sources/semanticscholar": ("https://api.semanticscholar.org/graph/v1/paper/search",
                                      {"query", "limit", "fields", "year"}),
}


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def make_server(gateway, token, address=("0.0.0.0", 8787)):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # never record prompts, credentials, or provider error bodies

        def setup(self):
            super().setup()
            self.connection.settimeout(320)

        def reply(self, status, data):
            raw = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def authorized(self):
            supplied = self.headers.get("Authorization", "")
            if not hmac.compare_digest(supplied, "Bearer " + token):
                raise PolicyBlocked("Request credential rejected")

        def handle_error(self, exc):
            if isinstance(exc, BoundaryError):
                self.reply(exc.status, {"error": exc.code, "message": str(exc)})
            else:
                self.reply(503, {"error": "boundary_unavailable", "message": "Request failed closed"})

        def do_POST(self):
            try:
                self.authorized()
                if self.path != "/v1/complete":
                    raise PolicyBlocked("Endpoint not allowed")
                if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Encoding"):
                    raise PolicyBlocked("Encoded or chunked requests are not accepted")
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= MAX_BODY_BYTES or self.headers.get_content_type() != "application/json":
                    raise PolicyBlocked("Invalid request size or type")
                raw = self.rfile.read(size)
                if len(raw) != size:
                    raise PolicyBlocked("Incomplete request")
                value = json.loads(raw, object_pairs_hook=strict_object)
                self.reply(200, gateway.complete(value))
            except Exception as exc:
                self.handle_error(exc)

        def do_GET(self):
            try:
                self.authorized()
                path = urlsplit(self.path)
                if path.path == "/v1/status" and not path.query:
                    self.reply(200, {"model": ALLOWED_MODEL, **gateway.ledger.status()})
                    return
                if path.path not in SOURCES or len(self.path) > 8192:
                    raise PolicyBlocked("Endpoint not allowed")
                url, allowed = SOURCES[path.path]
                params = parse_qs(path.query, keep_blank_values=True)
                if not set(params) <= allowed or any(len(v) != 1 for v in params.values()):
                    raise PolicyBlocked("Source parameters not allowed")
                headers = {}
                if path.path.endswith("semanticscholar") and os.environ.get("SEMANTIC_SCHOLAR_API_KEY"):
                    headers["x-api-key"] = os.environ["SEMANTIC_SCHOLAR_API_KEY"]
                response = gateway.http.get(url, params={k:v[0] for k,v in params.items()},
                                            headers=headers, timeout=30)
                if response.status_code != 200:
                    self.reply(response.status_code if response.status_code in (400,429) else 502,
                               {"error": "source_unavailable"})
                    return
                self.reply(200, response.json())
            except Exception as exc:
                self.handle_error(exc)

    return ThreadingHTTPServer(address, Handler)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("init", "serve", "status"))
    args = parser.parse_args()
    path = Path("/state/usage.sqlite3")
    access = Path("/state/access.key")
    if args.command == "init":
        Ledger.initialize(path)
        with access.open("x") as out:
            out.write(secrets.token_urlsafe(32))
        access.chmod(0o600)
        print("Persistent ledger initialized; ceiling 200000000 tokens / 86400 seconds")
        return
    ledger = Ledger(path)  # missing or corrupt state never creates a fresh allowance
    if args.command == "status":
        print(json.dumps(ledger.status(), indent=2))
        return
    key = os.environ.get("MUSE_API_KEY")
    if not key:
        raise SystemExit("Fresh MUSE_API_KEY required for the trusted service")
    gateway = Gateway(ledger, key)
    make_server(gateway, access.read_text().strip()).serve_forever()


if __name__ == "__main__":
    main()
