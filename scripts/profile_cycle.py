"""Bounded cycle profiling. Fixture results are not scientific evidence."""
import argparse
import cProfile
from dataclasses import asdict
import json
import os
from pathlib import Path
import platform
import pstats
import resource
import sys
import time
import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from canary.cycle import run_cycle
from canary.journal import Journal
from canary.muse_client import MuseClient
from canary.report import write_cycle_bundle

SEED = "How do urban tree canopy and reflective roofs affect daytime heat during heat waves?"


def fixture_clients(out):
    from boundary.gateway import Gateway
    from boundary.ledger import Ledger
    from boundary.policy import ALLOWED_MODEL
    ledger = Ledger.initialize(out / "fixture-usage.sqlite3")
    def provider(request):
        data = json.loads(request.content)
        system = data["messages"][0]["content"]
        if "research strategist" in system:
            text = json.dumps([
                {"question":"How does shade change pedestrian heat exposure?", "kind":"review", "rationale":"fixture"},
                {"question":"How do roof properties affect nighttime cooling?", "kind":"review", "rationale":"fixture"}])
        elif "senior researcher" in system:
            text = "Synthetic timing fixture only. No real-world findings were established. (iter 1)"
        else:
            text = "Synthetic timing fixture only; the supplied mock source is not scientific evidence [1]."
        # Explicitly simulated token usage, not tokenizer or live provider measurements.
        p = max(1, len(request.content)//4)
        c = max(1, len(text)//4) + 30
        return httpx.Response(200, json={"model": ALLOWED_MODEL,
            "choices":[{"message":{"content":text}, "finish_reason":"stop"}],
            "usage":{"prompt_tokens":p,"completion_tokens":c,"total_tokens":p+c,
                     "completion_tokens_details":{"reasoning_tokens":30}}})
    gate = Gateway(ledger, "fixture-only", httpx.MockTransport(provider))
    def relay(request):
        return httpx.Response(200, json=gate.complete(json.loads(request.content)))
    os.environ["CANARY_GATE_TOKEN"] = "fixture-only"
    os.environ["CANARY_GATE_URL"] = "http://fixture"
    muse = MuseClient(client=httpx.Client(transport=httpx.MockTransport(relay)))
    def literature(request):
        return httpx.Response(200, json={"results":[{"id":"FIXTURE-1",
            "title":"Urban canopy and reflective roofs: synthetic timing fixture",
            "publication_year":2025, "cited_by_count":0, "authorships":[],
            "primary_location":{}, "abstract_inverted_index":{"Synthetic":[0],"fixture":[1]}}]})
    return muse, httpx.Client(transport=httpx.MockTransport(literature)), ledger


def main():
    parser=argparse.ArgumentParser()
    mode=parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--fixture",action="store_true")
    mode.add_argument("--live",action="store_true")
    parser.add_argument("--out",required=True)
    parser.add_argument("--question",default=SEED)
    args=parser.parse_args()
    if not Path("/.dockerenv").exists():
        raise SystemExit("Run profiling in the disposable container")
    out=Path(args.out)
    out.mkdir(parents=True,exist_ok=False)
    ledger=None
    if args.fixture:
        muse, http, ledger=fixture_clients(out)
    else:
        muse=MuseClient()
        http=httpx.Client(trust_env=False,follow_redirects=False)
    journal=Journal(out/"journal.jsonl")
    profiler=cProfile.Profile()
    start=time.perf_counter()
    result=profiler.runcall(run_cycle,args.question,None,None,3,3,muse,http=http,journal=journal)
    elapsed=time.perf_counter()-start
    write_cycle_bundle(out,args.question,result,journal)
    stats=pstats.Stats(profiler)
    top=sorted(stats.stats.items(),key=lambda pair:pair[1][3],reverse=True)[:20]
    info={"mode":"offline-fixture" if args.fixture else "live", "seed":args.question,
          "iterations":len(result.iterations),"stopped":result.stopped,"model":muse.model,
          "wall_seconds":elapsed,"peak_rss_kib":resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
          "python":platform.python_version(),"requests":muse.calls,"receipts":muse.receipts,
          "measured_provider_tokens":not args.fixture,
          "ledger":ledger.status() if ledger else None,
          "top_functions":[{"function":key[2],"file":key[0],"line":key[1],
              "calls":val[1],"cumulative_seconds":val[3]} for key,val in top]}
    (out/"profile.json").write_text(json.dumps(info,indent=2)+"\n")
    muse.close()
    http.close()
    print(json.dumps(info,indent=2))


if __name__=="__main__":
    main()
