# -*- coding: utf-8 -*-
"""_r329_farm_us.py N tagbase [--nodes 22023,22030,22031,22032,22033] [--invite CODE]
US-exit registration -> 4-task cohort (340 credits) -> claim -> pp_acc file.
"""
import argparse, glob, io, json, os, subprocess, sys, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _pp_http as H

OUT = os.path.join(H.C, "outputs", "evidence_r323")
POOL = os.environ.get("PP_FARM_POOL") or os.path.join(OUT, "pp_farm_pool.json")
REG = os.path.join(os.environ.get("CODEX_HOME", r"<CODEX_HOME>"), "work", "_r329_farm_reg.py")

def log(*a):
    print(time.strftime("[%H:%M:%S] ") + " ".join(str(x) for x in a), flush=True)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("n", type=int); ap.add_argument("tag")
    ap.add_argument("--nodes", default="22031,22031,22033")
    ap.add_argument("--invite", default="<INVITE_CODE>")
    ap.add_argument("--no-claim", action="store_true")
    a = ap.parse_args()
    nodes = [int(x) for x in a.nodes.split(",") if x]
    env = dict(os.environ); env["PP_REG"] = "US"
    okc = 0
    for i in range(a.n):
        port = nodes[i % len(nodes)]
        tag = "%s_%d" % (a.tag, i + 1)
        t0 = time.time()
        p = subprocess.run([H.PY, "-u", REG, str(port), a.invite, "1", tag], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=300, env=env)
        tail = [x for x in (p.stdout or "").splitlines() if "ok=" in x]
        log("%s node=%d reg %.0fs %s" % (tag, port, time.time() - t0, " | ".join(tail)[-160:]))
        try:
            pool = json.load(io.open(POOL, encoding="utf-8-sig"))
        except Exception:
            pool = []
        ent = None
        for e in reversed(pool):
            if e.get("tag") == tag and e.get("ok") and e.get("cookies"):
                ent = e; break
        if not ent:
            log("%s REG FAIL" % tag); continue
        fp = os.path.join(OUT, "pp_acc_%s.json" % tag)
        io.open(fp, "w", encoding="utf-8").write(json.dumps(ent, ensure_ascii=False, indent=1))
        if not a.no_claim:
            try:
                clf = os.path.join(H.C, "work", "farm_claim_%s.out" % tag)
                with io.open(clf, "w", encoding="utf-8", errors="replace") as cf:
                    p2 = subprocess.run([H.PY, "-u", os.path.join(H.C, "work", "_r329_claim2.py"), fp, str(port),
                                         os.path.join(H.C, "work", "pp_prof_claim2_%s" % tag)],
                                        stdout=cf, stderr=subprocess.STDOUT, timeout=600)
                try:
                    tail2 = [x for x in io.open(clf, encoding="utf-8", errors="replace").read().splitlines() if x.strip()][-2:]
                except Exception:
                    tail2 = []
                if p2.returncode != 0 or tail2:
                    log("%s claim rc=%s %s" % (tag, p2.returncode, " | ".join(tail2)[-160:]))
            except subprocess.TimeoutExpired:
                log("%s claim TIMEOUT 600s (will be claimed later by line)" % tag)
        try:
            c = H.PP(ent["cookies"], [port]).credit()["total"]
        except Exception:
            c = -1
        log("%s uid=%s credit=%d file=%s" % (tag, ent.get("uid"), c, os.path.basename(fp)))
        if c == 0:
            try:
                qf = os.path.join(H.C, "work", "claim_zero.json")
                try:
                    q = json.load(io.open(qf, encoding="utf-8"))
                except Exception:
                    q = []
                if not any(str(x.get("uid")) == str(ent["uid"]) for x in q):
                    q.append({"uid": str(ent["uid"]), "file": os.path.basename(fp), "port": port,
                              "ts": time.time(), "tries": 0})
                    io.open(qf, "w", encoding="utf-8").write(json.dumps(q, ensure_ascii=False, indent=1))
                    log("%s queued for claim retry" % tag)
            except Exception as e:
                log("%s claim_zero queue err %r" % (tag, e))
        if c >= 120: okc += 1
    log("DONE ok=%d/%d" % (okc, a.n))

main()
