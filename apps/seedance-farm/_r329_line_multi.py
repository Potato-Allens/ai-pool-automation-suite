# -*- coding: utf-8 -*-
"""_r329_line_multi.py <tagbase> <target> <nodes-csv> [--cooldown 600] [--res 1080p] [--ratio 9:16] [--secs 30] [--prompts a,b]
One worker per exit node (distinct IP); per-node cooldown avoids the generation wall.
"""
import argparse, glob, io, json, os, shutil, subprocess, sys, threading, time
import psutil
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _pp_http as H

OUT = os.path.join(H.C, "outputs", "evidence_r323")
LOCKS = os.path.join(H.C, "work", "line_locks"); os.makedirs(LOCKS, exist_ok=True)
# --- global browser/agent cap: cross-process slot files (max 8 concurrent prod/agent) ---
SLOTS = os.path.join(H.C, "work", "agent_slots"); os.makedirs(SLOTS, exist_ok=True)
MAX_SLOTS = int(os.environ.get("PP_MAX_SLOTS", "8"))

def _slot_owner_alive(fp):
    try:
        with io.open(fp, "r", encoding="utf-8", errors="replace") as f:
            pid = int((f.read() or "0").strip())
        if pid <= 0: return False
        return _pid_alive(pid)
    except Exception:
        return False

def _slot_stale(fp):
    """Leaked-slot reclaim: owner alive but no prod/agent child for 10min, or older than 60min."""
    try:
        age = time.time() - os.path.getmtime(fp)
        pid = int((io.open(fp, encoding="utf-8", errors="replace").read() or "0").strip())
    except Exception:
        return True
    if age > 3600:
        return True
    if age > 600 and pid > 0:
        try:
            kids = psutil.Process(pid).children(recursive=True)
            for k in kids:
                cl = " ".join(k.cmdline() or [])
                if "_r329_prod" in cl or "_r329_agent" in cl:
                    return False
            return True
        except Exception:
            return True
    return False

def acquire_slot(timeout=1800):
    t0 = time.time()
    while True:
        for i in range(MAX_SLOTS):
            fp = os.path.join(SLOTS, "slot_%d.lock" % i)
            try:
                fd = os.open(fp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode("ascii")); os.close(fd)
                return fp
            except FileExistsError:
                if (not _slot_owner_alive(fp)) or _slot_stale(fp):
                    try: os.remove(fp)
                    except Exception: pass
            except Exception:
                time.sleep(5)
        if time.time() - t0 > timeout:
            return None
        time.sleep(10)

def release_slot(fp):
    if fp:
        try: os.remove(fp)
        except Exception: pass

LOG = os.path.join(OUT, "multi_%s.log" % time.strftime("%H%M%S"))
RUN_ID = time.strftime("%H%M%S")
ONLY_FILE = ""
lock = threading.Lock()

def disk_free_gb():
    try:
        return shutil.disk_usage(H.C).free / (1024.0 ** 3)
    except Exception:
        return 0.0
prod_lock = threading.Lock()

def log(*a):
    m = " ".join(str(x) for x in a)
    with lock:
        print(time.strftime("[%H:%M:%S] ") + m, flush=True)
        try:
            with io.open(LOG, "a", encoding="utf-8") as f: f.write(time.strftime("[%H:%M:%S] ") + m + "\n")
        except Exception:
            pass

def load_accounts():
    seen, out = {}, []
    if ONLY_FILE:
        try:
            items = json.load(io.open(ONLY_FILE, encoding="utf-8-sig"))
        except Exception as ex:
            log("accounts-file read err", repr(ex)[:100]); return []
        for it in items:
            fp = it if isinstance(it, str) else (it.get("file") or it.get("path") or "")
            if not fp or not os.path.exists(fp):
                continue
            try:
                e = json.load(io.open(fp, encoding="utf-8-sig"))
            except Exception:
                continue
            uid = str(e.get("uid") or "")
            if not uid or uid in seen or not e.get("cookies"):
                continue
            seen[uid] = 1
            out.append({"uid": uid, "tag": e.get("tag") or os.path.basename(fp)[7:-5], "cookies": e["cookies"], "path": fp})
        return out
    pool = os.path.join(OUT, "pp_farm_pool.json")
    try:
        for e in json.load(io.open(pool, encoding="utf-8-sig")):
            if e.get("ok") and e.get("cookies"):
                uid = str(e.get("uid") or "")
                if uid and uid not in seen:
                    seen[uid] = 1; out.append({"uid": uid, "tag": e.get("tag"), "cookies": e["cookies"], "path": None})
    except Exception as ex:
        log("pool read err", repr(ex)[:80])
    for fp in sorted(glob.glob(os.path.join(OUT, "pp_acc_*.json"))):
        try:
            e = json.load(io.open(fp, encoding="utf-8-sig")); uid = str(e.get("uid") or "")
            if uid and uid not in seen and e.get("cookies"):
                seen[uid] = 1; out.append({"uid": uid, "tag": e.get("tag") or os.path.basename(fp)[7:-5], "cookies": e["cookies"], "path": fp})
        except Exception: pass
    try:
        ex = set(json.load(io.open(os.path.join(H.C, "work", "pp_exhausted.json"), encoding="utf-8")))
    except Exception:
        ex = set()
    return [a for a in out if a["uid"] not in ex]

def acct_file(a):
    if a.get("path") and os.path.exists(a["path"]): return a["path"]
    fp = os.path.join(OUT, "pp_acc_multi_%s.json" % a["uid"])
    io.open(fp, "w", encoding="utf-8").write(json.dumps({"uid": a["uid"], "tag": a.get("tag"), "cookies": a["cookies"]}, ensure_ascii=False, indent=1))
    a["path"] = fp
    return fp

def _pid_alive(pid):
    """Windows-safe liveness check (os.kill(pid,0) would TerminateProcess on Windows)."""
    try:
        pid = int(pid)
    except Exception:
        return False
    if pid <= 0:
        return False
    try:
        import psutil
        return bool(psutil.pid_exists(pid))
    except Exception:
        pass
    try:
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
                             capture_output=True, text=True, timeout=15)
        return str(pid) in (out.stdout or "")
    except Exception:
        return False

def _account_busy(acc):
    """If a live prod/agent still references this account file, do not touch it.
    This covers orphan children left behind when their line process dies."""
    uid = acc["uid"]
    needles = ["pp_acc_multi_%s.json" % uid]
    p = acc.get("path")
    if p:
        needles.append(os.path.basename(p))
    try:
        import psutil
        for pr in psutil.process_iter(["pid", "cmdline"]):
            if pr.pid == os.getpid():
                continue
            cl = " ".join(pr.info["cmdline"] or [])
            if "_r329_prod.py" not in cl and "_r329_agent.py" not in cl:
                continue
            for n in needles:
                if n in cl:
                    return pr.pid
    except Exception:
        pass
    return 0

def _try_create(fp):
    try:
        fd = os.open(fp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode("ascii", "ignore"))
        os.close(fd)
        return True
    except FileExistsError:
        return False
    except Exception:
        return False

def _lock_owner(fp):
    try:
        return int(io.open(fp, encoding="utf-8", errors="replace").read().strip() or 0)
    except Exception:
        return 0


def claim_blocked():
    """True when the last claim-retry pass got only -4 (global claim freeze)."""
    try:
        fp = os.path.join(H.C, "work", "claim_block.flag")
        return os.path.exists(fp) and (time.time() - os.path.getmtime(fp)) < 2700
    except Exception:
        return False

def try_lock(uid, acc=None):
    if acc is not None:
        busy = _account_busy(acc)
        if busy:
            log("uid %s busy by pid=%d, skip" % (uid, busy))
            return None
    fp = os.path.join(LOCKS, uid + ".lock")
    # atomic acquire (two lines may race for the same uid)
    if _try_create(fp):
        return fp
    pid = _lock_owner(fp)
    age = time.time() - os.path.getmtime(fp)
    if _pid_alive(pid) or (pid == 0 and age < 3 * 3600):
        return None
    try:
        os.remove(fp)
    except Exception:
        return None
    return fp if _try_create(fp) else None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tag"); ap.add_argument("target", type=int); ap.add_argument("nodes")
    ap.add_argument("--cooldown", type=float, default=600); ap.add_argument("--res", default="1080p")
    ap.add_argument("--ratio", default="9:16"); ap.add_argument("--secs", default="30")
    ap.add_argument("--prompts", default="")
    ap.add_argument("--min-credit", type=int, default=0, dest="min_credit_arg"); ap.add_argument("--grace", type=float, default=1800, help="新号额度到账宽限期(秒)")
    ap.add_argument("--accounts-file", default="", help="optional accounts json list")
    a = ap.parse_args()
    global ONLY_FILE
    ONLY_FILE = a.accounts_file
    min_credit = int(a.min_credit_arg) if int(a.min_credit_arg or 0) > 0 else max(120, int(float(a.secs) * 4) + 80)
    nodes = [int(x) for x in a.nodes.split(",") if x]
    _new = [os.path.join(H.C, "work", "prompt_prod_coffee6_30.txt"), os.path.join(H.C, "work", "prompt_prod_fox6_30.txt")]
    _new = [x for x in _new if os.path.exists(x)]
    _pref = _new if len(_new) >= 2 else [os.path.join(H.C, "work", "prompt_vert_coffee30.txt"), os.path.join(H.C, "work", "prompt_vert_fox30.txt")]
    _pref = [x for x in _pref if os.path.exists(x)]
    files = [x for x in a.prompts.split(",") if x] or (_pref if len(_pref) >= 2 else sorted(glob.glob(os.path.join(H.C, "work", "prompt_vert_*30.txt"))))
    prompts = []
    for f in files:
        try: prompts.append((os.path.basename(f), io.open(f, encoding="utf-8").read().strip()))
        except Exception: pass
    if not prompts:
        # R-WZ-85: an empty prompt list used to blow up as
        # ZeroDivisionError("integer modulo by zero") at `n % len(prompts)`
        # and the line would "skip account" forever while producing nothing.
        log("!! no prompt files found -> using built-in 30s fallback prompt")
        prompts = [("builtin_fallback_30",
                    "Vertical 9:16 30-second photorealistic film. Six 5-second shots with slow "
                    "camera moves, natural light, no text, no captions, no logos.")]
    _soft_pref = [p for p in prompts if os.path.basename(p[0]) in ("prompt_prod_coffee6_30.txt", "prompt_prod_fox6_30.txt")]
    soft_prompts = _soft_pref or [p for p in prompts if "three connected" in p[1].lower() or "3_" in p[0] or "_3" in p[0]] or prompts
    if not soft_prompts:
        soft_prompts = prompts
    accounts = load_accounts(); q = list(accounts)
    produced = [0]; idx = [0]; done = set(); fails = {}
    last_start = {n: 0.0 for n in nodes}
    last_refill = [0.0]
    log("=== multi %s run=%s target=%d nodes=%s accounts=%d prompts=%d cooldown=%.0fs min_credit=%d" %
        (a.tag, RUN_ID, a.target, nodes, len(accounts), len(prompts), a.cooldown, min_credit))

    def wait_node(node):
        with lock:
            wait = a.cooldown - (time.time() - last_start[node])
        if wait > 0:
            log("node %d cooldown %.0fs" % (node, wait)); time.sleep(wait)
        with lock: last_start[node] = time.time()

    def refill():
        fresh = [x for x in load_accounts() if x["uid"] not in done]
        with lock:
            have = {x["uid"] for x in q}
            added = 0
            for x in fresh:
                if x["uid"] not in have:
                    q.append(x); have.add(x["uid"]); added += 1
        return added

    def worker(node):
        while True:
            try:
                if time.time() - last_refill[0] > 600:
                    last_refill[0] = time.time()
                    _n = refill()
                    if _n: log("node %d periodic refill +%d" % (node, _n))
            except Exception as _e:
                log("node %d refill err %r" % (node, _e))
            free = disk_free_gb()
            if free < 8.0:
                log("node %d disk low %.1fGB -> wait 120s" % (node, free))
                time.sleep(120); continue
            with lock:
                if produced[0] >= a.target: return
                acc = q.pop(0) if q else None
            if acc is None:
                time.sleep(15)
                try: n = refill()
                except Exception as e: n = -1
                log("node %d queue empty -> refill %s" % (node, n))
                time.sleep(15)
                continue
            uid = acc["uid"]
            if uid in done: continue
            fp = try_lock(uid, acc)
            if not fp: continue
            try:
                requeue = False
                slot = None
                pp = H.PP(acc["cookies"], [node] if not node else [node] + [p for p in H.PORTS if p != node])
                try: total = pp.credit()["total"]
                except Exception as e: log("node %d %s credit ERR %r" % (node, uid, e)); continue
                log("node %d %s credit=%d" % (node, uid, total))
                if 0 < total < min_credit:
                    log("node %d %s low credit=%d < %d, claim already used -> skip claim2" % (node, uid, total, min_credit))
                if total < min_credit and total == 0 and not claim_blocked():
                    try:
                        clf = os.path.join(H.C, "work", "claim_%s.out" % uid)
                        with io.open(clf, "w", encoding="utf-8", errors="replace") as cf:
                            subprocess.run([H.PY, "-u", os.path.join(H.C, "work", "_r329_claim2.py"), acct_file(acc), str(node),
                                            os.path.join(H.C, "work", "pp_prof_claim2_%s" % uid)],
                                           stdout=cf, stderr=subprocess.STDOUT, timeout=600)
                    except subprocess.TimeoutExpired:
                        log("node %d %s claim2 TIMEOUT 600s -> skip" % (node, uid))
                    time.sleep(2)
                    try: total = pp.credit()["total"]
                    except Exception: total = 0
                    log("node %d %s credit after claim=%d" % (node, uid, total))
                if total < 120:
                    # 新号额度到账有延迟(实测注册时读0, 数分钟后变340): 宽限期内不拉黑, 稍后重试
                    try:
                        fresh = (time.time() - os.path.getmtime(acct_file(acc))) < a.grace
                    except Exception:
                        fresh = False
                    if fresh and total <= 0:
                        log("node %d %s credit not ready (new acct), requeue in 120s" % (node, uid))
                        try:
                            if fp and os.path.exists(fp): os.remove(fp)
                        except Exception:
                            pass
                        time.sleep(120)
                        requeue = True
                        continue
                    log("node %d %s skip (no credit)" % (node, uid))
                    try:
                        exf = os.path.join(H.C, "work", "pp_exhausted.json")
                        cur = set(json.load(io.open(exf, encoding="utf-8")))
                        if uid not in cur:
                            cur.add(uid)
                            io.open(exf, "w", encoding="utf-8").write(json.dumps(sorted(cur), indent=1))
                    except Exception:
                        pass
                    continue
                lowc = total < min_credit
                if lowc:
                    log("node %d %s low-credit lane=%d (>=120), 1 round via --low-credit" % (node, uid, total))
                rounds = 1 if lowc else (2 if total >= min_credit + 120 else 1)
                for r in range(rounds):
                    try: cur_credit = pp.credit()["total"]
                    except Exception: cur_credit = total
                    if cur_credit < (120 if lowc else min_credit):
                        log("node %d %s stop rounds (credit=%d < %d)" % (node, uid, cur_credit, min_credit))
                        break
                    with lock:
                        if produced[0] >= a.target: break
                        n = idx[0]; idx[0] += 1
                    if acc.get("soft_retry"):
                        pf, ptxt = soft_prompts[n % len(soft_prompts)]
                        acc.pop("soft_retry", None)
                        log("node %d %s using simplified prompt=%s" % (node, uid, pf))
                    else:
                        pf, ptxt = prompts[n % len(prompts)]
                    tag = "%s_%d%s_%s" % (a.tag, n, "ab"[r % 2] if rounds > 1 else "", RUN_ID)
                    wait_node(node)
                    slot = acquire_slot()
                    if slot is None:
                        log("node %d %s slot timeout -> requeue" % (node, uid)); requeue = True; break
                    log("node %d %s start prompt=%s slots=%d/%d" % (node, tag, pf, len(os.listdir(SLOTS)), MAX_SLOTS))
                    t0 = time.time()
                    try: pre_credit = pp.credit()["total"]
                    except Exception: pre_credit = cur_credit
                    retried = False
                    try:
                        plf = os.path.join(H.C, "work", "prod_%s.out" % tag)
                        with io.open(plf, "w", encoding="utf-8", errors="replace") as pf:
                            p = subprocess.run([H.PY, "-u", os.path.join(H.C, "work", "_r329_prod.py"), acct_file(acc), tag, ptxt,
                                                "--secs", a.secs, "--res", a.res, "--ratio", a.ratio, "--port", str(node), "--min-credit", str(min_credit)] + (["--low-credit"] if lowc else []),
                                               stdout=pf, stderr=subprocess.STDOUT, timeout=3300)
                        rc = p.returncode
                    except subprocess.TimeoutExpired:
                        rc = -9
                    if rc in (6, -1, 4294967295) and not retried:
                        retried = True
                        log("node %d %s rc=%s -> wait cooldown then retry" % (node, tag, rc))
                        wait_node(node)
                        try: c2 = pp.credit()["total"]
                        except Exception: c2 = 0
                        if c2 < 120:
                            log("node %d %s retry skipped (credit=%d)" % (node, tag, c2))
                            rc = 2
                        else:
                            try:
                                p2 = subprocess.run([H.PY, "-u", os.path.join(H.C, "work", "_r329_prod.py"), acct_file(acc), tag, ptxt,
                                                     "--secs", a.secs, "--res", a.res, "--ratio", a.ratio, "--port", str(node), "--min-credit", str(min_credit)] + (["--low-credit"] if lowc else []),
                                                    capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=2400)
                                rc = p2.returncode
                            except subprocess.TimeoutExpired:
                                rc = -9
                    if rc in (4, 5, 7, 1, 3):
                        try: c3 = pp.credit()["total"]
                        except Exception: c3 = 0
                        fails[uid] = fails.get(uid, 0) + 1
                        _soft = rc in (4, 5, 7)
                        _trycap = 2 if _soft else 1
                        if c3 >= min_credit and fails[uid] <= _trycap and (_soft or c3 == pre_credit):
                            requeue = True
                            acc["soft_retry"] = True
                            log("node %d %s rc=%s -> requeue (credit=%d pre=%d try=%d)" % (node, tag, rc, c3, pre_credit, fails[uid]))
                        else:
                            log("node %d %s rc=%s -> stop (credit=%d < %d or tries=%d)" % (node, tag, rc, c3, min_credit, fails[uid]))
                    log("node %d %s done rc=%s %.0fs" % (node, tag, rc, time.time() - t0))
                    if rc == 0:
                        with lock: produced[0] += 1
                        log("=== progress %d/%d" % (produced[0], a.target))
                    elif rc == 2:
                        break
            except Exception as e:
                requeue = False
                log("node %d %s worker exc %r -> skip account" % (node, uid, e))
                time.sleep(2)
            finally:
                release_slot(slot)
                try: os.remove(fp)
                except Exception: pass
                with lock:
                    if requeue:
                        q.append(acc)
                    else:
                        done.add(uid)

    ts = [threading.Thread(target=worker, args=(n,)) for n in nodes]
    [t.start() for t in ts]; [t.join() for t in ts]
    log("=== multi %s END produced=%d/%d" % (a.tag, produced[0], a.target))

main()
