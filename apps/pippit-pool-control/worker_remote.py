# -*- coding: utf-8 -*-
"""Remote worker: pull a job from the pool API, render it here (Chrome + proxies +
accounts live on this box), then upload the finished mp4 back.

  python worker_remote.py --base http://SERVER:8787 --token <API_WORKER_TOKEN> --wid win-1
  python worker_remote.py --dry        # protocol smoke test: claim then fail
"""
import argparse, io, json, os, re, subprocess, sys, time, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
C = r"<PROJECT_ROOT>"
sys.path.insert(0, os.path.join(C, "work")); sys.path.insert(0, HERE)
import pool  # noqa: E402

PY = r"python"
FINAL = os.path.join(C, "outputs", "R329_pippit", "final")
PORT = int(os.environ.get("WORKER_PORT", "22015"))


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


def call(base, path, token, body=None, timeout=60, method=None):
    h = {"X-Worker-Token": token, "Content-Type": "application/json"}
    data = json.dumps(body if body is not None else {}).encode()
    r = urllib.request.Request(base.rstrip("/") + path, data=data, headers=h, method=method or "POST")
    with urllib.request.urlopen(r, timeout=timeout) as x:
        return json.loads(x.read())


def beat(base, token, wid):
    """Keep-alive so the console can show this render box as online."""
    try:
        info = {"pid": os.getpid(), "host": os.environ.get("COMPUTERNAME", "")}
        try:                      # cheap file-based stats, no network
            info.update(pool.stats())
        except Exception:
            pass
        call(base, "/v1/worker/heartbeat", token, {"wid": wid, "info": info}, timeout=15)
    except Exception:
        pass


def upload(base, jid, token, path_file, uid):
    import requests
    with open(path_file, "rb") as f:
        r = requests.post(base.rstrip("/") + "/v1/worker/%s/result" % jid,
                          headers={"X-Worker-Token": token},
                          files={"file": (jid + ".mp4", f, "video/mp4")},
                          data={"account_uid": uid}, timeout=900)
    r.raise_for_status()
    return r.json()


_BURNED = set()   # uids that returned rc=2 (not enough platform credits)
MIN_CREDIT = int(os.environ.get("WORKER_MIN_CREDIT", "200"))


def pick_affordable(exclude=None, tries=8):
    """Pick an account and verify it can actually pay for a 30s 1080p run.

    capacity_pool.json is a snapshot and goes stale as the production line drains
    accounts, so check the live credit before spawning a 30s prod run.
    """
    exclude = set(exclude or set())
    for _ in range(tries):
        acct, lf = pool.pick(exclude | _BURNED)
        if not acct:
            return None, None
        cr = pool.credit(acct, PORT)
        if cr is None or cr >= MIN_CREDIT:
            return acct, lf
        log("skip low credit", acct["uid"], cr, "<", MIN_CREDIT)
        _BURNED.add(acct["uid"])
        pool.unlock(lf)
        exclude.add(acct["uid"])
    return None, None


def run_job(job, wid):
    jid = job["id"]
    seg = jid.replace("vid_", "")[:8]
    tag = "REMOTE_%s" % seg
    excluded = set()
    acct, lf = pick_affordable(excluded)
    if not acct:
        return False, "NO_CAPACITY"
    out = os.path.join(HERE, "logs", "remote_%s.out" % tag)
    os.makedirs(os.path.dirname(out), exist_ok=True)
    cmd = [PY, "-u", os.path.join(C, "work", "_r329_prod.py"), acct["path"], tag, job["prompt"],
           "--secs", "%g" % job["duration"], "--res", job["resolution"], "--ratio", job["ratio"],
           "--port", str(PORT)]
    log("start", jid, "uid", acct["uid"], "port", PORT)
    rc = -1
    t0 = time.time()
    try:
        with io.open(out, "w", encoding="utf-8", errors="replace") as f:
            # PP_HOME must point at the project root: _pp_http resolves C=PP_HOME
            # or its own directory, and _r329_prod derives W/OUT/DL from it.
            p = subprocess.Popen(cmd, cwd=C, env=dict(os.environ, PP_HOME=C),
                                 stdout=f, stderr=subprocess.STDOUT)
            rc = p.wait(timeout=2700)
    except subprocess.TimeoutExpired:
        subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True)
    txt = io.open(out, encoding="utf-8", errors="replace").read() if os.path.exists(out) else ""
    m = re.findall(r"FINAL\s+(\S+\.mp4)", txt)
    final = m[-1] if m else None
    pool.unlock(lf)
    if rc == 2:
        # rc=2 = this account cannot afford a 30s run (<200 platform credits).
        # Skip it for the rest of this worker's life instead of burning attempts.
        _BURNED.add(acct["uid"])
        log("burned", acct["uid"], "rc=2 (skip in this worker)")
    if rc == 0 and final and os.path.exists(final):
        log("done", jid, "%.0fs" % (time.time() - t0), "uploading")
        return True, (final, acct["uid"])
    return False, "rc=%s %s" % (rc, " | ".join(txt.strip().splitlines()[-2:]))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("PP_REMOTE", "http://127.0.0.1:8787"))
    ap.add_argument("--token", default=os.environ.get("API_WORKER_TOKEN", ""))
    ap.add_argument("--wid", default="win-1")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    if not a.token:
        a.token = io.open(os.path.join(HERE, "admin_token.txt"), encoding="utf-8").read().strip()
    log("worker", a.wid, "->", a.base, "dry" if a.dry else "")
    beat(a.base, a.token, a.wid)
    job = None
    while True:
        try:
            beat(a.base, a.token, a.wid)
            r = call(a.base, "/v1/worker/claim?wid=" + a.wid, a.token)
            job = r.get("job")
            if not job:
                if a.once:
                    return
                time.sleep(5)
                continue
            log("claimed", job["id"], job["duration"], "s")
            if a.dry:
                call(a.base, "/v1/worker/%s/fail" % job["id"], a.token, {"error": "dry-run"})
                log("failed (dry)", job["id"])
                continue
            ok, res = run_job(job, a.wid)
            if res == "NO_CAPACITY":
                # Pool momentarily dry: hand the job back without burning an attempt,
                # then back off so we do not spin on it.
                try:
                    call(a.base, "/v1/worker/%s/release" % job["id"], a.token, {})
                    log("released (no capacity)", job["id"], "-> sleep 120s")
                except Exception as e:
                    log("release err", repr(e)[:120])
                job = None
                time.sleep(120)
                continue
            if ok:
                path_file, uid = res
                log("upload", upload(a.base, job["id"], a.token, path_file, uid))
            else:
                log("fail", str(res)[:200])
                call(a.base, "/v1/worker/%s/fail" % job["id"], a.token, {"error": str(res)[:400]})
            job = None
        except Exception as e:
            log("loop err", repr(e)[:160])
            if job:
                try:
                    call(a.base, "/v1/worker/%s/fail" % job["id"], a.token,
                         {"error": "worker exception: %r" % (e,)})
                    log("fail(exception)", job["id"])
                except Exception:
                    pass
                job = None
            time.sleep(10)


if __name__ == "__main__":
    main()
