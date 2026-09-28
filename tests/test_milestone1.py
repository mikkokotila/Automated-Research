import httpx
import pytest

from canary import rank, report, retrieval, synthesize
from canary.muse_client import BASE_URL, DEFAULT_MODEL, resolve_api_key
from canary.papers import Paper
from canary.spec import ResearchSpec


def paper(**kw):
    base = dict(ref="t:1", title="Mortality factors in advanced cervical cancer", abstract="")
    base.update(kw)
    return Paper(**base)


# --- spec ---


def test_spec_rejects_empty_question():
    with pytest.raises(ValueError):
        ResearchSpec(question="   ")


def test_spec_rejects_bad_max_papers():
    with pytest.raises(ValueError):
        ResearchSpec(question="q", max_papers=0)


# --- rank ---


def test_rerank_prefers_topical_cited_recent():
    q = "mortality factors advanced cervical cancer"
    topical = paper(
        title="Mortality factors in advanced cervical cancer patients",
        abstract="cervical cancer mortality analysis",
        citations=50,
        year=2024,
    )
    off = paper(title="Wheat yields under drought", abstract="agriculture", citations=5000, year=2025)
    ranked = rank.rerank(q, [off, topical], 2)
    assert ranked[0].title == topical.title
    assert ranked[0].score > ranked[1].score


def test_rerank_is_deterministic():
    q = "cervical cancer mortality"
    papers = [paper(title=f"paper {i} cervical cancer", citations=i) for i in range(10)]
    assert rank.rerank(q, papers, 5) == rank.rerank(q, papers, 5)


# --- retrieval ---


def mock_client(routes: dict) -> httpx.Client:
    def handler(req: httpx.Request) -> httpx.Response:
        for key, payload in routes.items():
            if key in str(req.url):
                return httpx.Response(200, json=payload)
        return httpx.Response(404, json={})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_openalex_parses_and_inverts_abstract():
    client = mock_client(
        {
            "openalex": {
                "results": [
                    {
                        "id": "W1",
                        "title": "Cervical cancer mortality",
                        "doi": "https://doi.org/10.1/x",
                        "publication_year": 2023,
                        "cited_by_count": 12,
                        "authorships": [{"author": {"display_name": "A. Doe"}}],
                        "primary_location": {"source": {"display_name": "Lancet"}},
                        "abstract_inverted_index": {"Cervical": [0], "mortality": [2], "rises": [1]},
                    }
                ]
            }
        }
    )
    (p,) = retrieval.openalex_search(ResearchSpec(question="cervical cancer"), client)
    assert p.abstract == "Cervical rises mortality"
    assert p.authors == ("A. Doe",) and p.citations == 12 and p.source == "openalex"


def test_retrieve_dedupes_by_doi_across_sources():
    client = mock_client(
        {
            "openalex": {
                "results": [
                    {
                        "id": "W1",
                        "title": "Same study",
                        "doi": "https://doi.org/10.1/dup",
                        "publication_year": 2022,
                        "cited_by_count": 3,
                        "authorships": [],
                        "primary_location": {},
                        "abstract_inverted_index": None,
                    }
                ]
            },
            "semanticscholar": {
                "data": [
                    {
                        "paperId": "S1",
                        "title": "Same study",
                        "doi": "10.1/dup",
                        "abstract": "richer abstract",
                        "authors": [],
                        "year": 2022,
                        "venue": "",
                        "url": "",
                        "citationCount": 3,
                    }
                ]
            },
        }
    )
    papers = retrieval.retrieve(ResearchSpec(question="dup study"), client)
    assert len(papers) == 1
    assert papers[0].abstract == "richer abstract"


def test_retrieve_degrades_when_one_source_fails():
    def handler(req: httpx.Request) -> httpx.Response:
        if "openalex" in str(req.url):
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"data": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert retrieval.retrieve(ResearchSpec(question="q"), client) == []


def test_retrieve_raises_when_all_fail():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="retrieval failed"):
        # Keyword-bearing: arXiv must actually call (and fail) too.
        retrieval.retrieve(ResearchSpec(question="timing evidence"), client)


def test_semscolar_sends_api_key_when_set(monkeypatch):
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.update(req.headers)
        return httpx.Response(200, json={"data": []})

    monkeypatch.setenv("SEMANTIC_SCHOLAR_API_KEY", "s2key")
    # Direct-provider headers only: a brokered route replaces them. Scrub the
    # ambient gate env (present when the suite runs in-guest as the eval gate).
    monkeypatch.delenv("CANARY_GATE_URL", raising=False)
    monkeypatch.delenv("CANARY_GATE_TOKEN", raising=False)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    retrieval.semscholar_search(ResearchSpec(question="q"), client)
    assert seen.get("x-api-key") == "s2key"


def test_search_text_strips_query_breakers():
    assert "?" not in retrieval.search_text("does delay raise mortality?")
    assert retrieval.search_text("cervical cancer: 2023-24") == "cervical cancer: 2023-24"


def test_get_retries_transient_then_succeeds(monkeypatch):
    import time as _t

    monkeypatch.setattr(_t, "sleep", lambda s: None)
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) < 3:
            return httpx.Response(429, json={})
        return httpx.Response(200, json={"results": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    resp = retrieval._get(client, "https://x", {})
    assert resp.status_code == 200 and len(calls) == 3


def test_get_raises_client_errors_immediately(monkeypatch):
    import time as _t

    monkeypatch.setattr(_t, "sleep", lambda s: None)
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(400, json={})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError):
        retrieval._get(client, "https://x", {})
    assert len(calls) == 1


# --- muse client ---


def test_muse_defaults():
    assert BASE_URL == "https://api.meta.ai/v1"
    assert DEFAULT_MODEL == "muse-spark-1.3-contributor"


def test_resolve_api_key_prefers_muse_first():
    env = {"MUSE_API_KEY": "a", "MODEL_API_KEY": "b", "META_API_KEY": "c"}
    assert resolve_api_key(env) == "a"
    assert resolve_api_key({"META_API_KEY": "c"}) == "c"
    with pytest.raises(RuntimeError, match="MUSE_API_KEY"):
        resolve_api_key({})


def _service_client(monkeypatch, responses):
    monkeypatch.setenv("CANARY_GATE_TOKEN", "fixture-access")
    monkeypatch.setenv("CANARY_GATE_URL", "http://fixture")
    from canary.muse_client import MuseClient
    calls = []
    def handler(request):
        calls.append(request)
        status, body = responses[min(len(calls)-1, len(responses)-1)]
        return httpx.Response(status, json=body)
    return MuseClient(client=httpx.Client(transport=httpx.MockTransport(handler))), calls


def _reply(text, finish="stop"):
    return {"model": DEFAULT_MODEL, "text": text, "finish_reason": finish,
            "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14}, "receipt": "fixture"}


def test_muse_retries_transient(monkeypatch):
    import time
    monkeypatch.setattr(time, "sleep", lambda s: None)
    client, calls = _service_client(monkeypatch, [
        (502, {"error": "provider_request_failed"}),
        (502, {"error": "provider_request_failed"}), (200, _reply("hello"))])
    assert client.complete("s", "u") == "hello"
    assert len(calls) == 3


def test_muse_no_retry_on_auth(monkeypatch):
    from canary.muse_client import RequestBlocked
    client, calls = _service_client(monkeypatch, [(403, {"error": "policy_blocked", "message": "denied"})])
    with pytest.raises(RequestBlocked, match="policy_blocked"):
        client.complete("s", "u")
    assert len(calls) == 1


def test_muse_empty_reports_finish_reason(monkeypatch):
    client, _ = _service_client(monkeypatch, [(200, _reply("  ", "length"))])
    with pytest.raises(RuntimeError, match="finish=length"):
        client.complete("s", "u")


# --- synthesize ---


class FakeCompleter:
    model = "fake"

    def __init__(self, text):
        self.text = text

    def complete(self, system, user, max_tokens=2000):
        assert "[1]" in user and "Papers:" in user
        return self.text


def test_synthesize_extracts_valid_citations():
    papers = [paper(), paper(title="Second paper")]
    s = synthesize.synthesize("q", papers, FakeCompleter("Claims [1] hold, but [2] differs. [9] ignored."))
    assert s.text.startswith("Claims") and s.cited == (1, 2) and s.model == "fake"


def test_synthesize_rejects_empty():
    with pytest.raises(ValueError):
        synthesize.synthesize("q", [], FakeCompleter("x"))
    with pytest.raises(RuntimeError, match="empty"):
        synthesize.synthesize("q", [paper()], FakeCompleter("  "))


def test_build_prompt_truncates_huge_lists(monkeypatch):
    monkeypatch.setattr(synthesize, "MAX_PROMPT_CHARS", 500)
    papers = [paper(title=f"t{i}", abstract="a" * 500) for i in range(10)]
    prompt = synthesize.build_prompt("q", papers)
    assert len(prompt) <= 540 and prompt.endswith("[truncated for length]")


# --- report ---


def test_report_bundle(tmp_path):
    spec = ResearchSpec(question="cervical cancer mortality", max_papers=2)
    papers = [paper(score=1.5), paper(title="Second", score=0.5, url="http://x")]
    synth = synthesize.Synthesis(text="Review [1] [2].", cited=(1, 2), model="m")
    out = report.write_bundle(tmp_path, spec, papers, synth)
    md = (out / "review.md").read_text()
    assert "Cited: 2/2" in md and "## Sources" in md
    assert (out / "provenance.json").exists()
