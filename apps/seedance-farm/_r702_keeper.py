# -*- coding: utf-8 -*-
"""R702 service keeper (runs once per scheduled tick): make sure the pool API, the
production line and the housekeeping watchers survive crashes/reboots.

Design notes:
- Only starts something when it is really absent (no duplicate processes).
- The line is only restarted when there is no prod/agent/chrome activity AND native
  has not grown for 20 minutes, so it never fights a healthy run.
- Everything is launched hidden, in the current interactive session (Chrome needs a
  desktop session; SYSTEM/service context cannot render).
"""
import io, json, os, subprocess, sys, time, urllib.request

C = r"<PROJECT_ROOT>"
W = os.path.join(C, "work")
API = os.path.join(W, "apipool")
PY = r"python"
LOG = os.path.join(W, "_r702_keeper.log")
NAT = r"<OUTPUT_DIR>\native"
HIDE = 0x08000000

WATCHERS = [
    ("_r330_harvest", ["-u", os.path.join(W, "_r330_harvest.py"), "--watch", "--interval", "120", "--copy", "--dst", NAT]),
    ("_r600_slot_reaper", ["-u", os.path.join(W, "_r600_slot_reaper.py")]),
    ("_r359_stuck_kill", ["-u", os.path.join(W, "_r359_stuck_kill.py"), "--watch"]),
    ("_r334_rescue", ["-u", os.path.join(W, "_r334_rescue.py"), "--watch", "--interval", "180"]),
    ("_r650_finalize", ["-u", os.path.join(W, "_r650_finalize.py")]),
    ("_r637_stallwatch", ["-u", os.path.join(W, "_r637_stallwatch.py")]),
    ("_r701_line_guard", ["-u", os.path.join(W, "_r701_line_guard.py")]),
]


def log(m):
    line = time.strftime("[%H:%M:%S] ") + m
    print(line, flush=True)
    with io.open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def procs():
    import psutil
    cmds = []
    for p in psutil.process_iter(["name", "cmdline"]):
        try:
            n = (p.info["name"] or "").lower()
            cl = " ".join(p.info["cmdline"] or [])
        except Exception:
            continue
        cmds.append((n, cl))
    return cmds


def public_up(timeout=10):
    try:
        req = urllib.request.Request("https://your-domain.com/pippit/healthz",
                                     headers={"User-Agent": "wz-keeper/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read()).get("ok") is True
    except Exception:
        return False


def api_up(timeout=6):
    try:
        with urllib.request.urlopen("http://127.0.0.1:8787/healthz", timeout=timeout) as r:
            return json.loads(r.read()).get("ok") is True
    except Exception:
        return False


def start(args, cwd=C, out="tmp.out"):
    fp = os.path.join(W, out)
    # PP_HOME MUST point at the project root. _pp_http resolves C = PP_HOME or its
    # own folder; without it the child derives W=<root>\work\work and every
    # _r329_* subprocess path is wrong (rc=3 "no_thread" in ~3s).
    env = dict(os.environ, PP_HOME=C)
    return subprocess.Popen([PY] + args, cwd=cwd, env=env, creationflags=HIDE,
                            stdout=io.open(fp, "a", encoding="utf-8"),
                            stderr=io.open(fp + ".err", "a", encoding="utf-8"))


def port_open(p, host="127.0.0.1", t=1.5):
    import socket
    s = socket.socket(); s.settimeout(t)
    try:
        s.connect((host, p)); return True
    except Exception:
        return False
    finally:
        try: s.close()
        except Exception: pass


def capacity_clips():
    """Clips we can actually afford right now (refreshed by the claim sweep)."""
    try:
        d = json.load(io.open(os.path.join(W, "CAPACITY.json"), encoding="utf-8"))
        if time.time() - float(d.get("ts") or 0) > 7200:
            return None
        return int(d.get("clips") or 0)
    except Exception:
        return None


def native_meta():
    try:
        n = sum(1 for f in os.listdir(NAT) if f.lower().endswith(".mp4"))
        return n, os.path.getmtime(NAT)
    except Exception:
        return -1, 0


def main():
    cmds = procs()
    joined = "\n".join(cl for _n, cl in cmds)
    actions = []

    bad = [p for p in (22015, 22018) if not port_open(p)]
    if bad:
        subprocess.run(["schtasks", "/run", "/tn", "R329_Mihomo"], capture_output=True, creationflags=HIDE)
        time.sleep(6)
        actions.append("proxy task kicked %s -> %s" % (bad, [p for p in bad if port_open(p)] or "still down"))

    if not api_up():
        start(["-u", "run_api.py"], cwd=API, out="_r702_api.out")
        actions.append("api restarted")

    if "worker_remote.py" not in joined:
        try:
            tok = io.open(os.path.join(API, "srv_admin_token.txt"), encoding="utf-8").read().strip()
        except Exception:
            tok = ""
        if tok:
            start(["-u", "worker_remote.py", "--base", "https://your-domain.com/pippit",
                   "--token", tok, "--wid", "win-1"], cwd=API, out="_r702_worker.out")
            actions.append("started worker_remote")

    for name, args in WATCHERS:
        if name not in joined:
            start(args, out="_r702_%s.out" % name.strip("_"))
            actions.append("started " + name)

    has_line = "_r329_line_multi.py" in joined
    has_work = ("_r329_prod.py" in joined) or ("_r329_agent.py" in joined) or ("pp_prof_" in joined)
    n, mt = native_meta()
    age = time.time() - mt if mt else 9e9
    cap = capacity_clips()
    if has_line and not has_work and age > 2700 and (cap is None or cap > 0):
        # Supervisor alive but nothing has been produced for 45 min: it is wedged.
        import psutil
        for pr in psutil.process_iter(["pid", "cmdline"]):
            try:
                cl = " ".join(pr.info["cmdline"] or [])
            except Exception:
                continue
            if "_r329_line_multi.py" in cl:
                try:
                    psutil.Process(pr.info["pid"]).kill()
                    actions.append("killed wedged line pid=%d" % pr.info["pid"])
                except Exception:
                    pass
        time.sleep(3)
        has_line = False

    if not has_line and not has_work and age > 1200 and (cap is None or cap > 0):
        start(["-u", os.path.join(W, "_r329_line_multi.py"), "R571_LOW", "54", "22015,22018",
               "--cooldown", "420", "--secs", "30", "--res", "1080p", "--ratio", "9:16",
               "--accounts-file", os.path.join(W, "capacity_pool.json")], out="_r702_line.out")
        actions.append("line restarted (idle %d min)" % (age // 60))

    if cap == 0:
        actions.append("no capacity (0 clips) -> line left idle")
    pub = public_up()
    if not pub:
        actions.append("PUBLIC API DOWN (https://your-domain.com/pippit)")
    if actions:
        log("; ".join(actions) + " | pub=%s" % ("up" if pub else "down"))
    else:
        log("ok api=up pub=up line=%s native=%d idle=%dm" % (has_line, n, age // 60))


if __name__ == "__main__":
    main()
