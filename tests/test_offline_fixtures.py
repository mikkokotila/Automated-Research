"""Contract checks for the shared offline fixtures in conftest.py."""
import pytest

from canary import retrieval, synthesize
from canary.spec import ResearchSpec
from tests.conftest import FakeMuse, recorded_http_client


def test_fake_muse_serves_scripts_and_records_calls():
    muse = FakeMuse(default="fallback").script("strategist", "first", "second")
    assert muse.complete("research strategist", "u") == "first"
    assert muse.complete("research strategist", "u") == "second"
    assert muse.complete("other", "u") == "fallback"
    assert len(muse.calls) == 3 and muse.model == "fake-muse"


def test_fake_muse_satisfies_synthesize_protocol(fake_muse):
    from tests.test_milestone1 import paper

    fake_muse.default = "Summary [1]."
    out = synthesize.synthesize("q", [paper()], fake_muse)
    assert out.cited == (1,) and out.model == "fake-muse"


def test_recorded_sources_parse_and_dedupe_across_providers(recorded_sources):
    client, seen = recorded_sources
    spec = ResearchSpec(question="fixture question", max_papers=5)
    s2 = retrieval.semscholar_search(spec, client)
    assert len(s2) == 1 and s2[0].abstract == "A recorded abstract."
    papers = retrieval.retrieve(spec, client)
    assert any(p.abstract == "Fixture abstract" for p in papers)
    # Shared DOI dedupes to one record; the DOI-less paper survives.
    assert len(papers) == 2
    assert [p.ref for p in papers].count("doi:10.9990/fixture-one") == 1
    assert {r.url.path for r in seen} == {"/v1/sources/openalex", "/v1/sources/semanticscholar"}


def test_recorded_client_rejects_unknown_hosts():
    client = recorded_http_client()
    try:
        resp = client.get("https://example.com/")
    finally:
        client.close()
    assert resp.status_code == 404


@pytest.mark.live
@pytest.mark.skip(reason="template: live provider checks stay opt-in and unimplemented in step 01")
def test_live_template_never_runs_by_default():
    raise AssertionError("live marker must be deselected in the default suite")
