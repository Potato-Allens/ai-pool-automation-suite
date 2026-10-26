# -*- coding: utf-8 -*-
"""Late-asset recovery: a render that already left the browser can still be fetched
over plain HTTP with the account cookies (thread_assets -> clean_url -> download)."""
import io, json, os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
C = r"<PROJECT_ROOT>"
sys.path.insert(0, os.path.join(C, "work")); sys.path.insert(0, HERE)
import _pp_http as H


def gate(path, expected=30.0):
    try:
        m = H.probe(path); w = H.wm_check(path)
    except Exception as e:
        return False, {"err": repr(e)[:120]}, {}
    want_frames = int(round(float(expected) * 30.0))
    got_dur = float(m.get("dur") or 0)
    got_frames = int(m.get("frames") or 0)
    # platform quantises to 5s steps (6s -> 5.015s/150f); allow one-frame-level drift
    ok = (m.get("w") == 1080 and m.get("h") == 1920
          and abs(got_dur - float(expected)) <= 0.35
          and abs(got_frames - want_frames) <= 12
          and (w or {}).get("verdict") == "CLEAN")
    return ok, m, w


def recover(tid, acct_path, dest, ports, wait_s=420, log=print, expected=30.0):
    if not tid:
        return False, None, None
    try:
        d = json.load(io.open(acct_path, encoding="utf-8-sig"))
    except Exception as e:
        log("recover: acct load err", repr(e)[:100]); return False, None, None
    pp = H.PP(d.get("cookies") or {}, ports=ports)
    deadline = time.time() + wait_s
    last = ""
    while True:
        try:
            assets, _j = pp.thread_assets(tid)
        except Exception as e:
            assets = []; last = repr(e)[:80]
        if assets:
            aid = assets[-1]
            url = None
            for _ in range(3):
                try:
                    url, _m = pp.clean_url(aid)
                except Exception:
                    url = None
                if url:
                    break
                time.sleep(8)
            if url:
                try:
                    n = pp.download_fast(url, dest)
                except Exception as e:
                    log("recover: dl err", repr(e)[:100]); n = 0
                if n:
                    ok, m, w = gate(dest, expected)
                    log("recover: %s aid=%s %.1fMB gate=%s wm=%s" %
                        (tid[:8], aid, n / 1048576.0, ok, (w or {}).get("verdict")))
                    return ok, m, w
        if time.time() > deadline:
            log("recover: timeout %s %s" % (tid[:8], last))
            return False, None, None
        time.sleep(30)
