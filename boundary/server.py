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


def make_server(gateway, access_token, address=("0.0.0.0", 8787)):
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
            if supplied.startswith("Bearer ") and hmac.compare_digest(
                    supplied, "Bearer " + access_token):
                return
            if supplied.startswith("Bearer ") and gateway.ledger.check_run_token(supplied[7:]):
                return
            raise PolicyBlocked("Request credential rejected")

        def handle_error(self, exc):
            if isinstance(exc, BoundaryError):
                body = {"error": exc.code, "message": str(exc)}
                if getattr(exc, "retry_after", None) is not None:
                    body["retry_after"] = exc.retry_after
                self.reply(exc.status, body)
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


def main(argv=None, state_dir="/state", clock=None):
    from .ledger import system_clock
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("init", "serve", "status", "preflight",
                                            "mint-token", "revoke-token"))
    parser.add_argument("--ttl", type=int, default=3600, help="run-token lifetime in seconds")
    parser.add_argument("--id", default="", help="run-token id for revoke-token")
    args = parser.parse_args(argv)
    state = Path(state_dir)
    path = state / "usage.sqlite3"
    access = state / "access.key"
    clock = clock or system_clock
    if args.command == "init":
        Ledger.initialize(path, clock)
        with access.open("x") as out:
            out.write(secrets.token_urlsafe(32))
        access.chmod(0o600)
        print("Persistent ledger initialized; ceiling 200000000 tokens / 86400 seconds")
        return
    ledger = Ledger(path, clock)  # missing or corrupt state never creates a fresh allowance
    if args.command == "status":
        print(json.dumps(ledger.status(), indent=2))
        return
    if args.command == "preflight":
        state = ledger.status()
        outcome = state["last_provider_outcome"] or {}
        if state["halted"]:
            print(json.dumps({"access": "blocked", "reason": state["halted"]}))
        elif outcome.get("kind") == "success":
            print(json.dumps({"access": "authorized", **outcome}))
        else:
            print(json.dumps({"access": "unknown",
                              "reason": "no provider request yet; live acceptance blocked"}))
        return 0 if not state["halted"] and outcome.get("kind") == "success" else 1
    if args.command == "mint-token":
        ident, secret = ledger.mint_run_token(args.ttl)
        print(f"{ident} {secret}")
        return
    if args.command == "revoke-token":
        print("revoked" if ledger.revoke_run_token(args.id) else "unknown")
        return
    key = os.environ.get("MUSE_API_KEY")
    if not key:
        raise SystemExit("Fresh MUSE_API_KEY required for the trusted service")
    gateway = Gateway(ledger, key)
    make_server(gateway, access.read_text().strip()).serve_forever()


if __name__ == "__main__":
    raise SystemExit(main())
