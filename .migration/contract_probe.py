"""Deterministic application contract probe; no live services."""
import contextlib, dataclasses, datetime, importlib.util, io, json, os, subprocess, sys
from pathlib import Path
import httpx, numpy as np, pandas as pd, pytest
from autoresearch import analysis, cli, data, github_ops, improve, journal, loop, modeling, rank, reflect, report, retrieval, synthesize
from autoresearch.spec import ResearchSpec

class Clock(datetime.datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 9, 26, 12, 0, 0, tzinfo=tz)
for module in (journal, report, improve):
    module.datetime = Clock
os.environ.pop("GITHUB_TOKEN", None)
os.environ.pop("GITHUB_REPO", None)
root=Path("/tmp/contracts"); root.mkdir()
results={}
requests=[]

def http_handler(request):
    requests.append({"method":request.method,"url":str(request.url)})
    if "openalex" in str(request.url):
        return httpx.Response(200,json={"results":[{"id":"W1","title":"Study on treatment timing","doi":"https://doi.org/10.1/example","publication_year":2024,"cited_by_count":12,"authorships":[{"author":{"display_name":"A Author"}}],"primary_location":{"source":{"display_name":"Journal"}},"abstract_inverted_index":{"Treatment":[0],"timing":[1],"evidence":[2]}}]})
    return httpx.Response(200,json={"data":[]})

class Service:
    model="fixture-service"
    def __init__(self, followups=None):
        self.followups=list(followups or []); self.calls=[]
    def complete(self, system, user, max_tokens=8000):
        self.calls.append({"system":system,"user":user,"max_tokens":max_tokens})
        if system==loop.FOLLOWUP_SYSTEM:
            return json.dumps(self.followups.pop(0) if self.followups else [])
        if system==reflect.SYSTEM:
            return json.dumps({"reflection":"## Assessment\nNo changes needed.","proposals":[]})
        if system==analysis.SYSTEM: return "The measured results have limitations."
        if system==loop.SYNTHESIS_SYSTEM: return "Evidence is limited (iter 1)."
        return "The supplied study describes timing [1]."

def plain(value):
    if dataclasses.is_dataclass(value): return plain(dataclasses.asdict(value))
    if isinstance(value,dict): return {str(k):plain(v) for k,v in value.items()}
    if isinstance(value,(tuple,list)): return [plain(x) for x in value]
    if isinstance(value,Path): return str(value)
    if isinstance(value,np.ndarray): return value.tolist()
    if isinstance(value,np.generic): return value.item()
    return value

def attempt(name, fn):
    try: results[name]={"value":plain(fn())}
    except Exception as exc: results[name]={"error":type(exc).__name__,"message":str(exc)}

def bundle(directory):
    return {str(p.relative_to(directory)):p.read_text() for p in sorted(directory.rglob("*")) if p.is_file()}

for i,kw in enumerate([{"question":"  timing  "},{"question":""},{"question":"q","max_papers":0},{"question":"q","year_from":1800}]):
    attempt("spec-"+str(i),lambda kw=kw:ResearchSpec(**kw))
for i,s in enumerate(["[]","bad",'[{}]', '[{"question":"why?","kind":"review","rationale":"gap"}]']):
    attempt("followups-"+str(i),lambda s=s:loop.parse_followups(s))
for i,s in enumerate(["bad",'{"reflection":"OK","proposals":[]}', '{"reflection":"Review","proposals":[{"target":"src/autoresearch/foo.py","change":"fix","reason":"r"}]}']):
    attempt("assessment-"+str(i),lambda s=s:reflect.parse_reflection(s))

with httpx.Client(transport=httpx.MockTransport(http_handler)) as http:
    papers=retrieval.retrieve(ResearchSpec("timing"),http)
    results["retrieval"]=plain(papers)
    top=rank.rerank("timing",papers,3); results["ranking"]=plain(top)
    service=Service(); syn=synthesize.synthesize("timing",top,service)
    dest=report.write_bundle(root/"review",ResearchSpec("timing"),top,syn)
    results["review-bundle"]=bundle(dest); results["review-prompts"]=service.calls
    for name,followups,maximum in [("converged",[],3),("bounded",[[{"question":"more timing?","kind":"review","rationale":"gap"}]],2)]:
        service=Service(followups); j=journal.Journal(root/name/"journal.jsonl")
        res=loop.run_loop("timing",None,None,maximum,3,service,http=http,journal=j)
        report.write_loop_bundle(root/name,"timing",res,j)
        results[name]={"result":plain(res),"bundle":bundle(root/name),"calls":service.calls}
results["requests"]=requests

rng=np.random.RandomState(7)
x=rng.normal(size=100)
for name,y in [("classification",(x>0).astype(int)),("regression",3*x+rng.normal(0,.1,100))]:
    frame=pd.DataFrame({"value":x,"category":np.where(x>0,"a","b"),"target":y})
    csv=root/(name+".csv"); frame.to_csv(csv,index=False)
    prep=data.prepare(data.load_csv(csv),"target"); scores=modeling.run(prep)
    service=Service(); findings=analysis.narrate("what predicts target?",prep,scores,service)
    dest=report.write_analysis_bundle(root/name,"what predicts target?",str(csv),"target",prep,scores,findings)
    results[name]={"profile":plain(prep.profile),"scores":plain(scores),"report":bundle(dest),"calls":service.calls}


real_client=httpx.Client
with pytest.MonkeyPatch.context() as mp:
    mp.setattr(httpx,"Client",lambda *a,**kw:real_client(*a,transport=httpx.MockTransport(http_handler),**kw))
    mp.setattr(cli,"MuseClient",Service)
    for i,args in enumerate([["--help"],["review","--help"],["loop","--help"],["improve","--help"],[],["bad-command"],["review","timing","--out",str(root/"cli-review")],["analyze",str(root/"classification.csv"),"--target","target","--out",str(root/"cli-analysis")],["loop","timing","--max-iterations","2","--out",str(root/"cli-loop")],["loop","timing","--max-iterations","0"]]):
        output=io.StringIO(); errors=io.StringIO()
        with contextlib.redirect_stdout(output),contextlib.redirect_stderr(errors):
            try: status=cli.main(args)
            except SystemExit as exc: status=exc.code
        results["cli-"+str(i)]={"status":status,"stdout":output.getvalue(),"stderr":errors.getvalue()}
    for folder in ("cli-review","cli-analysis","cli-loop"):
        results[folder+"-files"]=bundle(root/folder)

for name in ("foo.py","../bad.py","src/autoresearch/foo.py","src/autoresearch/improve.py","tests/test_x.py"):
    attempt("path-"+name,lambda name=name:improve.target_allowed(name))

fixture=importlib.util.spec_from_file_location("publication_fixture",Path("/work/tests/test_autonomy.py"))
module=importlib.util.module_from_spec(fixture); fixture.loader.exec_module(module)
for verdict in ("success","failure","no-change"):
    checkout=module.repo.__wrapped__(root/("publish-"+verdict))
    if verdict!="no-change":
        (checkout/"src/autoresearch/foo.py").write_text("X = 2\n")
    fake=module.FakeGitHubAPI(checks_script=(verdict if verdict!="no-change" else "success",))
    doc=reflect.ReflectionDoc("## Assessment",())
    outcome=improve.ImproveReport(kept=0 if verdict=="no-change" else 1)
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("GITHUB_TOKEN","fixture-only")
        publication=improve.publish_round(checkout,"fixture-id",doc,outcome,journal.Journal(),fake.client(),merge_timeout_s=3,merge_interval_s=0)
    results["publish-"+verdict]={"result":plain(publication),"requests":fake.calls,"bodies":fake.bodies,"files":bundle(checkout/"runs")}

print("CONTRACT_RESULT="+json.dumps(results,sort_keys=True,allow_nan=False))
