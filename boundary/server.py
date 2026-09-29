"""Narrow authenticated HTTP interface on the private worker network."""
import argparse
import hmac
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import parse_qs, urlsplit
import xml.etree.ElementTree as ET
import httpx
from .gateway import Gateway
from .ledger import Ledger
from .policy import ALLOWED_MODEL, MAX_BODY_BYTES, BoundaryError, PolicyBlocked

SOURCES = {
    "/v1/sources/openalex": ("https://api.openalex.org/works", {"search", "per-page", "mailto", "filter"}),
    "/v1/sources/arxiv": ("https://export.arxiv.org/api/query",
                           {"search_query", "start", "max_results", "sortBy", "sortOrder"}),
}

# arXiv asks for at most one request per 3 seconds; the broker enforces the
# spacing so guests can never violate it no matter how many run at once.
_ARXIV_MIN_INTERVAL_S = 3.0
_ARXIV_MAX_RESULTS = 50
_ARXIV_MAX_BODY = 2_000_000
_ARXIV_UPSTREAM_TIMEOUT_S = 30
_ARXIV_SORT_BY = {"", "relevance", "lastUpdatedDate", "submittedDate"}
_ARXIV_SORT_ORDER = {"", "ascending", "descending"}
_arxiv_lock = threading.Lock()
_arxiv_last_upstream = 0.0  # monotonic; guarded by _arxiv_lock

_ATOM = "http://www.w3.org/2005/Atom"
_ARXIV_NS = "http://arxiv.org/schemas/atom"
_NS = {"a": _ATOM, "arxiv": _ARXIV_NS}
_ARXIV_VERSIONED_ID = re.compile(r"(\d+\.\d+)v(\d+)$")


def _collapse(text: str | None) -> str:
    return " ".join((text or "").split())


def _fetch_arxiv_upstream(url: str, params: dict) -> tuple[int, bytes]:
    """GET the arXiv API with stdlib urllib; returns (status, body).

    urllib's minimal wire behavior (Accept-Encoding: identity, Connection:
    close, default UA) passes arXiv's frontend where httpx's compressed
    keep-alive requests were 406'd from our egress on 2026-09-28 (same
    URLs, same source IP, minutes apart). Non-2xx surfaces as (status,
    body); only transport failure raises URLError.
    """
    full = url + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(full, timeout=_ARXIV_UPSTREAM_TIMEOUT_S) as resp:
            return resp.status, resp.read(_ARXIV_MAX_BODY + 1)
    except urllib.error.HTTPError as e:
        try:
            return e.code, e.read(_ARXIV_MAX_BODY + 1)
        except (OSError, ValueError):
            return e.code, b""
    # URLError (DNS, refused, timeout) propagates: caller maps it to 502.


def _arxiv_entries(atom: bytes) -> list[dict]:
    """Parse an arXiv Atom feed into JSON-safe entries.

    Raises ValueError on malformed feeds; entries without a title or id are
    dropped so one bad record never poisons the batch.
    """
    try:
        root = ET.fromstring(atom)
    except ET.ParseError as e:
        raise ValueError(f"malformed arxiv feed: {e}")
    if root.tag != f"{{{_ATOM}}}feed":
        raise ValueError("not an arxiv atom feed")
    out: list[dict] = []
    for e in root.findall("a:entry", _NS):
        title = _collapse(e.findtext(f"{{{_ATOM}}}title"))
        if not title:
            continue
        ident = (e.findtext(f"{{{_ATOM}}}id") or "").strip()
        bare = ident
        for prefix in ("https://arxiv.org/abs/", "http://arxiv.org/abs/"):
            if bare.startswith(prefix):
                bare = bare[len(prefix):]
                break
        bare = bare.strip()
        if not bare:
            continue
        m = _ARXIV_VERSIONED_ID.fullmatch(bare)
        aid, version = (m.group(1), m.group(2)) if m else (bare, "")
        authors = [_collapse(a.findtext(f"{{{_ATOM}}}name")) for a in e.findall("a:author", _NS)]
        authors = [a for a in authors if a][:10]
        published = (e.findtext(f"{{{_ATOM}}}published") or "")[:4]
        year = int(published) if published.isdigit() else None
        categories = [c.get("term", "") for c in e.findall("a:category", _NS)]
        categories = [c for c in categories if c]
        primary = e.find("arxiv:primary_category", _NS)
        abs_url = pdf_url = ""
        for link in e.findall("a:link", _NS):
            href = (link.get("href") or "").strip()
            if not href:
                continue
            if link.get("title") == "pdf":
                pdf_url = href
            elif link.get("rel") == "alternate" and not abs_url:
                abs_url = href
        if not abs_url:
            abs_url = f"https://arxiv.org/abs/{bare}"
        if not pdf_url:
            pdf_url = f"https://arxiv.org/pdf/{bare}"
        out.append({
            "id": aid,
            "version": version,
            "title": title,
            "abstract": _collapse(e.findtext(f"{{{_ATOM}}}summary")),
            "authors": authors,
            "year": year,
            "categories": categories,
            "primary_category": (primary.get("term", "") if primary is not None else ""),
            "doi": (e.findtext(f"{{{_ARXIV_NS}}}doi") or "").strip(),
            "url_abs": abs_url,
            "url_pdf": pdf_url,
        })
    return out


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

        def serve_arxiv(self, url, params):
            """Fetch one arXiv page: validated, politely spaced, parsed to JSON."""
            global _arxiv_last_upstream
            try:
                start = int(params.get("start", "0"))
                max_results = int(params.get("max_results", "25"))
            except (TypeError, ValueError):
                raise PolicyBlocked("Source parameters not allowed")
            if (start < 0 or not 1 <= max_results <= _ARXIV_MAX_RESULTS
                    or params.get("sortBy", "") not in _ARXIV_SORT_BY
                    or params.get("sortOrder", "") not in _ARXIV_SORT_ORDER):
                raise PolicyBlocked("Source parameters not allowed")
            with _arxiv_lock:
                wait = _ARXIV_MIN_INTERVAL_S - (time.monotonic() - _arxiv_last_upstream)
                if wait > 0:
                    time.sleep(wait)
                try:
                    status, content = _fetch_arxiv_upstream(url, params)
                except urllib.error.URLError:
                    status, content = 0, b""
                _arxiv_last_upstream = time.monotonic()
            if status != 200 or len(content) > _ARXIV_MAX_BODY:
                self.reply(status if status in (400, 429) else 502,
                           {"error": "source_unavailable"})
                return
            try:
                entries = _arxiv_entries(content)
            except ValueError:
                self.reply(502, {"error": "source_unavailable"})
                return
            self.reply(200, {"entries": entries})

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
                flat = {k: v[0] for k, v in params.items()}
                if path.path == "/v1/sources/arxiv":
                    self.serve_arxiv(url, flat)
                    return
                response = gateway.http.get(url, params={k:v[0] for k,v in params.items()},
                                            timeout=30)
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
