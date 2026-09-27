"""Shared offline fixtures: no live credentials, network, or Docker required.

Tests marked `live` (real provider) or `containment` (Linux/Docker host checks)
are deselected by default; see pyproject addopts. Everything here runs anywhere.
"""
from __future__ import annotations

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
