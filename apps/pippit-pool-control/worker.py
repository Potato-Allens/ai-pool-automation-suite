# -*- coding: utf-8 -*-
"""Background workers: claim a queued job, run the proven _r329_prod pipeline, publish the mp4."""
import io, json, os, re, shutil, subprocess, sys, threading, time
HERE = os.path.dirname(os.path.abspath(__file__))
C = r"<PROJECT_ROOT>"
sys.path.insert(0, os.path.join(C, "work")); sys.path.insert(0, HERE)
import db, pool, recover  # noqa: E402

OUTAPI = r"<OUTPUT_DIR>\api"
FINAL = os.path.join(C, "outputs", "R329_pippit", "final")
LOGD = os.path.join(HERE, "logs")
os.makedirs(OUTAPI, exist_ok=True); os.makedirs(LOGD, exist_ok=True)

PORTS = [int(x) for x in os.environ.get("API_PORTS", "22023,22021").split(",") if x.strip()]
WORKERS = int(os.environ.get("API_WORKERS", "2"))
MIN_FREE_GB = float(os.environ.get("API_MIN_FREE_GB", "5"))
COST = 120

def log(wid, *a):
    s = time.strftime("[%H:%M:%S] ") + " ".join(str(x) for x in a)
    print("[%s] %s" % (wid, s), flush=True)
    with io.open(os.path.join(LOGD, "worker.log"), "a", encoding="utf-8") as f:
        f.write("[%s] %s\n" % (wid, s))

def free_gb():
    try:
        import psutil
        return psutil.virtual_memory().available / 1024.0 ** 3
    except Exception:
        return 99.0

def run_job(wid, jid, port):
    job = db.get_job(jid)
    if not job:
        return
    if db.is_canceled(jid):
        db.finish_job(jid, "canceled", error="canceled before start"); return
    try:
        excluded = set(json.loads(job.get("tried") or "[]"))
    except Exception:
        excluded = set()
    acct, lf = pool.pick(excluded)
    if not acct:
        db.requeue_job(jid, "no free account")
        time.sleep(20); return
    tag = "API_%s" % jid.replace("vid_", "")[:10]
    outlog = os.path.join(LOGD, "%s.out" % tag)
    cmd = [sys.executable, "-u", os.path.join(C, "work", "_r329_prod.py"),
           acct["path"], tag, job["prompt"],
           "--secs", "%g" % job["duration"], "--res", job["resolution"],
           "--ratio", job["ratio"], "--port", str(port)]
    log(wid, "start", jid, "tag", tag, "uid", acct["uid"], "port", port)
    t0 = time.time(); rc = -1
    try:
        with io.open(outlog, "w", encoding="utf-8", errors="replace") as f:
            p = subprocess.Popen(cmd, cwd=C, stdout=f, stderr=subprocess.STDOUT)
            try:
                rc = p.wait(timeout=2700)
            except subprocess.TimeoutExpired:
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
                rc = -9
    except Exception as e:
        log(wid, "spawn err", repr(e)[:120])
    dt = time.time() - t0
    txt = ""
    try:
        txt = io.open(outlog, encoding="utf-8", errors="replace").read()
    except Exception:
        pass
    m = re.findall(r"FINAL\s+(\S+\.mp4)", txt)
    final = m[-1] if m else None
    # fallback: newest final file with this tag prefix
    if not final:
        try:
            cand = [os.path.join(FINAL, n) for n in os.listdir(FINAL) if n.startswith(tag + "_")]
            if cand:
                final = max(cand, key=os.path.getmtime)
        except Exception:
            pass
    pool.unlock(lf)
    if rc == 0 and final and os.path.exists(final):
        dest = os.path.join(OUTAPI, jid + ".mp4")
        try:
            shutil.copy2(final, dest)
            size = os.path.getsize(dest)
            db.finish_job(jid, "succeeded", result_path=dest, account_uid=acct["uid"], tag=tag, cost=COST)
            db.bump_usage(job["key_id"], COST)
            log(wid, "done", jid, "%.0fs" % dt, "%.1fMB" % (size / 1048576.0))
        except Exception as e:
            db.finish_job(jid, "failed", error="copy failed: %r" % e, account_uid=acct["uid"], tag=tag)
    elif rc == 2:
        pool.mark_exhausted(acct["uid"])
        db.add_tried(jid, acct["uid"])
        if job["attempts"] >= 3:
            db.finish_job(jid, "failed", error="no credit after 3 accounts", account_uid=acct["uid"], tag=tag)
        else:
            db.requeue_job(jid, "account without credit")
        log(wid, "no credit", acct["uid"], "-> requeue", jid)
    elif rc == 7 and job["attempts"] < 3:
        db.add_tried(jid, acct["uid"])
        db.requeue_job(jid, "render_fail, retry on another account")
        log(wid, "render_fail -> retry", jid, "attempt", job["attempts"])
    else:
        # Browser died after submit: the render usually still completes server-side.
        # Try to pull the finished asset over plain HTTP before giving up.
        m = re.search(r"thread=([0-9a-fA-F-]{36})", txt or "")
        tid = m.group(1) if m else ""
        rec_dest = os.path.join(OUTAPI, jid + ".mp4")
        ok = False
        if tid:
            log(wid, "browser died, recover asset", jid, tid[:8])
            ok, _pm, _pw = recover.recover(tid, acct["path"], rec_dest, PORTS,
                                           wait_s=420, log=lambda *a: log(wid, *a),
                                           expected=float(job["duration"]))
        if ok:
            size = os.path.getsize(rec_dest)
            db.finish_job(jid, "succeeded", result_path=rec_dest, account_uid=acct["uid"], tag=tag, cost=COST)
            db.bump_usage(job["key_id"], COST)
            log(wid, "recovered", jid, "%.0fs" % dt, "%.1fMB" % (size / 1048576.0))
        elif job["attempts"] < 3:
            db.add_tried(jid, acct["uid"])
            db.requeue_job(jid, "browser died, asset not ready")
            log(wid, "recover-miss -> requeue", jid, "attempt", job["attempts"])
        else:
            tail = (txt or "").strip().splitlines()[-3:]
            db.finish_job(jid, "failed", error=("rc=%s %s" % (rc, " | ".join(tail)))[-500:],
                          account_uid=acct["uid"], tag=tag)
            log(wid, "fail", jid, "rc", rc, "%.0fs" % dt)
    # refresh credit cache after a run (best effort)
    time.sleep(1)

def worker(wid, port):
    log(wid, "up port", port)
    while True:
        try:
            if free_gb() < MIN_FREE_GB:
                time.sleep(60); continue
            jid = db.claim_job(wid)
            if not jid:
                time.sleep(5); continue
            run_job(wid, jid, port)
        except Exception as e:
            log(wid, "loop err", repr(e)[:150]); time.sleep(10)

_started = False
def start():
    global _started
    if _started: return
    _started = True
    for i in range(WORKERS):
        port = PORTS[i % len(PORTS)]
        t = threading.Thread(target=worker, args=("w%d" % (i + 1), port), daemon=True)
        t.start()
        time.sleep(3)

if __name__ == "__main__":
    db.init()
    start()
    while True: time.sleep(3600)
