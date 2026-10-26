# -*- coding: utf-8 -*-
"""_r329_prod.py - one-account production run: credit -> wm flag -> agent 30s -> thread -> clean master -> gate -> ledger.
exit: 0 ok | 2 no credit | 3 no thread | 4 no clean url | 5 quality gate failed
"""
import argparse, io, json, os, re, shutil, subprocess, sys, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _pp_http as H

C = H.C
W = os.path.join(C, "work")
OUT = os.path.join(C, "outputs", "evidence_r323")
FINAL = os.path.join(H.DL, "final")
LEDGER = os.path.join(H.DL, "manifest.jsonl")

def save_rec(rec):
    try:
        with io.open(LEDGER, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception:
        pass
PY = H.PY

def log(tag, *a):
    print(time.strftime("[%H:%M:%S] ") + ("[%s] " % tag) + " ".join(str(x) for x in a), flush=True)

def run(args, timeout, tag, name):
    t0 = time.time()
    if name == "agent":
        # Redirect to a file instead of PIPE: a hung Chrome grandchild inherits
        # the pipe and would otherwise block communicate() forever after timeout.
        lf = os.path.join(OUT, "agent_%s_stdout.txt" % tag)
        try:
            with io.open(lf, "w", encoding="utf-8", errors="replace") as f:
                p = subprocess.run(args, stdout=f, stderr=subprocess.STDOUT, timeout=timeout)
            log(tag, "%s rc=%s %.0fs (stdout->%s)" % (name, p.returncode, time.time() - t0, os.path.basename(lf)))
            return p.returncode, ""
        except subprocess.TimeoutExpired:
            log(tag, "%s TIMEOUT %.0fs" % (name, time.time() - t0))
            return -9, ""
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout,
                           encoding="utf-8", errors="replace")
        tail = (p.stdout or "").strip().splitlines()[-3:]
        log(tag, "%s rc=%s %.0fs %s" % (name, p.returncode, time.time() - t0, " | ".join(tail)[-400:]))
        return p.returncode, p.stdout or ""
    except subprocess.TimeoutExpired:
        log(tag, "%s TIMEOUT %.0fs" % (name, time.time() - t0))
        return -9, ""

def resolve_thread(tag):
    fp = os.path.join(OUT, "thread_%s.txt" % tag)
    try:
        tid = io.open(fp, encoding="utf-8").read().strip()
        if re.match(r"^[0-9a-f-]{36}$", tid): return tid
    except Exception: pass
    for nm in ("agent_%s_net.log" % tag, "agent_%s.log" % tag):
        try:
            s = io.open(os.path.join(OUT, nm), encoding="utf-8", errors="replace").read()
        except Exception:
            continue
        m = re.findall(r"threadId=([0-9a-fA-F-]{36})", s) or re.findall(r"/thread/([0-9a-fA-F-]{36})", s)
        if m:
            return m[-1]
        return ""
    return ""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("acct"); ap.add_argument("tag"); ap.add_argument("prompt")
    ap.add_argument("--secs", default="30"); ap.add_argument("--res", default="1080p")
    ap.add_argument("--ratio", default="9:16"); ap.add_argument("--port", type=int, default=21030)
    ap.add_argument("--no-claim", action="store_true"); ap.add_argument("--no-gen", action="store_true")
    ap.add_argument("--thread", default=""); ap.add_argument("--expect", default="1080x1920")
    ap.add_argument("--secs-expect", type=float, default=30.0)
    ap.add_argument("--min-credit", type=int, default=120)
    ap.add_argument("--low-credit", action="store_true", help="allow 120-199 credit accounts (1 round)")
    a = ap.parse_args()
    # 30s/1080p needs 120 upfront plus tail-composition headroom; keep a safe floor
    # so a 180-credit account cannot burn 120 and then fail at final composition.
    need = max(120, int(float(a.secs) * 4) + 80)
    if a.low_credit:
        need = 120
        # R-WZ-561: line passes --min-credit 200 for headroom, but a stranded
        # 120-199 account must be allowed one low-credit round. Lower the floor.
        a.min_credit = 120
    _force = int(os.environ.get("PP_FORCE_MIN_CREDIT", "0") or 0)
    if _force > 0:
        need = _force
    if a.min_credit < need:
        a.min_credit = need
    tag = a.tag
    # Unique tags are expected, but clear stale artifacts defensively so a reused
    # tag can never resolve an old thread and report no_asset.
    for nm in ("thread_%s.txt" % tag, "agent_status_%s.txt" % tag,
               "agent_%s_net.log" % tag, "agent_%s.log" % tag,
               "agent_%s_stdout.txt" % tag):
        try: os.remove(os.path.join(OUT, nm))
        except Exception: pass
    d = json.load(io.open(a.acct, encoding="utf-8-sig"))
    pp = H.PP(d.get("cookies") or {}, [a.port] if not a.port else [a.port] + [p for p in H.PORTS if p != a.port])
    os.makedirs(FINAL, exist_ok=True)
    ew, eh = [int(x) for x in a.expect.lower().split("x")]
    rec = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "tag": tag, "acct": os.path.basename(a.acct),
           "uid": d.get("uid"), "port": a.port, "prompt": a.prompt[:200], "secs": a.secs,
           "expect": a.expect}
    try:
        c = pp.credit(); log(tag, "credit", json.dumps(c))
        if c["total"] < a.min_credit and not a.no_claim:
            log(tag, "claiming tasks (playwright)")
            run([PY, "-u", os.path.join(W, "_r329_claim2.py"), a.acct, str(a.port),
                 os.path.join(W, "pp_prof_claim2_%s" % re.sub(r"\W+", "", tag))], 420, tag, "claim")
            time.sleep(3); c = pp.credit(); log(tag, "credit after claim", json.dumps(c))
        rec["credit"] = c
        if c["total"] < a.min_credit:
            log(tag, "NO CREDIT (%d < %d)" % (c["total"], a.min_credit)); rec["status"] = "no_credit"; save_rec(rec); return 2
        try:
            j = pp.set_wmban(True); log(tag, "wmban ret=%s" % j.get("ret"))
        except Exception as e:
            log(tag, "wmban ERR", repr(e)[:80])
    except Exception as e:
        log(tag, "preflight ERR", repr(e)[:140]); rec["status"] = "preflight_err"; rec["err"] = repr(e)[:200]; return 2

    tid = a.thread.strip()
    gen_start_ms = int(time.time() * 1000)
    recovered = None
    recovered_name = ""
    if not a.no_gen:
        pr = a.prompt
        if pr.startswith("@"):
            path = pr[1:]
            if not os.path.isabs(path): path = os.path.join(C, path)
            pr = io.open(path, encoding="utf-8").read().strip()
            log(tag, "prompt file=%s chars=%d" % (os.path.basename(path), len(pr)))
        stf = os.path.join(OUT, "agent_status_%s.txt" % tag)
        try: os.remove(stf)
        except Exception: pass
        run([PY, "-u", os.path.join(W, "_r329_agent.py"), a.acct, pr, a.secs, tag,
             str(a.port), a.res, a.ratio], 2700, tag, "agent")
        try: ast = io.open(stf, encoding="utf-8").read().strip()
        except Exception: ast = ""
        log(tag, "agent_status=%s" % ast)
        if ast == "wall":
            rec["status"] = "gen_wall"; save_rec(rec); return 6
        if ast == "render_fail":
            rec["status"] = "render_fail"; save_rec(rec); return 7
        tid = resolve_thread(tag) or tid
    log(tag, "thread=%s" % tid); rec["thread"] = tid

    def recover_project_asset(reason):
        nonlocal recovered, recovered_name
        try:
            ra = pp.recent_assets(gen_start_ms, 5)
        except Exception as e:
            log(tag, "project recover ERR", repr(e)[:140]); ra = []
        if not ra:
            log(tag, "project recover: none after %s" % reason); return None
        recovered = ra[0]
        recovered_name = recovered.get("name") or ""
        rec["recovered"] = "project_file"
        rec["project_id"] = recovered.get("project_id")
        log(tag, "project recover %s -> aid=%s project=%s name=%s" %
            (reason, recovered["asset_id"], recovered.get("project_id"), recovered_name[:60]))
        return recovered["asset_id"]

    assets = []
    if tid:
        for attempt in range(4):
            try:
                assets, j = pp.thread_assets(tid)
            except Exception as e:
                log(tag, "get_thread ERR", repr(e)[:100]); assets = []
            if assets: break
            log(tag, "no asset yet, wait %ds" % (15 * (attempt + 1))); time.sleep(15 * (attempt + 1))
    else:
        aid0 = recover_project_asset("no_thread")
        if aid0: assets = [aid0]
    if not assets:
        aid0 = recover_project_asset("no_asset" if tid else "no_thread")
        if aid0: assets = [aid0]
    log(tag, "assets", assets)
    rec["assets"] = assets
    if not assets:
        rec["status"] = "no_thread" if not tid else "no_asset"
        save_rec(rec); return 3 if not tid else 4
    aid = assets[-1]; rec["asset_id"] = aid
    url, meta_a = None, None
    for attempt in range(4):
        try:
            url, meta_a = pp.clean_url(aid)
        except Exception as e:
            log(tag, "asset/query ERR", repr(e)[:100])
        if url: break
        time.sleep(10)
    if not url:
        rec["status"] = "no_clean_url"; save_rec(rec); return 4
    rec["asset_name"] = (meta_a or {}).get("Name") or recovered_name
    rec["clean_url"] = url[:300]
    dest = os.path.join(H.DL, "CLEAN_%s_%d.mp4" % (tag, int(time.time())))
    try:
        n = pp.download_fast(url, dest)
        log(tag, "downloaded", dest, n)
    except Exception as e:
        log(tag, "DOWNLOAD FAIL", repr(e)[:120]); rec["status"] = "dl_fail"; rec["err"] = repr(e)[:200]; save_rec(rec); return 4
    m = H.probe(dest); w = H.wm_check(dest)
    log(tag, "probe", json.dumps(m)); log(tag, "wm", json.dumps(w, ensure_ascii=False))

    # R-WZ-67m: late-finishing render recovery. A short probe usually means the
    # platform returned the first segment while the rest was still encoding.
    try:
        exp_s = float(a.secs_expect)
    except Exception:
        exp_s = 30.0
    if float(m.get("dur") or 0) < exp_s - 1.5:
        seen_ids = set(assets)
        for _late in range(6):
            time.sleep(30)
            if not tid:
                log(tag, "late poll: no thread id; stop"); break
            try:
                a2, _j2 = pp.thread_assets(tid)
            except Exception:
                a2 = []
            new_ids = [x for x in (a2 or []) if x not in seen_ids]
            if not new_ids:
                log(tag, "late poll: no new asset (%d)" % (_late + 1)); continue
            aid2 = new_ids[-1]; seen_ids.add(aid2)
            try:
                url2, _m2 = pp.clean_url(aid2)
            except Exception:
                url2 = None
            if not url2:
                continue
            dest2 = os.path.join(H.DL, "CLEAN_%s_%d_late.mp4" % (tag, int(time.time())))
            try:
                pp.download_fast(url2, dest2)
                m2 = H.probe(dest2)
            except Exception as e:
                log(tag, "late dl ERR", repr(e)[:120]); continue
            log(tag, "late asset", aid2, json.dumps(m2))
            if float(m2.get("dur") or 0) > float(m.get("dur") or 0):
                dest, m, aid = dest2, m2, aid2
                w = H.wm_check(dest)
                log(tag, "late adopted", json.dumps(m))
            if float(m.get("dur") or 0) >= exp_s - 1.5:
                break
    ok, errs = H.gate(m, w, ew, eh, a.secs_expect)
    rec.update({"file": dest, "probe": m, "wm": w, "gate_ok": ok, "gate_errs": errs})
    if ok:
        slug = re.sub(r"[^A-Za-z0-9]+", "_", os.path.basename(rec.get("asset_name") or "video"))[:48].strip("_")
        fn = "%s_%s_%dx%d_%.3fs.mp4" % (tag, slug or "clip", m["w"], m["h"], m["dur"])
        fp = os.path.join(FINAL, fn)
        shutil.copy2(dest, fp); rec["final"] = fp; rec["status"] = "ok"
        log(tag, "FINAL", fp)
        rc = 0
    else:
        rec["status"] = "gate_fail"; rc = 5
    save_rec(rec)
    with io.open(os.path.splitext(dest)[0] + ".json", "w", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False, indent=1))
    return rc

sys.exit(main())
