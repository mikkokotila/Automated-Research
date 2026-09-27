"""Disposable Docker checks. Uses a fake key and never permits provider egress."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
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
    ap.add_argument("--report",default=None,help="write the machine-readable report here too")
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
        tree_before=subprocess.run(["git","status","--porcelain","--","src","tests"],
            capture_output=True,text=True,timeout=60).stdout
        result.update(launcher_checks(args.image,network,name))
        result.update(containment_checks(args.image,network))
        tree_after=subprocess.run(["git","status","--porcelain","--","src","tests"],
            capture_output=True,text=True,timeout=60).stdout
        result["source_checkout_untouched"]=tree_before==tree_after
        print(json.dumps(result,indent=2))
        if args.report:
            Path(args.report).write_text(json.dumps(result,indent=2),encoding="utf-8")
        if not all(result.values()):
            raise SystemExit(1)
    finally:
        subprocess.run(["docker","rm","-f",name],capture_output=True)
        subprocess.run(["docker","volume","rm",volume],capture_output=True)
        subprocess.run(["docker","network","rm",network],capture_output=True)


GUEST_FIXTURE = r"""
import json, subprocess, sys
from pathlib import Path
page = Path('/inputs/page.html').read_text()
r = subprocess.run([sys.executable, '-m', 'pip', 'install', '--quiet', '--no-index',
    '--no-deps', '--target', '/tmp/site', '--find-links', '/wheels', 'fixture_pkg'],
    capture_output=True, text=True, timeout=120)
assert r.returncode == 0, r.stderr[-500:]
sys.path.insert(0, '/tmp/site')
import fixture_pkg
locked = {}
for p in ('/etc/launcher-probe', '/app/launcher-probe'):
    try:
        Path(p).write_text('x')
        locked[p] = False
    except OSError:
        locked[p] = True
Path('/work/out/result.json').write_text(json.dumps(
    {'answer': fixture_pkg.answer(), 'page': page, 'locked': locked}))
"""

GUEST_NET_PROBE = r"""
import json, socket
from pathlib import Path
out = {}
try:
    socket.create_connection(('169.254.169.254', 80), 3).close()
    out['metadata_blocked'] = False
except OSError:
    out['metadata_blocked'] = True
try:
    socket.getaddrinfo('example.com', 443)
    out['public_dns_blocked'] = False
except socket.gaierror:
    out['public_dns_blocked'] = True
Path('/work/out/net.json').write_text(json.dumps(out))
"""


ISOLATION_PROBE = r"""
import json, shutil, socket
from pathlib import Path
r = {}
pids = [p for p in Path('/proc').iterdir() if p.name.isdigit()]
r['pid_namespace_small'] = len(pids) < 50
try:
    Path('/sys/fs/cgroup/pids.max').write_text('1000000')
    r['cgroup_locked'] = False
except OSError:
    r['cgroup_locked'] = True
r['no_docker_cli'] = shutil.which('docker') is None
for ip in ('10.0.0.1', '192.168.1.1', 'fe80::1'):
    try:
        socket.create_connection((ip, 80), 2).close()
        r['blocked_' + ip] = False
    except OSError:
        r['blocked_' + ip] = True
print(json.dumps(r))
"""

PIDS_PROBE = r"""
import json, subprocess
from pathlib import Path
procs, limited = [], False
try:
    for _ in range(400):
        try:
            procs.append(subprocess.Popen(['sleep', '30']))
        except OSError:
            limited = True
            break
finally:
    for p in procs:
        p.kill()
    for p in procs:
        p.wait()
print(json.dumps({'pids_limited': limited, 'spawned': len(procs)}))
"""

EGRESS_PROBE = r"""
import json, socket
try:
    socket.create_connection(('1.1.1.1', 443), 5).close()
    print(json.dumps({'egress_open': True}))
except OSError:
    print(json.dumps({'egress_open': False}))
"""


def containment_checks(image, network):
    """Build 04: isolation depth plus a negative control for the egress probe."""
    checks = {}
    worker = ["run", "--rm", "-i", "--network", network, "--user", "10002:10002",
              "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
              "--pids-limit", "256", "--tmpfs", "/tmp:rw,size=64m",
              "--entrypoint", "python", image, "-I", "-"]
    checks.update(json.loads(call(worker, input=ISOLATION_PROBE)))
    bomb = json.loads(call(worker, input=PIDS_PROBE))
    checks["pids_limit_enforced"] = bomb["pids_limited"] is True and bomb["spawned"] < 400
    # Negative control: the same probe on an egress-enabled worker MUST see
    # openness, proving the denials above are real detections, not vacuous.
    unsafe = ["run", "--rm", "-i", "--network", "bridge",
              "--entrypoint", "python", image, "-I", "-"]
    checks["egress_probe_detects_open_network"] = json.loads(
        call(unsafe, input=EGRESS_PROBE))["egress_open"] is True
    return checks


def launcher_checks(image, network, gate):
    """Build 03: drive the real launcher; fail closed on every unsafe path."""
    root = Path(__file__).resolve().parent.parent
    checks = {}
    tmp = Path(tempfile.mkdtemp(prefix="launcher-"))
    try:
        base = dict(os.environ, IMG=image, CANARY_NETWORK=network,
                    CANARY_GATE_NAME=gate, CANARY_SKIP_BUILD="1",
                    ALLOW_DIRTY="1")  # verify checks containment, not tree state

        def launch(out, args, extra=None, timeout=420):
            env = dict(base, OUT=str(out), CANARY_TIMEOUT_S="120")
            env.update(extra or {})
            proc = subprocess.run(["bash", "scripts/container_run.sh", *args],
                                  cwd=root, env=env, capture_output=True,
                                  text=True, timeout=timeout)
            if proc.returncode != 0 and not (extra or {}).get("EXPECT_FAIL"):
                raise RuntimeError(f"launcher {args} exited {proc.returncode}: "
                                   f"{proc.stdout[-800:]} {proc.stderr[-800:]}")
            return proc

        def receipt(out):
            return json.loads(Path(str(out) + ".receipt.json").read_text())

        wheels = tmp / "wheels"
        wheels.mkdir()
        subprocess.run([sys.executable, "scripts/make_fixture_wheel.py",
                        "--out", str(wheels)], cwd=root, check=True,
                       capture_output=True, timeout=60)
        page = tmp / "page.html"
        page.write_text("<title>staged fixture page</title>", encoding="utf-8")

        # 1. staged input + wheel install + output write + receipt
        out1 = tmp / "out1"
        r1 = launch(out1, ["exec", "python", "-c", GUEST_FIXTURE],
                    {"CANARY_WHEELS": str(wheels), "CANARY_STAGE": f"{page}:page.html"})
        rec1 = receipt(out1)
        res1 = json.loads((out1 / "result.json").read_text())
        checks["launcher_exit_zero"] = r1.returncode == 0
        checks["receipt_records_image"] = rec1["IMAGE_ID"] == call(
            ["inspect", "-f", "{{.Id}}", image]) and rec1["GIT_REV"] != ""
        checks["receipt_records_outcome"] = (
            rec1["RC"] == "0" and rec1["TIMED_OUT"] == "false"
            and rec1["HAVE_WHEELS"] == "true" and "page.html" in rec1["STAGED"])
        checks["guest_installed_wheel"] = res1["answer"] == 42
        checks["guest_read_staged_input"] = "staged fixture page" in res1["page"]
        checks["guest_image_locked"] = all(res1["locked"].values())

        # 2. independent workspaces: second run sees a fresh out-volume
        out2 = tmp / "out2"
        r2 = launch(out2, ["exec", "python", "-c",
                           "from pathlib import Path; print(sorted(p.name for p in Path('/work/out').iterdir()))"])
        checks["workspaces_independent"] = (
            r2.returncode == 0 and "[]" in r2.stdout
            and {p.name for p in out2.iterdir()} <= {"manifest.canary.json"})

        # 3. forbidden destinations fail closed from the guest
        out3 = tmp / "out3"
        r3 = launch(out3, ["exec", "python", "-c", GUEST_NET_PROBE])
        net = json.loads((out3 / "net.json").read_text())
        checks["guest_metadata_blocked"] = r3.returncode == 0 and net["metadata_blocked"] is True
        checks["guest_public_dns_blocked"] = net["public_dns_blocked"] is True

        # 4. wall-time kills the guest tree and releases resources
        out4 = tmp / "out4"
        r4 = launch(out4, ["exec", "sleep", "60"],
                    {"CANARY_TIMEOUT_S": "8", "EXPECT_FAIL": "1"}, timeout=300)
        rec4 = receipt(out4)
        gone = subprocess.run(["docker", "inspect", rec4["NAME"]],
                              capture_output=True, timeout=60).returncode != 0
        vols = subprocess.run(["docker", "volume", "ls", "-q", "--filter",
                               "name=" + rec4["NAME"]], capture_output=True,
                              text=True, timeout=60).stdout.strip()
        checks["timeout_kills_and_releases"] = (
            r4.returncode != 0 and rec4["TIMED_OUT"] == "true" and gone and vols == "")

        # 5. unsafe launch configurations fail closed before anything starts
        for bad in ("host", "bridge"):
            outb = tmp / f"out-bad-{bad}"
            rb = launch(outb, ["exec", "true"],
                        {"CANARY_NETWORK": bad, "EXPECT_FAIL": "1"}, timeout=120)
            checks[f"unsafe_network_{bad}_refused"] = (
                rb.returncode != 0 and not Path(str(outb) + ".receipt.json").exists())

        # 6. stage paths containing colons split on the LAST colon (URL-safe)
        colon = tmp / "with:colon.txt"
        colon.write_text("colon-split-ok", encoding="utf-8")
        outc = tmp / "out-colon"
        rc = launch(outc, ["exec", "python", "-c",
                           "from pathlib import Path; "
                           "Path('/work/out/echo.txt').write_text(Path('/inputs/echo.txt').read_text())"],
                    {"CANARY_STAGE": f"{colon}:echo.txt"})
        checks["colon_stage_split"] = (
            rc.returncode == 0
            and (outc / "echo.txt").read_text() == "colon-split-ok")

        # 7. unreachable stage URL fails loudly before anything starts
        outu = tmp / "out-unreachable-stage"
        ru = launch(outu, ["exec", "true"],
                    {"CANARY_STAGE": "https://127.0.0.1:9/x:evil.html",
                     "EXPECT_FAIL": "1"}, timeout=120)
        checks["unreachable_stage_refused"] = (
            ru.returncode != 0 and not Path(str(outu) + ".receipt.json").exists())

        # 8. exported leaks are caught by the maintainer-side scanner
        outs = tmp / "out-scan"
        rs = launch(outs, ["exec", "python", "-c",
                           "from pathlib import Path; "
                           "Path('/work/out/leak.txt').write_text('token ghp_fixture_planted_0123456789abcdef')"])
        sys.path.insert(0, str(root))
        from scripts.scan_export import scan_export
        flagged = scan_export(outs)
        checks["scanner_flags_exported_leak"] = (
            rs.returncode == 0 and not flagged["clean"]
            and flagged["findings"] == [{"file": "leak.txt", "pattern": "github_token"}]
            and flagged["manifest_problems"] == [])

        # 9. tripwire: launcher never imports candidate code or evals guest shell
        text = (root / "scripts" / "container_run.sh").read_text()
        checks["launcher_imports_nothing"] = (
            "import canary" not in text and "from canary" not in text
            and "\neval " not in text)
        return checks
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
