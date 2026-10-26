# -*- coding: utf-8 -*-
"""_pp_http.py - Pippit HTTP clients: sign API, credit, wm flag, thread->asset->clean url, download/probe.
Rules: API needs sign md5('9e2c|'+last7+...); download CDN needs proxy (direct 403).
"""
import hashlib, io, json, os, re, subprocess, sys, time, warnings
warnings.filterwarnings("ignore")
import requests

import shutil as _shutil
C = os.environ.get("PP_HOME") or os.path.dirname(os.path.abspath(__file__))
DL = os.path.join(C, "outputs", "R329_pippit")
PY = os.environ.get("PP_PY") or sys.executable
FFPROBE = (os.environ.get("PP_FFPROBE") or _shutil.which("ffprobe")
           or r"<HOME>\AppData\Local\Microsoft\WinGet\Links\ffprobe.exe")
PORTS = [int(x) for x in os.environ.get("PP_PORTS", "21030,21999,21107,21104,21100,21106,21101,21105,21103").split(",")]
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
PAPI = "https://www.pippit.ai"

def sign(path):
    ts = int(time.time() * 1000)
    return hashlib.md5(("9e2c|%s|7|5.8.0|%d||11ac" % (path.split("?")[0][-7:], ts)).encode()).hexdigest(), ts

def _proxies(port):
    if not port:
        return {"http": None, "https": None}
    u = "http://127.0.0.1:%d" % port
    return {"http": u, "https": u}

class PP:
    def __init__(self, cookies, ports=None):
        self.ck = dict(cookies or {})
        self.ports = ports or PORTS
        self.s = requests.Session()
        self.s.cookies.update(self.ck)
        self.last_port = self.ports[0]

    def _hdr(self, path):
        s, ts = sign(path)
        return {"User-Agent": UA, "Accept": "application/json, text/plain, */*",
                "Content-Type": "application/json", "sign": s, "device-time": str(ts),
                "sign-ver": "1", "pf": "7", "appvr": "5.8.0", "Referer": PAPI + "/home",
                "Origin": PAPI, "Appid": "573081", "X-SUBJECT-APPID": "578071"}

    def req(self, path, body=None, method="POST", tries=3, timeout=40):
        last = None
        for t in range(tries):
            for p in self.ports:
                try:
                    r = self.s.request(method, PAPI + path, data=None if body is None else json.dumps(body),
                                       headers=self._hdr(path), timeout=timeout, verify=False,
                                       proxies=_proxies(p), allow_redirects=True)
                    if r.status_code == 200:
                        self.last_port = p
                        return r
                    last = "HTTP%d" % r.status_code
                except Exception as e:
                    last = repr(e)[:60]
        raise RuntimeError("req %s failed: %s" % (path, last))

    # ---- business helpers ----
    def credit(self):
        j = self.req("/commerce/v1/benefits/user_credit", {}).json()
        resp = j.get("response")
        if isinstance(resp, str):
            resp = json.loads(resp or "{}")
        c = (resp or {}).get("credit") or {}
        return {k: int(c.get(k) or 0) for k in ("vip_credit", "gift_credit", "purchase_credit")} | {
            "total": sum(int(c.get(k) or 0) for k in ("vip_credit", "gift_credit", "purchase_credit"))}

    def set_wmban(self, on=True):
        j = self.req("/api/biz/v1/user/update_user_setting",
                     {"user_settings": {"aigc_watermark_ban_enabled": bool(on)}}).json()
        return j

    def thread_assets(self, tid):
        j = self.req("/api/biz/v1/agent/get_thread",
                     {"scopes": ["run_list.entry_list"], "thread_id": tid}, timeout=45).json()
        ids = []
        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if k == "pippit_asset_id" and v:
                        ids.append(str(v))
                    else:
                        walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        walk(j)
        return ids, j

    def asset(self, aid):
        j = self.req("/api/biz/v1/asset/query",
                     {"Base": {"Client": "web"}, "pippit_asset_ids": [aid]}, timeout=45).json()
        return ((j.get("data") or {}).get("Assets") or []), j

    def clean_url(self, aid):
        assets, _ = self.asset(aid)
        if not assets:
            return None, None
        a = assets[0]
        v = a.get("Video") or {}
        nw = v.get("no_watermark_video_url") or {}
        return (nw.get("download_url") or v.get("origin_url") or ""), a

    def list_projects(self, count=10):
        j = self.req("/api/biz/v1/agent/list_creative_project",
                     {"count": int(count), "cursor": 0}, timeout=45).json()
        return ((j.get("data") or {}).get("project_list") or [])

    def project_files(self, pid, page_size=300):
        j = self.req("/api/biz/v1/agent/list_project_file",
                     {"Base": {"Client": "web"}, "PageNum": 1, "PageSize": int(page_size),
                      "project_id": int(pid)}, timeout=45).json()
        return ((j.get("data") or {}).get("Files") or [])

    def recent_assets(self, since_ms, max_projects=5):
        """Recover video assets when the agent lost / never wrote its thread id.
        Project-file metadata contains the same PippitAssetID used by asset/query,
        so the recovered id can still resolve to a no-watermark download URL."""
        out = []
        projects = sorted(self.list_projects(10),
                          key=lambda x: int(x.get("create_time") or 0), reverse=True)
        for pr in projects[:int(max_projects)]:
            ct = int(pr.get("create_time") or 0)
            if ct and ct < int(since_ms) - 120000:
                continue
            pid = pr.get("project_id")
            if not pid:
                continue
            try:
                files = self.project_files(pid)
            except Exception:
                continue
            for f in files:
                a = ((f.get("Raw") or {}).get("AgentAsset") or {})
                try:
                    if int(a.get("AssetType") or 0) != 2:
                        continue
                except Exception:
                    continue
                v = a.get("Video") or {}
                url = v.get("download_url") or v.get("origin_url") or ""
                aid = str(a.get("PippitAssetID") or "")
                if not aid or not url:
                    continue
                out.append({"asset_id": aid, "project_id": pid,
                            "create_time": int(a.get("CreateTime") or 0),
                            "name": a.get("Name") or f.get("Filename") or "",
                            "url": url})
        out.sort(key=lambda x: x.get("create_time") or 0, reverse=True)
        return out

    def download(self, url, dest, min_bytes=200000, tries=3, timeout=(20, 90)):
        """Resumable download: keeps .part and retries each port with HTTP Range."""
        tmp = dest + ".part"
        last = None
        for t in range(tries):
            for p in self.ports:
                start = os.path.getsize(tmp) if os.path.exists(tmp) else 0
                hdr = {"Range": "bytes=%d-" % start} if start else {}
                try:
                    with self.s.get(url, timeout=timeout, verify=False, stream=True,
                                    proxies=_proxies(p), allow_redirects=True, headers=hdr) as r:
                        if r.status_code == 206 and start > 0:
                            mode = "ab"; total = start
                        elif r.status_code == 200:
                            mode = "wb"; total = 0
                        else:
                            last = "GET%d" % r.status_code
                            continue
                        with io.open(tmp, mode) as f:
                            for ch in r.iter_content(1 << 20):
                                if ch:
                                    f.write(ch); total += len(ch)
                        if total < min_bytes:
                            last = "small%d" % total
                            continue
                        os.replace(tmp, dest)
                        self.last_port = p
                        return total
                except Exception as e:
                    last = repr(e)[:70]
            time.sleep(2)
        try:
            os.remove(tmp)
        except Exception:
            pass
        raise RuntimeError("download failed: %s" % last)

    def download_fast(self, url, dest, chunk=4 << 20, workers=12, retries=3):
        """Parallel ranged download (CDN 206). Falls back to sequential resume if any chunk fails."""
        import threading
        tmp = dest + ".part"
        total = 0
        for p in self.ports:
            try:
                r = self.s.get(url, headers={"Range": "bytes=0-0"}, timeout=(15, 30), verify=False,
                               proxies=_proxies(p), allow_redirects=True)
                cr = r.headers.get("Content-Range") or ""
                m = re.search(r"/(\d+)", cr)
                if r.status_code in (200, 206) and m:
                    total = int(m.group(1)); self.last_port = p; break
            except Exception:
                continue
        if not total:
            return self.download(url, dest)
        ranges = [(o, min(o + chunk, total) - 1) for o in range(0, total, chunk)]
        with io.open(tmp, "wb") as f:
            f.truncate(total)
        ok = [False] * len(ranges)

        def fetch(i, a, b):
            for attempt in range(retries):
                p = self.ports[(i + attempt) % len(self.ports)]
                try:
                    r = self.s.get(url, headers={"Range": "bytes=%d-%d" % (a, b)}, timeout=(15, 60),
                                   verify=False, proxies=_proxies(p), allow_redirects=True)
                    if r.status_code not in (200, 206):
                        continue
                    body = r.content
                    if len(body) != (b - a + 1):
                        continue
                    with io.open(tmp, "r+b") as f:
                        f.seek(a); f.write(body)
                    ok[i] = True
                    return
                except Exception:
                    continue

        for rnd in range(2):
            todo = [i for i in range(len(ranges)) if not ok[i]]
            if not todo: break
            idx = [0]; lk = threading.Lock()
            def worker():
                while True:
                    with lk:
                        if idx[0] >= len(todo): return
                        i = todo[idx[0]]; idx[0] += 1
                    a, b = ranges[i]
                    fetch(i, a, b)
            ts = [threading.Thread(target=worker) for _ in range(min(workers, len(todo)))]
            [t.start() for t in ts]; [t.join() for t in ts]
        if not all(ok):
            try: os.remove(tmp)
            except Exception: pass
            return self.download(url, dest)
        if os.path.getsize(tmp) != total:
            raise RuntimeError("fast download size mismatch %d != %d" % (os.path.getsize(tmp), total))
        os.replace(tmp, dest)
        return total

def probe(fp):
    pr = subprocess.run([FFPROBE, "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format", fp],
                        capture_output=True, text=True)
    d = json.loads(pr.stdout)
    vs = [x for x in d["streams"] if x["codec_type"] == "video"][0]
    num, den = (vs.get("avg_frame_rate") or "0/1").split("/")
    fps = float(num) / float(den or 1)
    return {"w": int(vs.get("width") or 0), "h": int(vs.get("height") or 0),
            "fps": round(fps, 3), "frames": int(vs.get("nb_frames") or 0),
            "dur": round(float(d.get("format", {}).get("duration") or vs.get("duration") or 0), 6),
            "size": int(d.get("format", {}).get("size") or os.path.getsize(fp))}

def wm_check(fp):
    _wm = os.path.join(C, "work", "_wm_check.py")
    if not os.path.exists(_wm):
        return {"verdict": "UNCERTAIN", "error": "wm_check.py not found", "max": -1}
    r = subprocess.run([PY, _wm, fp], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    try:
        return json.loads((r.stdout or "").strip().splitlines()[-1])
    except Exception:
        return {"verdict": "UNCERTAIN", "error": (r.stdout or "")[-200:], "max": -1}

def gate(meta, wm, expect_w=1080, expect_h=1920, expect_secs=30.0, tol_frames=1):
    errs = []
    if meta["w"] != expect_w or meta["h"] != expect_h:
        errs.append("size %dx%d != %dx%d" % (meta["w"], meta["h"], expect_w, expect_h))
    if abs(meta["frames"] - int(round(expect_secs * 30))) > tol_frames:
        errs.append("frames %d" % meta["frames"])
    if abs(meta["dur"] - expect_secs) > 0.08:
        errs.append("dur %.3f" % meta["dur"])
    if wm.get("verdict") != "CLEAN":
        errs.append("wm %s max=%s" % (wm.get("verdict"), wm.get("max")))
    return (len(errs) == 0), errs
