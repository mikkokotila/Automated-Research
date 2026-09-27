"""Build 09: provider contracts, evidence tiers, honest relevance."""
import httpx
import pytest

from canary import rank, report, retrieval
from canary.papers import Paper
from canary.spec import ResearchSpec


def mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def openalex_work(**kw):
    work = {"id": "https://openalex.org/W1", "title": "T", "doi": None,
            "publication_year": 2023, "cited_by_count": 0, "authorships": [],
            "primary_location": {}, "abstract_inverted_index": None,
            "ids": {}, "open_access": {}, "best_oa_location": {}}
    work.update(kw)
    return work


def s2_paper(**kw):
    paper = {"paperId": "S1", "title": "T", "abstract": None, "authors": [],
             "year": None, "venue": None, "url": None, "citationCount": 0,
             "externalIds": {}, "openAccessPdf": None, "publicationTypes": []}
    paper.update(kw)
    return paper


# --- DOI normalization ---


@pytest.mark.parametrize("raw,expected", [
    ("https://doi.org/10.1/XyZ ", "10.1/xyz"),
    ("http://dx.doi.org/10.1000/abc.", "10.1000/abc"),
    ("DOI:10.1/q", "10.1/q"),
    ("10.1/plain", "10.1/plain"),
    ("  10.5555/With Space  ", ""),
    ("not-a-doi", ""),
    ("11.1/wrong-prefix", ""),
    (None, ""),
    ("", ""),
])
def test_normalize_doi_variants(raw, expected):
    assert retrieval.normalize_doi(raw) == expected


def test_s2_reads_doi_from_external_ids():
    client = mock_client(lambda req: httpx.Response(200, json={
        "data": [s2_paper(paperId="S9", externalIds={"DOI": "10.9/AbC", "ArXiv": "1234.5"})]}))
    (p,) = retrieval.semscholar_search(ResearchSpec(question="q"), client)
    assert p.doi == "10.9/abc"
    assert p.ref == "doi:10.9/abc"
    assert p.identifiers["arxiv"] == "1234.5"


def test_s2_top_level_doi_is_only_a_fallback():
    client = mock_client(lambda req: httpx.Response(200, json={
        "data": [dict(s2_paper(paperId="S9"), doi="10.9/top")]}))
    (p,) = retrieval.semscholar_search(ResearchSpec(question="q"), client)
    assert p.doi == "10.9/top"


def test_openalex_reads_ids_doi_fallback_and_api_key(monkeypatch):
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        return httpx.Response(200, json={
            "results": [openalex_work(doi=None, ids={"doi": "https://doi.org/10.7/Zz"})]})

    monkeypatch.setenv("OPENALEX_API_KEY", "oakey")
    (p,) = retrieval.openalex_search(ResearchSpec(question="q"), mock_client(handler))
    assert p.doi == "10.7/zz"
    assert "api_key=oakey" in seen["url"]
    assert "mailto" not in seen["url"]  # retired: the provider ignores it


def test_openalex_without_key_sends_no_auth(monkeypatch):
    seen = {}

    def handler(req: httpx.Request) -> httpx.Response:
        seen["url"] = str(req.url)
        return httpx.Response(200, json={"results": []})

    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    retrieval.openalex_search(ResearchSpec(question="q"), mock_client(handler))
    assert "api_key" not in seen["url"] and "mailto" not in seen["url"]


# --- hostile payloads ---


def test_unicode_and_null_fields_survive():
    def handler(req: httpx.Request) -> httpx.Response:
        if "openalex" in str(req.url):
            return httpx.Response(200, json={"results": [
                openalex_work(title="Café — naïve β-cell étude?",
                              authorships=[{"author": {"display_name": None}}, None],
                              publication_year=None, cited_by_count="7",
                              abstract_inverted_index={"Café": [0], "β-cell": [1]}),
                "not-a-dict", {"title": ""}]})
        return httpx.Response(200, json={"data": [
            s2_paper(title="Zürich über alles", authors=[None, {"name": "Aß"}],
                     year=None, venue=None, citationCount=None, abstract=None)]})

    papers = retrieval.retrieve(ResearchSpec(question="café"), mock_client(handler))
    assert {p.title for p in papers} == {"Café — naïve β-cell étude?", "Zürich über alles"}
    assert papers[0].citations == 7 and papers[1].authors == ("Aß",)


def test_int_identifier_values_coerced_not_crashed():
    client = mock_client(lambda req: httpx.Response(200, json={
        "results": [openalex_work(ids={"mag": 123456, "doi": "https://doi.org/10.7/m"})]}))
    (p,) = retrieval.openalex_search(ResearchSpec(question="q"), client)
    assert p.identifiers["mag"] == "123456"
    assert p.doi == "10.7/m"


def test_search_text_keeps_unicode_drops_query_breakers():
    assert retrieval.search_text("café β-cell: 2023–24?") == "café β-cell: 202324"
    assert "?" not in retrieval.search_text("does delay raise mortality?")


# --- dedupe ---


def test_dedupe_merges_case_variant_dois_and_keeps_richer_abstract():
    def handler(req: httpx.Request) -> httpx.Response:
        if "openalex" in str(req.url):
            return httpx.Response(200, json={"results": [
                openalex_work(doi="HTTPS://DOI.ORG/10.3/SAME", abstract_inverted_index=None)]})
        return httpx.Response(200, json={"data": [
            s2_paper(title="T", externalIds={"DOI": "10.3/same"}, abstract="richer")]})

    papers, rep = retrieval.retrieve_with_report(ResearchSpec(question="q"), mock_client(handler))
    assert len(papers) == 1 and papers[0].abstract == "richer"
    assert rep["dedupe"] == {"candidates": 2, "kept": 1, "merged": 1}


# --- backoff, failure, refinement ---


def test_retry_after_honored_within_cap(monkeypatch):
    import time as _t

    waits = []
    monkeypatch.setattr(_t, "sleep", waits.append)
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={}, headers={"retry-after": "2"})
        return httpx.Response(200, json={"results": []})

    retrieval._get(mock_client(handler), "https://x", {})
    assert waits == [2.0]


def test_retry_after_over_cap_falls_back_to_backoff(monkeypatch):
    import time as _t

    waits = []
    monkeypatch.setattr(_t, "sleep", waits.append)
    calls = []

    def handler(req: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            return httpx.Response(429, json={}, headers={"retry-after": "600"})
        return httpx.Response(200, json={"results": []})

    retrieval._get(mock_client(handler), "https://x", {})
    assert waits == [retrieval.BACKOFF_S[0]]


def test_single_provider_failure_degrades_with_honest_warning():
    def handler(req: httpx.Request) -> httpx.Response:
        if "openalex" in str(req.url):
            raise httpx.ConnectError("down")
        return httpx.Response(200, json={"data": [s2_paper(abstract="a")]})

    papers, rep = retrieval.retrieve_with_report(ResearchSpec(question="q"), mock_client(handler))
    assert len(papers) == 1
    assert rep["providers"]["openalex"]["outcome"] == "error"
    assert "down" in rep["providers"]["openalex"]["error"]
    assert rep["warning"].startswith("degraded coverage: openalex failed")
    assert "remaining sources only" in rep["warning"]


def test_both_providers_failing_raises():
    client = mock_client(lambda req: (_ for _ in ()).throw(httpx.ConnectError("down")))
    with pytest.raises(RuntimeError, match="retrieval failed"):
        retrieval.retrieve(ResearchSpec(question="q"), client)


def test_empty_results_refine_once_with_recorded_simplification():
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        if "openalex" in str(req.url):
            return httpx.Response(200, json={"results": []})
        if len([u for u in seen if "semanticscholar" in u]) == 1:
            return httpx.Response(200, json={"data": []})
        return httpx.Response(200, json={"data": [s2_paper(abstract="found")]})  # refined hit

    papers, rep = retrieval.retrieve_with_report(
        ResearchSpec(question="does treatment delay raise cancer mortality?"), mock_client(handler))
    assert len(papers) == 1
    (change,) = rep["refinements"]
    assert change["from"].startswith("does treatment delay")
    import re as _re

    assert set(change["to"].split()) <= set(_re.findall(r"[a-z0-9]+", change["from"].lower()))
    assert rep["queries"] == [change["from"], change["to"]]
    assert sum("semanticscholar" in u for u in seen) == 2  # exactly one retry round


# --- evidence tiers ---


def test_evidence_tiers_and_licenses():
    def handler(req: httpx.Request) -> httpx.Response:
        if "openalex" in str(req.url):
            return httpx.Response(200, json={"results": [
                openalex_work(id="https://openalex.org/W1", title="OA paper",
                              abstract_inverted_index={"Some": [0]},
                              best_oa_location={"pdf_url": "https://x/y.pdf",
                                                "license": "cc-by"}),
                openalex_work(id="https://openalex.org/W2", title="Abstract only",
                              abstract_inverted_index={"Just": [0]}),
                openalex_work(id="https://openalex.org/W3", title="No evidence"),
            ]})
        return httpx.Response(200, json={"data": []})

    papers = retrieval.retrieve(ResearchSpec(question="q"), mock_client(handler))
    by_title = {p.title: p for p in papers}
    assert by_title["OA paper"].evidence == "fulltext"
    assert by_title["OA paper"].license == "cc-by"
    assert by_title["Abstract only"].evidence == "abstract"
    assert by_title["No evidence"].evidence == "none"


def test_inverted_index_gaps_mark_abstract_truncated():
    client = mock_client(lambda req: httpx.Response(200, json={
        "results": [openalex_work(abstract_inverted_index={"First": [0], "third": [7]})]}))
    (p,) = retrieval.openalex_search(ResearchSpec(question="q"), client)
    assert p.abstract == "First third"
    assert p.extra["abstract_complete"] is False


# --- relevance: the archived off-topic run ---


OFF_TOPIC_SEED = "does treatment delay raise cancer mortality?"


def off_topic_fixtures():
    return [
        Paper(ref="doi:10.1/jsccr", title="JSCCR guidelines 2016 for colorectal cancer",
              abstract="Committee consensus on standard treatment strategies and institutional practice.",
              citations=900, source="openalex"),
        Paper(ref="doi:10.2/global", title="Global cancer statistics",
              abstract="Worldwide estimates of cases and deaths by site and region.",
              citations=5000, source="semanticscholar"),
    ]


def test_off_topic_high_citation_run_flags_weak_coverage():
    coverage = rank.question_coverage(OFF_TOPIC_SEED, off_topic_fixtures())
    assert coverage["verdict"] == "weak"
    assert coverage["best_cover"] < coverage["min_cover"]
    assert "citations do not establish relevance" in coverage["detail"]


def test_on_topic_control_passes_coverage():
    papers = [Paper(ref="doi:10.3/ontopic",
                    title="Treatment delay raises cancer mortality in a cohort",
                    abstract="Longer delay to treatment raises mortality across stages.",
                    citations=4, source="openalex")]
    coverage = rank.question_coverage(OFF_TOPIC_SEED, papers)
    assert coverage["verdict"] == "adequate"


def test_rank_reasons_flag_citation_driven_ordering():
    papers = rank.rerank(OFF_TOPIC_SEED, off_topic_fixtures(), 2)
    assert papers[0].ref == "doi:10.2/global"  # citations still order the baseline
    reasons = papers[0].extra["rank_reasons"]
    assert reasons["title_hits"] == ["cancer"]  # only the generic term
    assert reasons["citation_driven"] is False  # ...so the flag stays honest too
    stray = Paper(ref="x", title="Unrelated widget engineering", citations=99)
    assert rank.reasons(OFF_TOPIC_SEED, stray)["citation_driven"] is True


# --- provenance persistence ---


def test_record_retrieval_persists_report_and_evidence(tmp_path):
    papers = rank.rerank("delay mortality", [
        Paper(ref="doi:10.1/a", title="Delay and mortality", abstract="ab",
              citations=2, source="openalex", doi="10.1/a",
              evidence="abstract", identifiers={"openalex": "W1"})], 1)
    rep = {"queries": ["delay mortality"], "providers": {"openalex": {"outcome": "ok"}},
           "warning": None}
    report.record_retrieval(tmp_path, "delay mortality", papers, rep)
    import json

    entry = json.loads((tmp_path / "retrieval.jsonl").read_text(encoding="utf-8"))
    (saved,) = entry["papers"]
    assert saved["abstract"] == "ab" and saved["evidence"] == "abstract"
    assert saved["rank_reasons"]["title_hits"] == ["delay", "mortality"]
    assert entry["report"]["queries"] == ["delay mortality"]


def test_render_marks_coverage_warning():
    from canary.synthesize import Synthesis

    md = report.render_markdown(ResearchSpec(question="q"), [], Synthesis("t", (), "m"),
                                "degraded coverage: openalex failed")
    assert "> Coverage warning: degraded coverage" in md
    md = report.render_markdown(ResearchSpec(question="q"), [], Synthesis("t", (), "m"), None)
    assert "Coverage warning" not in md
