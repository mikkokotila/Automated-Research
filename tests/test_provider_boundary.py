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
    monkeypatch.setenv("CANARY_GATE_TOKEN", "fixture")
    monkeypatch.setenv("MUSE_MAX_CALLS", "1")
    calls = []

    def broker(request):
        calls.append(request)
        return httpx.Response(200, json={**reply(), "text": "ok", "receipt": "r0"})

    client = MuseClient(client=httpx.Client(transport=httpx.MockTransport(broker)))
    assert client.complete("s", "u") == "ok"
    with pytest.raises(RequestBlocked, match="MUSE_MAX_CALLS budget exhausted"):
        client.complete("s", "u")
    assert len(calls) == 1


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
