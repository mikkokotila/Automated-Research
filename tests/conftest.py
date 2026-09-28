"""Shared offline fixtures: no live credentials, network, or Docker required.

Tests marked `live` (real provider) or `containment` (Linux/Docker host checks)
are deselected by default; see pyproject addopts. Everything here runs anywhere.
"""
from __future__ import annotations

import ipaddress
import socket

import httpx
import pytest


class FakeMuse:
    """Scripted stand-in for the completer protocol (`.model`, `.complete`)."""

    model = "fake-muse"

    def __init__(self, default: str = "ok"):
        self.default = default
        self.scripts: dict[str, list[str]] = {}
        self.calls: list[tuple[str, str, int]] = []

    def script(self, system_substring: str, *responses: str) -> "FakeMuse":
        self.scripts.setdefault(system_substring, []).extend(responses)
        return self

    def complete(self, system: str, user: str, max_tokens: int = 8000) -> str:
        self.calls.append((system, user, max_tokens))
        for key, queue in self.scripts.items():
            if key in system and queue:
                return queue.pop(0)
        return self.default


@pytest.fixture
def fake_muse() -> FakeMuse:
    return FakeMuse()


@pytest.fixture(autouse=True)
def _scrub_role_markers(monkeypatch):
    """Role markers are set explicitly per test, never inherited from ambient env.

    The suite also runs in-guest as the revision eval gate, where the
    launcher injects CANARY_GUEST=1; refusal tests must see a clean env.
    """
    monkeypatch.delenv("CANARY_GUEST", raising=False)
    monkeypatch.delenv("CANARY_PUBLISHER", raising=False)


# Synthetic recorded payloads: structurally faithful, no real papers.
RECORDED_OPENALEX = {
    "results": [
        {
            "id": "https://openalex.org/W1",
            "doi": "https://doi.org/10.9990/fixture-one",
            "title": "Fixture study of nothing in particular",
            "abstract_inverted_index": {"Fixture": [0], "abstract": [1]},
            "authorships": [{"author": {"display_name": "Ada Fixture"}}],
            "publication_year": 2024,
            "primary_location": {"source": {"display_name": "Journal of Fixtures"}},
            "cited_by_count": 7,
        },
        {
            "id": "https://openalex.org/W2",
            "doi": None,
            "title": "Second fixture without a DOI",
            "abstract_inverted_index": None,
            "authorships": [],
            "publication_year": 2023,
            "primary_location": {},
            "cited_by_count": 0,
        },
    ]
}

RECORDED_SEMSCHOLAR = {
    "data": [
        {
            "paperId": "s2fixture1",
            "title": "Semantic fixture on recorded responses",
            "abstract": "A recorded abstract.",
            "authors": [{"name": "Bob Fixture"}],
            "year": 2025,
            "venue": "Fixture Conf",
            "doi": "10.9990/fixture-one",
            "url": "",
            "citationCount": 3,
        }
    ]
}

# Broker-parsed shape ({"entries": [...]}, never raw Atom): the arXiv record
# shares the DOI so three-provider dedupe collapses it with the other two.
RECORDED_ARXIV = {
    "entries": [
        {
            "id": "2601.00001",
            "version": "2",
            "title": "Fixture study of nothing in particular",
            "abstract": "",
            "authors": ["Ada Fixture"],
            "year": 2026,
            "categories": ["cs.AI"],
            "primary_category": "cs.AI",
            "doi": "10.9990/fixture-one",
            "url_abs": "https://arxiv.org/abs/2601.00001v2",
            "url_pdf": "https://arxiv.org/pdf/2601.00001v2",
        },
        {
            "id": "2601.00002",
            "version": "",
            "title": "Second arxiv fixture without a DOI",
            "abstract": "An arxiv abstract.",
            "authors": [],
            "year": 2025,
            "categories": [],
            "primary_category": "",
            "doi": "",
            "url_abs": "https://arxiv.org/abs/2601.00002",
            "url_pdf": "",
        },
    ]
}


def recorded_http_client(seen: list | None = None) -> httpx.Client:
    """MockTransport client serving the recorded source payloads by host."""

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        path = request.url.path
        if path.endswith("/sources/openalex"):
            return httpx.Response(200, json=RECORDED_OPENALEX)
        if path.endswith("/sources/semanticscholar"):
            return httpx.Response(200, json=RECORDED_SEMSCHOLAR)
        if path.endswith("/sources/arxiv"):
            return httpx.Response(200, json=RECORDED_ARXIV)
        return httpx.Response(404, json={})

    return httpx.Client(transport=httpx.MockTransport(handler))


@pytest.fixture
def recorded_sources(monkeypatch):
    """Fixture gate env plus a recorded HTTP client; returns (client, seen)."""
    monkeypatch.setenv("CANARY_GATE_URL", "http://fixture-gate")
    monkeypatch.setenv("CANARY_GATE_TOKEN", "fixture-access")
    seen: list = []
    client = recorded_http_client(seen)
    yield client, seen
    client.close()


class OfflineGuardError(RuntimeError):
    """An offline test attempted real (non-loopback) network I/O."""


def _guard_host_allowed(host: object) -> bool:
    if host == "localhost":
        return True
    if not isinstance(host, str):
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def offline_guard(request, monkeypatch):
    """Fail closed on real network access in the default (offline) suite.

    Tests marked `live` opt out explicitly. Everything else may only
    touch loopback (127/8, ::1, "localhost") or AF_UNIX paths; any other
    connect or DNS resolution raises OfflineGuardError. `connect` is the
    enforcement backstop: even a resolved name cannot reach the wire.
    """
    if request.node.get_closest_marker("live") is not None:
        yield
        return
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def _check_address(address) -> None:
        if isinstance(address, str):  # AF_UNIX path: not network I/O
            return
        host = address[0]
        if not _guard_host_allowed(host):
            raise OfflineGuardError(
                f"offline suite blocked non-loopback connect to {host!r}; "
                "use MockTransport/recorded fixtures or mark the test live"
            )

    def guarded_connect(self, address):
        _check_address(address)
        return real_connect(self, address)

    def guarded_connect_ex(self, address):
        _check_address(address)  # raises; fail loud instead of an errno
        return real_connect_ex(self, address)

    def guarded_getaddrinfo(host, port, *args, **kwargs):
        if host is not None and not _guard_host_allowed(host):
            raise OfflineGuardError(
                f"offline suite blocked DNS for {host!r}; "
                "use MockTransport/recorded fixtures or mark the test live"
            )
        return real_getaddrinfo(host, port, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", guarded_getaddrinfo)
    yield
