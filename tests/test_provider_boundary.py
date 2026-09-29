"""Build 05: credential-safe provider boundary.

Run-scoped worker tokens, provider preflight state, error-branch coverage,
finish/usage preservation, bounded backoff honoring, and no secret leakage.
No live credentials or provider calls; the broker transport is always faked.
"""
import json
import threading

import httpx
import pytest

from boundary.gateway import Gateway, ProviderFailure
from boundary.ledger import Ledger
from boundary.policy import (ALLOWED_MODEL, BudgetBlocked, PolicyBlocked, StateBlocked,
                             validate_request)
from boundary.server import main as server_main
from boundary.server import make_server
from canary.muse_client import MuseClient, RequestBlocked
from tests.test_budget import Clock, payload, reply, service


@pytest.fixture
def book(tmp_path):
    clock = Clock()
    return Ledger.initialize(tmp_path / "usage.sqlite3", clock), clock


def test_run_token_mint_use_revoke_expire(book):
    ledger, clock = book
    ident, secret = ledger.mint_run_token(60)
    assert ledger.check_run_token(secret) is True
    assert ledger.check_run_token("wrong") is False
    assert ledger.check_run_token("") is False
    assert ledger.revoke_run_token(ident) is True
    assert ledger.check_run_token(secret) is False
    assert ledger.revoke_run_token("missing") is False
    ident2, secret2 = ledger.mint_run_token(10)
    clock.advance(11)
    assert ledger.check_run_token(secret2) is False
    assert ident != ident2


def test_run_token_ttl_validated_and_halt_blocks_mint(book):
    ledger, _ = book
    for bad in (0, -5, 7 * 86_400 + 1, "60", 1.5, True):
        with pytest.raises(StateBlocked):
            ledger.mint_run_token(bad)
    ledger.halt("fixture")
    with pytest.raises(StateBlocked):
        ledger.mint_run_token(60)


def test_outcome_recorded_for_success_denial_and_failure(book):
    ledger, _ = book
    assert ledger.status()["last_provider_outcome"] is None
    gate, _ = service(book)
    out = gate.complete(payload())
    assert out["finish_reason"] == "stop"
    assert ledger.status()["last_provider_outcome"] == {
        "finish_reason": "stop", "kind": "success", "model": ALLOWED_MODEL, "tokens": 150}

    gate, _ = service(book, httpx.ConnectError("down"))
    with pytest.raises(ProviderFailure):
        gate.complete(payload())
    assert ledger.status()["last_provider_outcome"] == {"error": "transport", "kind": "failure"}


def test_denial_outcome_and_halt_are_persistent(book):
    ledger, clock = book
    gate, _ = service(book, {}, 403)
    with pytest.raises(PolicyBlocked):
        gate.complete(payload())
    assert ledger.status()["last_provider_outcome"] == {"kind": "denied", "status": 403}
    assert ledger.status()["halted"] == "provider_access_denied"


def test_provider_429_surfaces_retry_after_without_halt(book):
    ledger, _ = book
    seen = []

    def send(request):
        seen.append(request)
        return httpx.Response(429, headers={"retry-after": "7"}, json={"error": "slow"})

    gate = Gateway(ledger, "fixture-provider-key", httpx.MockTransport(send))
    with pytest.raises(ProviderFailure) as exc:
        gate.complete(payload())
    assert exc.value.retry_after == 7
    assert ledger.status()["halted"] == ""  # quota is retryable, never a halt
    assert ledger.status()["last_provider_outcome"] == {"kind": "failure", "status": 429}
    ledger.reserve(1)  # service still admits


def test_provider_transport_failure_carries_30s_hint(book):
    gate, _ = service(book, httpx.ConnectError("down"))
    with pytest.raises(ProviderFailure) as exc:
        gate.complete(payload())
    assert exc.value.retry_after == 30
    assert "transport" in str(exc.value)


def test_provider_5xx_and_bad_backoff_header_have_no_hint(book):
    ledger, _ = book
    for status, headers, expected in ((500, {}, None), (429, {"retry-after": "soon"}, None),
                                      (429, {"retry-after": "999"}, 60)):
        def send(request, status=status, headers=headers):
            return httpx.Response(status, headers=headers, json={})
        gate = Gateway(ledger, "fixture-provider-key", httpx.MockTransport(send))
        with pytest.raises(ProviderFailure) as exc:
            gate.complete(payload())
        assert exc.value.retry_after is expected
    assert ledger.status()["halted"] == ""


def test_empty_response_reaches_worker_as_explicit_error(book, monkeypatch):
    gate, _ = service(book, reply(
        usage={"prompt_tokens": 100, "completion_tokens": 0, "total_tokens": 100},
        choices=[{"message": {"content": ""}, "finish_reason": "stop"}]))

    def broker(request):
        return httpx.Response(200, json=gate.complete(payload()))

    monkeypatch.setenv("CANARY_GATE_TOKEN", "fixture")
    client = MuseClient(client=httpx.Client(transport=httpx.MockTransport(broker)))
    with pytest.raises(RuntimeError, match="empty content.*finish=stop"):
        client.complete("s", "u")


def _broker_body(text, finish, total=150):
    return {"model": ALLOWED_MODEL,
            "usage": {"prompt_tokens": 100, "completion_tokens": total - 100,
                      "total_tokens": total},
            "receipt": "r-test", "text": text, "finish_reason": finish}


def test_length_escalation_recovers_with_doubled_budget(monkeypatch):
    monkeypatch.setenv("CANARY_GATE_TOKEN", "fixture")
    seen = []
    script = [("  ", "length"), ("", "length"), ("recovered", "stop")]

    def broker(request):
        seen.append(json.loads(request.content)["max_tokens"])
        text, finish = script.pop(0)
        return httpx.Response(200, json=_broker_body(text, finish))

    client = MuseClient(client=httpx.Client(transport=httpx.MockTransport(broker)))
    assert client.complete("s", "u") == "recovered"
    assert seen == [8000, 16000, 32000]  # escalates within the attempt bound
    assert client.calls == 3  # every attempt reserves budget honestly
    assert [r["finish_reason"] for r in client.receipts] == ["length", "length", "stop"]


def test_persistent_length_exhaustion_fails_honestly(monkeypatch):
    monkeypatch.setenv("CANARY_GATE_TOKEN", "fixture")
    seen = []

    def broker(request):
        seen.append(json.loads(request.content)["max_tokens"])
        return httpx.Response(200, json=_broker_body("", "length", total=9000))

    client = MuseClient(client=httpx.Client(transport=httpx.MockTransport(broker)))
    with pytest.raises(RuntimeError, match="empty content.*finish=length"):
        client.complete("s", "u", max_tokens=20000)
    assert seen == [20000, 32768, 32768]  # capped, then one same-budget retry
    assert client.calls == 3


def test_truncation_finish_preserved_to_worker_receipts(book, monkeypatch):
    gate, _ = service(book, reply(
        choices=[{"message": {"content": "cut…"}, "finish_reason": "length"}]))

    def broker(request):
        return httpx.Response(200, json=gate.complete(payload()))

    monkeypatch.setenv("CANARY_GATE_TOKEN", "fixture")
    client = MuseClient(client=httpx.Client(transport=httpx.MockTransport(broker)))
    assert client.complete("s", "u") == "cut…"
    assert client.receipts[0]["finish_reason"] == "length"
    assert client.receipts[0]["usage"]["total_tokens"] == 150


def test_cancellation_budget_blocks_before_send(monkeypatch):
    from canary.spec import BudgetExhausted, Cancelled, RunBudget
    monkeypatch.setenv("CANARY_GATE_TOKEN", "fixture")
    calls = []

    def broker(request):
        calls.append(request)
        return httpx.Response(200, json={**reply(), "text": "ok", "receipt": "r0"})

    budget = RunBudget(1, 20_000_000, 1800)
    client = MuseClient(client=httpx.Client(transport=httpx.MockTransport(broker)),
                        budget=budget)
    assert client.complete("s", "u") == "ok"
    with pytest.raises(BudgetExhausted, match="model-call budget exhausted"):
        client.complete("s", "u")
    assert len(calls) == 1
    budget.calls = 0
    budget.cancel()
    with pytest.raises(Cancelled):
        client.complete("s", "u")
    assert len(calls) == 1  # cancelled dispatch never sends


def test_no_key_material_in_errors_status_or_receipts(book):
    ledger, _ = book
    sentinel = "SENTINEL-KEY-9f8e7d6c5b4a"
    gate = Gateway(ledger, sentinel, httpx.MockTransport(
        lambda request: (_ for _ in ()).throw(httpx.ConnectError("down"))))
    with pytest.raises(ProviderFailure) as exc:
        gate.complete(payload())
    assert sentinel not in str(exc.value)
    assert sentinel not in json.dumps(ledger.status())
    gate2, _ = service(book, {}, 403)
    with pytest.raises(PolicyBlocked) as exc2:
        gate2.complete(payload())
    assert sentinel not in str(exc2.value)
    assert sentinel not in json.dumps(ledger.status())


def test_worker_honors_server_backoff_only_when_retryable(monkeypatch):
    monkeypatch.setenv("CANARY_GATE_TOKEN", "fixture")
    sleeps = []
    monkeypatch.setattr("canary.muse_client.time.sleep", sleeps.append)
    script = [httpx.Response(502, json={"error": "provider_request_failed",
                                        "message": "busy", "retry_after": 3}),
              httpx.Response(200, json={**reply(), "text": "recovered", "receipt": "r1"})]

    def broker(request):
        return script.pop(0)

    client = MuseClient(client=httpx.Client(transport=httpx.MockTransport(broker)))
    assert client.complete("s", "u") == "recovered"
    assert sleeps == [3]  # server hint wins over the fixed schedule
    script = [httpx.Response(502, json={"error": "provider_request_failed", "message": "x"}),
              httpx.Response(200, json={**reply(), "text": "ok", "receipt": "r2"})]
    client.complete("s", "u")
    assert sleeps == [3, 2.0]  # no hint: fixed backoff
    script = [httpx.Response(403, json={"error": "policy_blocked", "message": "no"})]
    with pytest.raises(RequestBlocked):
        client.complete("s", "u")
    assert sleeps == [3, 2.0]  # non-retryable: no sleep, no retry


def test_worker_rides_hinted_outage_across_four_attempts(monkeypatch):
    monkeypatch.setenv("CANARY_GATE_TOKEN", "fixture")
    sleeps = []
    monkeypatch.setattr("canary.muse_client.time.sleep", sleeps.append)
    script = [httpx.Response(502, json={"error": "provider_request_failed",
                                        "message": "Provider transport failed",
                                        "retry_after": 30})] * 3 + [
        httpx.Response(200, json={**reply(), "text": "recovered", "receipt": "r9"})]

    def broker(request):
        return script.pop(0)

    client = MuseClient(client=httpx.Client(transport=httpx.MockTransport(broker)))
    assert client.complete("s", "u") == "recovered"
    assert sleeps == [30, 30, 30]
    script = [httpx.Response(502, json={"error": "provider_request_failed",
                                        "message": "x"})] * 4
    with pytest.raises(RequestBlocked):
        client.complete("s", "u")
    assert sleeps == [30, 30, 30, 2.0, 5.0, 15.0]  # no hint: fixed schedule


def test_run_token_auth_over_http(book):
    ledger, _ = book
    gate, _ = service(book)
    server = make_server(gate, "test-access", ("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02},
                              daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        ident, secret = ledger.mint_run_token(60)
        with httpx.Client(trust_env=False) as http:
            ok = {"Authorization": f"Bearer {secret}"}
            assert http.get(url + "/v1/status", headers=ok).status_code == 200
            assert http.get(url + "/v1/status",
                            headers={"Authorization": "Bearer test-access"}).status_code == 200
            assert http.get(url + "/v1/status",
                            headers={"Authorization": "Bearer wrong"}).status_code == 403
            assert ledger.revoke_run_token(ident) is True
            assert http.get(url + "/v1/status", headers=ok).status_code == 403
    finally:
        server.shutdown()
        server.server_close()
        gate.http.close()


def test_preflight_reports_authorized_blocked_or_unknown(tmp_path, capsys):
    clock = Clock()
    state = tmp_path / "state"
    assert server_main(["init"], state_dir=str(state), clock=clock) is None
    capsys.readouterr()
    assert server_main(["preflight"], state_dir=str(state), clock=clock) == 1
    assert json.loads(capsys.readouterr().out)["access"] == "unknown"
    ledger = Ledger(state / "usage.sqlite3", clock)
    ledger.note_outcome({"kind": "success", "model": ALLOWED_MODEL, "tokens": 5,
                         "finish_reason": "stop"})
    assert server_main(["preflight"], state_dir=str(state), clock=clock) == 0
    assert json.loads(capsys.readouterr().out)["access"] == "authorized"
    ledger.halt("fixture")
    assert server_main(["preflight"], state_dir=str(state), clock=clock) == 1
    assert json.loads(capsys.readouterr().out) == {"access": "blocked", "reason": "fixture"}


def test_mint_revoke_cli_roundtrip(tmp_path, capsys):
    clock = Clock()
    state = tmp_path / "state"
    server_main(["init"], state_dir=str(state), clock=clock)
    capsys.readouterr()
    server_main(["mint-token", "--ttl", "60"], state_dir=str(state), clock=clock)
    ident, secret = capsys.readouterr().out.split()
    ledger = Ledger(state / "usage.sqlite3", clock)
    assert ledger.check_run_token(secret) is True
    server_main(["revoke-token", "--id", ident], state_dir=str(state), clock=clock)
    assert capsys.readouterr().out.strip() == "revoked"
    assert ledger.check_run_token(secret) is False


def test_launcher_mints_run_scoped_tokens():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / "scripts/container_run.sh").read_text()
    assert "mint-token --ttl" in text and "revoke-token --id" in text
    assert "cat /state/access.key" not in text
    assert '"TOKEN_ID"' in text


# --- arXiv source route ---


ARXIV_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
  <id>https://arxiv.org/api/fixture</id>
  <title>arXiv Query: fixture</title>
  <updated>2026-01-01T00:00:00Z</updated>
  <entry>
    <id>https://arxiv.org/abs/2601.00001v2</id>
    <title>Fixture study of nothing in particular</title>
    <summary>First line.
      Second line.</summary>
    <author><name>Ada Fixture</name></author>
    <author><name></name></author>
    <published>2026-01-02T00:00:00Z</published>
    <arxiv:doi>10.9990/fixture-one</arxiv:doi>
    <arxiv:primary_category term="cs.AI"/>
    <category term="cs.AI"/><category term="cs.LG"/>
    <link href="https://arxiv.org/abs/2601.00001v2" rel="alternate" type="text/html"/>
    <link title="pdf" href="https://arxiv.org/pdf/2601.00001v2" rel="related" type="application/pdf"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/hep-th/9901001</id>
    <title>Old-style identifier without links</title>
    <summary></summary>
    <published>1999-01-01T00:00:00Z</published>
  </entry>
  <entry>
    <id>https://arxiv.org/abs/2601.00002</id>
    <summary>No title: dropped.</summary>
  </entry>
</feed>
"""

# Fixture credential, concatenated so no "Bearer <token>" literal appears.
_ARXIV_ACCESS = "test-access"
_ARXIV_AUTH = {"Authorization": "Bearer " + _ARXIV_ACCESS}


class _FakeUpstreamResponse:
    def __init__(self, status, body):
        self.status = status
        self._body = body

    def read(self, n=-1):
        return self._body[:n] if n is not None and n >= 0 else self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def arxiv_canned(book, monkeypatch):
    """Live broker whose arXiv upstream (urllib) serves canned bytes."""
    import io
    import urllib.error

    from boundary import server as server_mod

    monkeypatch.setattr(server_mod, "_ARXIV_MIN_INTERVAL_S", 0)
    monkeypatch.setattr(server_mod, "_arxiv_last_upstream", 0.0)
    ledger, _ = book
    calls: list = []  # urlopen URLs
    script: list = [(200, ARXIV_ATOM.encode())]  # (status, body) or Exception

    def fake_urlopen(url, timeout=None):
        calls.append(url)
        item = script[0] if len(script) == 1 else script.pop(0)
        if isinstance(item, Exception):
            raise item
        status, body = item
        if status != 200:
            raise urllib.error.HTTPError(url, status, "upstream", {}, io.BytesIO(body))
        return _FakeUpstreamResponse(status, body)

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    def send(request):
        raise AssertionError("arXiv must bypass gateway.http")

    gate = Gateway(ledger, "fixture-provider-key", httpx.MockTransport(send))
    server = make_server(gate, "test-access", ("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02},
                              daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls, script
    finally:
        server.shutdown()
        server.server_close()
        gate.http.close()


def test_sources_arxiv_proxies_and_parses_atom(arxiv_canned):
    import urllib.parse

    url, calls, _ = arxiv_canned
    headers = _ARXIV_AUTH
    with httpx.Client(trust_env=False) as http:
        resp = http.get(url + "/v1/sources/arxiv?search_query=all%3Atiming&max_results=5",
                        headers=headers)
    assert resp.status_code == 200
    first, second = resp.json()["entries"]  # title-less entry dropped
    assert first["id"] == "2601.00001" and first["version"] == "2"
    assert first["abstract"] == "First line. Second line."
    assert first["authors"] == ["Ada Fixture"] and first["year"] == 2026
    assert first["doi"] == "10.9990/fixture-one"
    assert first["categories"] == ["cs.AI", "cs.LG"] and first["primary_category"] == "cs.AI"
    assert first["url_abs"] == "https://arxiv.org/abs/2601.00001v2"
    assert first["url_pdf"] == "https://arxiv.org/pdf/2601.00001v2"
    assert second["id"] == "hep-th/9901001" and second["version"] == ""
    assert second["year"] == 1999 and second["authors"] == []
    assert second["url_abs"] == "https://arxiv.org/abs/hep-th/9901001"  # derived
    assert second["url_pdf"] == "https://arxiv.org/pdf/hep-th/9901001"
    (upstream,) = calls
    parts = urllib.parse.urlsplit(upstream)
    assert (parts.scheme, parts.hostname, parts.path) == (
        "https", "export.arxiv.org", "/api/query")
    qs = dict(urllib.parse.parse_qsl(parts.query))
    assert qs["max_results"] == "5" and qs["search_query"] == "all:timing"


def test_sources_arxiv_rejects_bad_params_without_upstream_call(arxiv_canned):
    url, calls, _ = arxiv_canned
    headers = _ARXIV_AUTH
    bad = ["max_results=500", "max_results=0", "max_results=many", "start=-1",
           "start=x", "sortBy=evil", "sortOrder=sideways", "id_list=1",
           "search_query=all:x&search_query=all:y"]
    with httpx.Client(trust_env=False) as http:
        for query in bad:
            assert http.get(url + "/v1/sources/arxiv?" + query, headers=headers).status_code == 403
        assert http.get(url + "/v1/sources/arxiv?search_query=all:x").status_code == 403
    assert calls == []


def test_sources_arxiv_upstream_failures_map_to_502_or_429(arxiv_canned):
    import urllib.error

    url, _, script = arxiv_canned
    headers = _ARXIV_AUTH
    script[:] = [(200, b"<html>not atom</html>"),
                 (500, b"upstream blew up"),
                 (429, b"slow down"),
                 urllib.error.URLError("connection refused"),
                 (200, ARXIV_ATOM.encode())]
    with httpx.Client(trust_env=False) as http:
        base = url + "/v1/sources/arxiv?search_query=all:x"
        assert http.get(base, headers=headers).status_code == 502  # malformed feed
        assert http.get(base, headers=headers).status_code == 502  # upstream 5xx
        assert http.get(base, headers=headers).status_code == 429  # quota passes through
        assert http.get(base, headers=headers).status_code == 502  # transport failure
        assert http.get(base, headers=headers).status_code == 200


def test_sources_arxiv_oversized_body_is_rejected(arxiv_canned, monkeypatch):
    from boundary import server as server_mod

    monkeypatch.setattr(server_mod, "_ARXIV_MAX_BODY", 10)
    url, _, _ = arxiv_canned
    with httpx.Client(trust_env=False) as http:
        resp = http.get(url + "/v1/sources/arxiv?search_query=all:x",
                        headers=_ARXIV_AUTH)
    assert resp.status_code == 502


def test_sources_arxiv_spaces_upstream_calls(book, monkeypatch):
    import time as _t

    from boundary import server as server_mod

    monkeypatch.setattr(server_mod, "_ARXIV_MIN_INTERVAL_S", 0.3)
    monkeypatch.setattr(server_mod, "_arxiv_last_upstream", 0.0)
    sleeps: list = []
    monkeypatch.setattr(_t, "sleep", sleeps.append)
    monkeypatch.setattr("urllib.request.urlopen", lambda url, timeout=None: _FakeUpstreamResponse(
        200, b"<feed xmlns='http://www.w3.org/2005/Atom'/>"))
    ledger, _ = book
    gate = Gateway(ledger, "fixture-provider-key", httpx.MockTransport(
        lambda req: (_ for _ in ()).throw(AssertionError("arXiv must bypass gateway.http"))))
    server = make_server(gate, "test-access", ("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.02},
                              daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        headers = _ARXIV_AUTH
        with httpx.Client(trust_env=False) as http:
            assert http.get(url + "/v1/sources/arxiv?search_query=all:x",
                            headers=headers).status_code == 200
            assert http.get(url + "/v1/sources/arxiv?search_query=all:y",
                            headers=headers).status_code == 200
    finally:
        server.shutdown()
        server.server_close()
        gate.http.close()
    assert len(sleeps) == 1 and 0.2 < sleeps[0] <= 0.3


def test_arxiv_entries_rejects_non_feeds():
    from boundary.server import _arxiv_entries

    with pytest.raises(ValueError, match="malformed"):
        _arxiv_entries(b"<feed><entry>")
    with pytest.raises(ValueError, match="not an arxiv atom feed"):
        _arxiv_entries(b"<html></html>")
    assert _arxiv_entries(b"<feed xmlns='http://www.w3.org/2005/Atom'/>") == []
