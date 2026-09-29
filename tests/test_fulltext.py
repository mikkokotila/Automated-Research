"""Lazy full-text: broker fetch, top-N enrichment, prompt and anchoring."""
import httpx
import pytest

from canary import fulltext
from canary.papers import Paper


def _paper(ref="arxiv:2601.00001", source="arxiv", aid="2601.00001v2", **kw):
    args = {"ref": ref, "title": "T", "abstract": "abstract words",
            "source": source, "identifiers": {"arxiv": aid} if aid else {}}
    args.update(kw)
    return Paper(**args)


def _gate(monkeypatch, handler):
    monkeypatch.setenv("CANARY_GATE_URL", "http://gate:8787")
    monkeypatch.setenv("CANARY_GATE_TOKEN", "fixture-token")
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_fetch_returns_record_via_mock_gate(monkeypatch):
    def send(request):
        assert request.url.path == "/v1/sources/arxiv-pdf"
        assert dict(request.url.params) == {"id": "2601.00001v2"}
        assert request.headers["authorization"] == "Bearer fixture-token"
        return httpx.Response(200, json={"id": "2601.00001v2",
                                         "text": "body text", "pages": 3})
    rec = fulltext.fetch(_gate(monkeypatch, send), "2601.00001v2")
    assert rec["text"] == "body text" and rec["pages"] == 3


def test_fetch_none_without_gate_or_on_failure(monkeypatch):
    monkeypatch.delenv("CANARY_GATE_URL", raising=False)
    monkeypatch.delenv("CANARY_GATE_TOKEN", raising=False)
    http = httpx.Client(transport=httpx.MockTransport(
        lambda req: httpx.Response(200, json={"text": "x"})))
    assert fulltext.fetch(http, "2601.00001v2") is None  # no gate, no call
    for resp in (httpx.Response(502, json={"error": "source_unavailable"}),
                 httpx.Response(200, json={"text": ""}),
                 httpx.Response(200, json=["not", "a", "dict"])):
        http = _gate(monkeypatch, lambda req, r=resp: r)
        assert fulltext.fetch(http, "2601.00001v2") is None


def test_enrich_attaches_first_n_arxiv_only(monkeypatch):
    def send(request):
        aid = dict(request.url.params)["id"]
        if aid == "bad-id":
            return httpx.Response(502, json={"error": "source_unavailable"})
        return httpx.Response(200, json={"id": aid, "text": f"TEXT-{aid}",
                                         "pages": 1})
    http = _gate(monkeypatch, send)
    papers = [_paper(aid="good-1"), _paper(aid="bad-id"),
              _paper(source="openalex", aid=""), _paper(aid="good-2")]
    out, counts = fulltext.enrich(papers, http, 2)
    assert counts == {"ok": 2, "skipped": 1}
    assert out[0].extra["fulltext"] == "TEXT-good-1"
    assert out[0].evidence == "fulltext"
    assert out[1].extra == {} and out[1].evidence == ""  # fetch failed: abstract stands
    assert out[2].extra == {}  # non-arxiv untouched, budget unspent
    assert out[3].extra["fulltext"] == "TEXT-good-2"
    assert papers[0].extra == {}  # inputs not mutated


def test_enrich_never_raises_and_zero_is_noop(monkeypatch):
    def send(request):
        raise RuntimeError("transport exploded")
    http = _gate(monkeypatch, send)
    out, counts = fulltext.enrich([_paper()], http, 2)
    assert counts == {"ok": 0, "skipped": 1} and out[0].extra == {}
    out, counts = fulltext.enrich([_paper()], http, 0)
    assert counts == {"ok": 0, "skipped": 0}


def test_span_anchored_against_fulltext():
    from canary.synthesize import _span_anchored

    p = _paper(extra={"fulltext": "deep body phrase here"})
    assert _span_anchored("deep body phrase", p) is True
    assert _span_anchored("absent everywhere", p) is False


def test_build_prompt_includes_fulltext_excerpt():
    from canary.synthesize import build_prompt

    p = _paper(extra={"fulltext": "F" * 9000})
    prompt = build_prompt("q?", [p])
    assert "Full text excerpt: " + "F" * 8000 in prompt
    assert "Abstract: abstract words" in prompt


def test_spec_max_fulltext_validated():
    from canary.spec import InvalidSpec, RunSpec

    assert RunSpec(question="q?").max_fulltext == 2
    assert RunSpec(question="q?", max_fulltext=0).max_fulltext == 0
    with pytest.raises(InvalidSpec):
        RunSpec(question="q?", max_fulltext=11)
    with pytest.raises(InvalidSpec):
        RunSpec(question="q?", max_fulltext=-1)
