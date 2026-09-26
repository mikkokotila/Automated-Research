"""No live credentials, external requests, or real token spending in these tests."""
import concurrent.futures
import json
import multiprocessing
from pathlib import Path
import sqlite3
import threading
import httpx
import pytest
from boundary.ledger import Ledger
from boundary.policy import (ALLOWED_MODEL, TOKEN_LIMIT, WINDOW_NS, CONTEXT_TOKENS,
    BudgetBlocked, StateBlocked, PolicyBlocked, validate_request)
from boundary.gateway import Gateway, ProviderFailure
from boundary.server import make_server


class Clock:
    def __init__(self):
        self.value = 0
        self.boot = "fixture"
    def __call__(self):
        return self.boot, self.value
    def advance(self, seconds):
        self.value += int(seconds * 1_000_000_000)


@pytest.fixture
def book(tmp_path):
    clock = Clock()
    return Ledger.initialize(tmp_path / "usage.sqlite3", clock), clock


def payload(**kw):
    return {"model": ALLOWED_MODEL, "system": "s", "user": "u", "max_tokens": 8000, **kw}


def reply(**kw):
    return {"model": ALLOWED_MODEL, "usage": {"prompt_tokens": 100, "completion_tokens": 50,
        "total_tokens": 150, "prompt_tokens_details": {"cached_tokens": 80},
        "completion_tokens_details": {"reasoning_tokens": 30}},
        "choices": [{"message": {"content": "fixture result"}, "finish_reason": "stop"}], **kw}


def service(book, result=None, status=200):
    ledger, _ = book
    seen = []
    def send(request):
        seen.append(request)
        if isinstance(result, Exception):
            raise result
        return httpx.Response(status, json=reply() if result is None else result)
    return Gateway(ledger, "fixture-provider-key", httpx.MockTransport(send)), seen


def test_exact_ceiling_and_preflight(book):
    ledger, _ = book
    ledger.reserve(TOKEN_LIMIT)
    with pytest.raises(BudgetBlocked):
        ledger.reserve(1)
    assert ledger.status()["charged_and_reserved"] == TOKEN_LIMIT


def test_rolling_window_from_completion_not_start(book):
    ledger, clock = book
    ident = ledger.reserve(TOKEN_LIMIT)
    clock.advance(3600)
    ledger.settle(ident, TOKEN_LIMIT, "{}")
    clock.advance(86_399)
    with pytest.raises(BudgetBlocked):
        ledger.reserve(1)
    clock.advance(1)
    assert ledger.status()["charged_and_reserved"] == 0
    ledger.reserve(TOKEN_LIMIT)


def test_cross_midnight_does_not_reset(book):
    ledger, clock = book
    clock.advance(23*3600)
    ident = ledger.reserve(TOKEN_LIMIT)
    ledger.settle(ident, TOKEN_LIMIT, "{}")
    clock.advance(2*3600)
    with pytest.raises(BudgetBlocked):
        ledger.reserve(1)


def test_pending_never_expires_even_after_restart(book):
    ledger, clock = book
    ledger.reserve(TOKEN_LIMIT)
    clock.advance(10*86_400)
    other = Ledger(ledger.path, clock)
    with pytest.raises(BudgetBlocked):
        other.reserve(1)


def test_new_kernel_boot_earns_no_downtime_credit(book):
    ledger, clock = book
    ident = ledger.reserve(TOKEN_LIMIT)
    ledger.settle(ident, TOKEN_LIMIT, "{}")
    clock.boot = "new-boot"
    clock.value = 1000 * WINDOW_NS
    with pytest.raises(BudgetBlocked):
        Ledger(ledger.path, clock).reserve(1)


def test_backwards_clock_fails_closed(book):
    ledger, clock = book
    clock.advance(10)
    ledger.reserve(100)
    clock.value = 0
    with pytest.raises(StateBlocked):
        ledger.reserve(1)


def test_wall_clock_changes_do_not_refund(book, monkeypatch):
    import time
    ledger, _ = book
    ident = ledger.reserve(TOKEN_LIMIT)
    ledger.settle(ident, TOKEN_LIMIT, "{}")
    monkeypatch.setattr(time, "time", lambda: 10**20)
    with pytest.raises(BudgetBlocked):
        ledger.reserve(1)


def test_threads_cannot_overbook(book):
    ledger, clock = book
    ledger.reserve(TOKEN_LIMIT-100_000)
    def attempt(_):
        try:
            Ledger(ledger.path, clock).reserve(10_000)
            return True
        except BudgetBlocked:
            return False
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
        assert sum(pool.map(attempt, range(30))) == 10
    assert ledger.status()["charged_and_reserved"] == TOKEN_LIMIT


def _process_attempt(path):
    try:
        Ledger(path).reserve(20_000_000)
        return True
    except BudgetBlocked:
        return False


def test_processes_share_one_ledger(tmp_path):
    ledger = Ledger.initialize(tmp_path / "usage.sqlite3")
    with multiprocessing.get_context("spawn").Pool(4) as pool:
        assert sum(pool.map(_process_attempt, [str(ledger.path)]*15)) == 10
    assert ledger.status()["charged_and_reserved"] == TOKEN_LIMIT


def test_missing_corrupt_and_existing_state(tmp_path, book):
    with pytest.raises(StateBlocked):
        Ledger(tmp_path / "absent")
    p = tmp_path / "corrupt"
    p.write_text("not sqlite")
    with pytest.raises(StateBlocked):
        Ledger(p)
    ledger, clock = book
    with pytest.raises(FileExistsError):
        Ledger.initialize(ledger.path, clock)
    ledger.path.unlink()
    with pytest.raises(StateBlocked):
        ledger.reserve(1)


def test_duplicate_settlement_and_overreport(book):
    ledger, _ = book
    ident = ledger.reserve(100)
    with pytest.raises(StateBlocked):
        ledger.settle(ident, 101, "{}")
    ledger.settle(ident, 80, "{}")
    with pytest.raises(StateBlocked):
        ledger.settle(ident, 0, "{}")
    assert ledger.status()["charged_and_reserved"] == 80


@pytest.mark.parametrize("amount", [-1, 0, True, 1.5, "10", TOKEN_LIMIT+1])
def test_invalid_reservations(book, amount):
    with pytest.raises(BudgetBlocked):
        book[0].reserve(amount)


@pytest.mark.parametrize("changes", [
    {"model": "muse-spark-1.3"}, {"model": "muse-spark-1.2-contributor"},
    {"model": ALLOWED_MODEL+" "}, {"model": "other"}, {"max_tokens": True},
    {"max_tokens": -1}, {"max_tokens": 32769}, {"max_tokens": 1.5},
    {"user": []}, {"stream": True}, {"n": 2}, {"base_url": "https://example.com"},
])
def test_request_rejected_before_provider(book, changes):
    gate, calls = service(book)
    with pytest.raises(PolicyBlocked):
        gate.complete(payload(**changes))
    assert not calls and book[0].status()["charged_and_reserved"] == 0


def test_budget_blocks_before_provider(book):
    book[0].reserve(TOKEN_LIMIT)
    gate, calls = service(book)
    with pytest.raises(BudgetBlocked):
        gate.complete(payload())
    assert not calls


def test_valid_response_counts_cache_and_reasoning_once(book):
    gate, calls = service(book)
    result = gate.complete(payload())
    assert result["usage"]["total_tokens"] == 150
    assert book[0].status()["charged_and_reserved"] == 150
    sent = json.loads(calls[0].content)
    assert sent["model"] == ALLOWED_MODEL
    assert sent["max_completion_tokens"] == 8000 and sent["n"] == 1 and sent["stream"] is False
    assert str(calls[0].url) == "https://api.meta.ai/v1/chat/completions"


@pytest.mark.parametrize("response", [
    reply(model="other"), reply(usage=None), {"choices": []},
    reply(usage={"prompt_tokens":100,"completion_tokens":50,"total_tokens":1}),
    reply(usage={"prompt_tokens":-1,"completion_tokens":50,"total_tokens":49}),
    reply(usage={"prompt_tokens":1.0,"completion_tokens":50,"total_tokens":51}),
    reply(usage={"prompt_tokens":100,"completion_tokens":8001,"total_tokens":8101}),
    reply(usage={"prompt_tokens":100,"completion_tokens":50,"total_tokens":150,
                 "completion_tokens_details":{"reasoning_tokens":51}}),
])
def test_invalid_response_retains_reservation_and_halts(book, response):
    gate, calls = service(book, response)
    with pytest.raises(StateBlocked):
        gate.complete(payload())
    assert book[0].status()["charged_and_reserved"] == validate_request(payload())
    with pytest.raises(StateBlocked):
        gate.complete(payload())
    assert len(calls) == 1


def test_timeout_is_not_refunded_and_each_retry_is_reserved(book):
    gate, calls = service(book, httpx.ReadTimeout("fixture timeout"))
    for _ in range(3):
        with pytest.raises(ProviderFailure):
            gate.complete(payload())
    assert len(calls) == 3
    book[1].advance(3*86_400)
    assert book[0].status()["charged_and_reserved"] == 3*validate_request(payload())


@pytest.mark.parametrize("status", [401, 403])
def test_provider_access_block_is_persistent(book, status):
    gate, calls = service(book, {}, status)
    with pytest.raises(PolicyBlocked):
        gate.complete(payload())
    other = Gateway(Ledger(book[0].path, book[1]), "fixture", gate.http._transport)
    with pytest.raises(StateBlocked):
        other.complete(payload())
    assert len(calls) == 1


@pytest.fixture
def endpoint(book):
    gate, calls = service(book)
    server = make_server(gate, "test-access", ("127.0.0.1", 0))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:"+str(server.server_port), calls
    finally:
        server.shutdown()
        server.server_close()
        gate.http.close()


def test_actual_http_boundary_and_client(endpoint, monkeypatch):
    from canary.muse_client import MuseClient, RequestBlocked
    url, calls = endpoint
    monkeypatch.setenv("CANARY_GATE_URL", url)
    monkeypatch.setenv("CANARY_GATE_TOKEN", "test-access")
    with pytest.raises(RequestBlocked):
        MuseClient(model="other")
    with pytest.raises(RequestBlocked):
        MuseClient(api_key="not-allowed")
    client = MuseClient()
    assert client.complete("s", "u") == "fixture result"
    assert client.receipts[0]["usage"]["total_tokens"] == 150
    with pytest.raises(AttributeError):
        client.model = "other"
    client.close()
    assert len(calls) == 1


def test_http_auth_duplicates_and_extra_routes(endpoint):
    url, calls = endpoint
    with httpx.Client(trust_env=False) as http:
        assert http.post(url+"/v1/complete",json=payload()).status_code == 403
        headers={"Authorization":"Bearer test-access", "Content-Type":"application/json"}
        assert http.post(url+"/v1/complete",json=payload(model="other"),headers=headers).status_code == 403
        assert http.post(url+"/v1/complete",content='{"model":"x","model":"y"}',headers=headers).status_code != 200
        assert http.get(url+"/v1/models",headers=headers).status_code == 403
        assert http.get(url+"/v1/sources/openalex?url=https://other",headers=headers).status_code == 403
    assert not calls


def test_no_direct_fallback(monkeypatch):
    from canary.muse_client import MuseClient, RequestBlocked
    monkeypatch.delenv("CANARY_GATE_TOKEN", raising=False)
    monkeypatch.setenv("MUSE_API_KEY", "fixture-should-not-be-used")
    with pytest.raises(RequestBlocked, match="direct provider access is disabled"):
        MuseClient()


def test_worker_launcher_keeps_credentials_and_ledger_outside():
    root=Path(__file__).resolve().parents[1]
    text=(root/"scripts/container_run.sh").read_text()
    assert "--network canary-private" in text
    assert "-e MUSE_API_KEY" not in text and "-e GITHUB_TOKEN" not in text
    assert "dst=/state" not in text and "docker.sock" not in text
    assert "--user 10002:10002" in text


def test_export_rejects_traversal_and_links(tmp_path):
    import io, tarfile
    from scripts.export_bundle import export
    for index, name in enumerate(("../outside", "/absolute", "link")):
        buf=io.BytesIO()
        with tarfile.open(fileobj=buf,mode="w") as tar:
            info=tarfile.TarInfo(name)
            if name == "link":
                info.type=tarfile.SYMTYPE
                info.linkname="/tmp"
            tar.addfile(info)
        buf.seek(0)
        with pytest.raises(ValueError):
            export(buf,tmp_path/str(index))


def test_expiry_progresses_after_kernel_restart_even_while_blocked(book):
    ledger, clock = book
    ident=ledger.reserve(TOKEN_LIMIT)
    ledger.settle(ident,TOKEN_LIMIT,"{}")
    clock.boot="restarted"
    clock.value=0
    with pytest.raises(BudgetBlocked):
        ledger.reserve(1)
    clock.advance(86_400)
    ledger.reserve(1)
    assert ledger.status()["charged_and_reserved"] == 1


def test_cycle_does_not_turn_budget_refusal_into_convergence():
    from canary.cycle import run_cycle
    from canary.muse_client import RequestBlocked
    class Blocked:
        model=ALLOWED_MODEL
        def complete(self,*args,**kwargs):
            raise RequestBlocked("token_budget_exhausted")
    def sources(request):
        if "semanticscholar" in str(request.url):
            return httpx.Response(200,json={"data":[]})
        return httpx.Response(200,json={"results":[{"id":"x","title":"fixture"}]})
    with pytest.raises(RequestBlocked,match="token_budget_exhausted"):
        run_cycle("q",None,None,2,1,Blocked(),http=httpx.Client(transport=httpx.MockTransport(sources)))
