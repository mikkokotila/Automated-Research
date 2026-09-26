"""Disposable Docker checks. Uses a fake key and never permits provider egress."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import time
import uuid


def call(args, **kw):
    result=subprocess.run(["docker", *args],capture_output=True,text=True,timeout=120,**kw)
    if result.returncode:
        raise RuntimeError(result.stderr[-1500:])
    return result.stdout.strip()


def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--image",required=True)
    ap.add_argument("--gate-image",required=True)
    args=ap.parse_args()
    suffix=uuid.uuid4().hex[:10]
    network="canary-check-"+suffix
    volume=network+"-ledger"
    name=network+"-gate"
    result={}
    try:
        call(["network","create","--internal",network])
        call(["volume","create",volume])
        mount="type=volume,src="+volume+",dst=/state"
        call(["run","--rm","--network","none","--mount",mount,args.gate_image,"init"])
        call(["run","-d","--name",name,"--network",network,"--network-alias","canary-gate",
              "--read-only","--cap-drop","ALL","--security-opt","no-new-privileges",
              "--mount",mount,"-e","MUSE_API_KEY=fixture-only",args.gate_image,"serve"])
        key=call(["exec",name,"cat","/state/access.key"])
        environment=dict(os.environ,CANARY_GATE_TOKEN=key)
        ready=False
        for _ in range(30):
            try:
                call(["exec",name,"python","-I","-c",
                      "import socket; socket.create_connection(('127.0.0.1',8787),1).close()"])
                ready=True
                break
            except RuntimeError:
                time.sleep(.1)
        if not ready:
            raise RuntimeError("Boundary did not start")
        worker=["run","--rm","-i","--network",network,"--user","10002:10002",
                "--read-only","--cap-drop","ALL","--security-opt","no-new-privileges",
                "--tmpfs","/tmp:rw,size=64m","-e","CANARY_GATE_TOKEN",
                "--entrypoint","python",args.image,"-I","-"]
        probe = r"""
import json, os, socket
from pathlib import Path
import httpx
headers={"Authorization":"Bearer "+os.environ["CANARY_GATE_TOKEN"]}
r={"provider_key_absent":not any(os.environ.get(k) for k in ("MUSE_API_KEY","MODEL_API_KEY","META_API_KEY")),
   "ledger_absent":not Path('/state/usage.sqlite3').exists(),
   "docker_socket_absent":not Path('/var/run/docker.sock').exists()}
for host in ('api.meta.ai','1.1.1.1'):
    try:
        socket.create_connection((host,443),2).close()
        r['direct_egress_blocked_'+host]=False
    except OSError:
        r['direct_egress_blocked_'+host]=True
with httpx.Client(trust_env=False,timeout=5) as c:
    p={"model":"not-allowed","system":"s","user":"u","max_tokens":1}
    response=c.post('http://canary-gate:8787/v1/complete',json=p,headers=headers)
    r['wrong_model_blocked']=response.status_code==403
    response=c.get('http://canary-gate:8787/v1/models',headers=headers)
    r['alternate_endpoint_blocked']=response.status_code==403
print(json.dumps(r))
"""
        result.update(json.loads(call(worker,input=probe,env=environment)))
        fill="from boundary.ledger import Ledger; from boundary.policy import TOKEN_LIMIT; Ledger('/state/usage.sqlite3').reserve(TOKEN_LIMIT)"
        call(["exec",name,"python","-I","-c",fill])
        call(["restart",name])
        cap_probe=r"""
import json, os, time
import httpx
headers={"Authorization":"Bearer "+os.environ['CANARY_GATE_TOKEN']}
with httpx.Client(trust_env=False,timeout=5) as c:
    for n in range(30):
        try:
            status=c.get('http://canary-gate:8787/v1/status',headers=headers).json()
            break
        except httpx.HTTPError:
            time.sleep(.1)
    p={"model":"muse-spark-1.3-contributor","system":"s","user":"u","max_tokens":1}
    r=c.post('http://canary-gate:8787/v1/complete',json=p,headers=headers)
    print(json.dumps({'cap_survives_service_restart':status['charged_and_reserved']==200000000,
        'request_blocked_before_provider':r.status_code==429 and r.json()['error']=='token_budget_exhausted'}))
"""
        result.update(json.loads(call(worker,input=cap_probe,env=environment)))
        result['private_network']=json.loads(call(["network","inspect",network]))[0]['Internal'] is True
        info=json.loads(call(["inspect",name]))[0]
        result['no_host_bind_mounts']=not any(m['Type']=='bind' for m in info['Mounts'])
        result['no_published_ports']=not info['HostConfig']['PortBindings']
        print(json.dumps(result,indent=2))
        if not all(result.values()):
            raise SystemExit(1)
    finally:
        subprocess.run(["docker","rm","-f",name],capture_output=True)
        subprocess.run(["docker","volume","rm",volume],capture_output=True)
        subprocess.run(["docker","network","rm",network],capture_output=True)


if __name__=="__main__":
    main()
