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
            "arxiv": {
                "entries": [
                    {
                        "id": "2601.1",
                        "title": "Same study",
                        "doi": "10.1/dup",
                        "abstract": "richer abstract",
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
        return httpx.Response(200, json={"entries": []})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    assert retrieval.retrieve(ResearchSpec(question="q"), client) == []


def test_retrieve_raises_when_all_fail():
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="retrieval failed"):
        # Keyword-bearing: arXiv must actually call (and fail) too.
        retrieval.retrieve(ResearchSpec(question="timing evidence"), client)


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


def test_build_prompt_adds_coverage_warning_and_footer_instruction():
    prompt = synthesize.build_prompt("q", [paper()], "degraded coverage: openalex failed")
    assert "Coverage warning: degraded coverage: openalex failed" in prompt
    assert "incomplete coverage" in prompt
    assert "Coverage warning" not in synthesize.build_prompt("q", [paper()])


def test_system_prompt_requires_numbers_verbatim_in_spans():
    assert "Every number in a claim sentence" in synthesize.SYSTEM
    assert "must appear verbatim in one of that claim's quoted spans" in synthesize.SYSTEM


def test_build_prompt_degraded_flag_without_detail():
    prompt = synthesize.build_prompt("q", [paper()], degraded=True)
    assert "some providers failed; sources are partial." in prompt


def test_has_coverage_footer():
    assert synthesize.has_coverage_footer("Review. Incomplete coverage of the evidence.")
    assert not synthesize.has_coverage_footer("Review. Thorough and complete.")


def test_synthesize_threads_coverage_warning_to_prompt():
    seen = []

    class Recorder:
        model = "rec"

        def complete(self, system, user, max_tokens=8000):
            seen.append(user)
            return "Fine [1]."

    synthesize.synthesize("q", [paper()], Recorder(), "degraded coverage: x failed")
    assert "Coverage warning: degraded coverage: x failed" in seen[0]


def test_ensure_coverage_footer_appends_only_when_degraded_and_missing():
    out = synthesize.ensure_coverage_footer("Review.", coverage_warning="x failed")
    assert out.startswith("Review.") and synthesize.has_coverage_footer(out)
    keep = "Review. Incomplete coverage of the sources."
    assert synthesize.ensure_coverage_footer(keep, coverage_warning="x failed") == keep
    assert synthesize.ensure_coverage_footer("Review.") == "Review."
    assert synthesize.ensure_coverage_footer("Review.", degraded=True).endswith(
        synthesize.COVERAGE_FOOTER)


def _claims_block(*items):
    import json as _json

    return "```claims\n" + _json.dumps([
        {"id": cid, "text": text, "evidence": [{"paper": n, "span": span}],
         "scope": "abstract", "uncertainty": "", "support": "supported"}
        for cid, text, n, span in items]) + "\n```"


def test_synthesize_scaled_single_shot_under_cap():
    class Solo:
        model = "rec"
        calls = 0

        def complete(self, system, user, max_tokens=8000):
            type(self).calls += 1
            return "Fine [1]."

    s = synthesize.synthesize_scaled("q", [paper()], Solo())
    assert Solo.calls == 1 and s.validation["batches"] == 1
    assert s.cited == (1,)


def test_synthesize_scaled_map_reduces_over_cap(monkeypatch):
    monkeypatch.setattr(synthesize, "MAX_PROMPT_CHARS", 2000)
    papers = [paper(title=f"Study {i} on widgets",
                   abstract=f"Abstract {i} widgets. " * 30)
              for i in range(10)]

    class Batched:
        model = "rec"

        def complete(self, system, user, max_tokens=8000):
            if system == synthesize.REDUCE_SYSTEM:
                return "Merged [1] [10]."
            return f"Batch. [1].\n{_claims_block(('c1', 'Widgets studied.', 1, 'widgets'))}"

    s = synthesize.synthesize_scaled("q widgets", papers, Batched())
    assert s.validation["batches"] == 5
    assert s.text.startswith("Merged")
    assert s.cited == (1, 10)
    # every batch claim remapped into the global paper list
    assert len(s.claims) == 5
    assert [c.id for c in s.claims] == [f"c1@b{i + 1}" for i in range(5)]
    assert [c.evidence[0].paper for c in s.claims] == [1, 3, 5, 7, 9]


def test_synthesize_scaled_rejects_empty_pool():
    class Never:
        model = "rec"

        def complete(self, system, user, max_tokens=8000):
            raise AssertionError("must not call")

    with pytest.raises(ValueError, match="no papers"):
        synthesize.synthesize_scaled("q", [], Never())


def test_synthesize_records_grounding_coverage():
    class Cited:
        model = "rec"

        def complete(self, system, user, max_tokens=8000):
            return "Fine [1]."

    s = synthesize.synthesize("q", [paper(), paper(title="Second")], Cited())
    assert s.validation["coverage"] == "1/2"
    assert s.validation["coverage_ratio"] == 0.5
    assert "coverage_alert" not in s.validation
    s2 = synthesize.synthesize("q", [paper(), paper(title="Second")], Cited(),
                               coverage_threshold=0.9)
    assert s2.validation["coverage_alert"].startswith("low coverage: cited 1/2")


def test_evidence_less_claim_is_flagged():
    class Bare:
        model = "rec"

        def complete(self, system, user, max_tokens=8000):
            import json as _json

            return "Claim [1].\n```claims\n" + _json.dumps([{
                "id": "c1", "text": "Something happened.", "evidence": [],
                "scope": "abstract", "uncertainty": "", "support": "supported"}]) + "\n```"

    s = synthesize.synthesize("q", [paper()], Bare())
    (c,) = s.claims
    assert c.support == "unsupported"
    assert any("missing source IDs" in r for r in s.validation["rejected"])


def test_synthesize_appends_footer_when_model_omits_it():
    class Omitter:
        model = "rec"

        def complete(self, system, user, max_tokens=8000):
            return "Fine [1]."

    s = synthesize.synthesize("q", [paper()], Omitter(), "degraded coverage: x failed")
    assert synthesize.has_coverage_footer(s.text)
    s2 = synthesize.synthesize("q", [paper()], Omitter())
    assert not synthesize.has_coverage_footer(s2.text)


# --- report ---


def test_report_bundle(tmp_path):
    spec = ResearchSpec(question="cervical cancer mortality", max_papers=2)
    papers = [paper(score=1.5), paper(title="Second", score=0.5, url="http://x")]
    synth = synthesize.Synthesis(text="Review [1] [2].", cited=(1, 2), model="m")
    out = report.write_bundle(tmp_path, spec, papers, synth)
    md = (out / "review.md").read_text()
    assert "Cited: 2/2" in md and "## Sources" in md
    assert (out / "provenance.json").exists()
