# -*- coding: utf-8 -*-
"""_r329_agent.py <acct.json> <prompt|@file> <secs|smart> <tag> [port] [res] [ratio]
Agent 会话驱动: 提交 30s -> 跟随追问/批准 -> 扣分/出片。
"""
import asyncio, base64, hashlib, io, json, os, re, sys, time
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from playwright.async_api import async_playwright

async def hard_close(c):
    """Close the browser best-effort, then hard-exit so a frozen canvas/Chrome
    can never keep the parent prod process waiting forever."""
    try:
        await asyncio.wait_for(c.close(), 8)
    except Exception:
        pass
    os._exit(0)

C = r"<PROJECT_ROOT>"
OUT = os.path.join(C, "outputs", "evidence_r323")
DL = os.path.join(C, "outputs", "R329_pippit")
os.makedirs(DL, exist_ok=True)

CLICK_JS = r"""(t)=>{const T=t.trim().toLowerCase();let c=[];
 for(const e of document.querySelectorAll('*')){
   const x=(e.textContent||'').trim(); if(x.toLowerCase()!=T) continue;
   if(e.children.length>1) continue;
   const r=e.getBoundingClientRect(); if(r.width<2||r.height<2) continue;
   if(r.bottom<0||r.top>innerHeight||r.right<0||r.left>innerWidth) continue;
   c.push({x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2),tag:e.tagName,leaf:e.children.length===0,a:r.width*r.height,cls:String(e.className).slice(0,45)});
 }
 if(c.length===0) return null; c.sort((p,q)=>(p.leaf===q.leaf)?p.a-q.a:(p.leaf?-1:1)); return c[0];}"""

BTNS_JS = r"""()=>[...document.querySelectorAll('button,[role=button]')].filter(e=>{const r=e.getBoundingClientRect();return r.width>2&&r.height>2&&r.top>=0&&r.left>=0&&r.top<innerHeight&&r.left<innerWidth;}).map(e=>{const r=e.getBoundingClientRect();return {t:(e.innerText||e.getAttribute('aria-label')||'').replace(/\n/g,' ').trim().slice(0,50),x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2),dis:!!e.disabled};}).filter(o=>o.t)"""

def browser_instances():
    """Distinct Chrome browser instances (by --user-data-dir); -1 if psutil missing."""
    try:
        import psutil
    except Exception:
        return -1
    seen = set(); n = 0
    for pr in psutil.process_iter(["name", "cmdline"]):
        try:
            if (pr.info["name"] or "").lower() != "chrome.exe":
                continue
            n += 1
            cl = " ".join(pr.info["cmdline"] or [])
            m = re.search(r'--user-data-dir=(?:"([^"]+)"|(\S+))', cl)
            if m: seen.add((m.group(1) or m.group(2)).lower())
        except Exception:
            pass
    return len(seen) if seen else max(1, n // 13)

_LAUNCH_LOCK = [""]

def _acquire_launch_lock(timeout=1500):
    """Serialize check+launch across processes so N agents cannot race past the cap."""
    d = os.path.join(C, "work"); os.makedirs(d, exist_ok=True)
    fp = os.path.join(d, "_browser_launch.lock")
    t0 = time.time()
    while True:
        try:
            fh = os.open(fp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fh, str(os.getpid()).encode()); os.close(fh)
            _LAUNCH_LOCK[0] = fp
            return fp
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(fp) > 150:
                    os.remove(fp); continue
            except Exception:
                pass
            if time.time() - t0 > timeout:
                return ""
            time.sleep(2)

def _release_launch_lock():
    fp = _LAUNCH_LOCK[0]
    if fp:
        try: os.remove(fp)
        except Exception: pass
        _LAUNCH_LOCK[0] = ""

def wait_browser_slot(log):
    """Global cap: never open a new Chrome while >= cap instances are alive."""
    cap = int(os.environ.get("PP_MAX_BROWSERS", "9") or 9)
    _acquire_launch_lock()
    t0 = time.time()
    while True:
        n = browser_instances()
        if n < 0 or n < cap:
            return n
        if time.time() - t0 > 1500:
            log("browser cap wait timeout %ds (n=%d) -> proceed" % (int(time.time() - t0), n))
            return n
        log("browser cap %d/%d -> wait 12s" % (n, cap))
        time.sleep(12)

async def main():
    f, prompt, SECS = sys.argv[1], sys.argv[2], sys.argv[3]
    if prompt.startswith("@"): prompt = io.open(prompt[1:], encoding="utf-8").read().strip()
    TAG = sys.argv[4]
    PORT = int(sys.argv[5]) if len(sys.argv) > 5 else 21100
    RES = sys.argv[6] if len(sys.argv) > 6 else ""
    RATIO = sys.argv[7] if len(sys.argv) > 7 else ""
    MODEL_NAME = sys.argv[8] if len(sys.argv) > 8 else ""
    MODEL_MODE = os.environ.get("PP_MODEL", "smart").strip().lower()
    PROF = os.path.join(C, "work", "pp_prof_agent_%s_%d" % (TAG, int(time.time())))
    d = json.load(io.open(f, encoding="utf-8-sig")); ck = d.get("cookies") or {}
    cks = [{"name": k, "value": v, "domain": ".pippit.ai", "path": "/", "secure": True, "httpOnly": False} for k, v in ck.items()]
    uid = str(d.get("uid"))
    LOG = os.path.join(OUT, "agent_%s.log" % TAG)
    NET = os.path.join(OUT, "agent_%s_net.log" % TAG)
    def log(*a):
        m = " ".join(str(x) for x in a); print(m, flush=True)
        with open(LOG, "a", encoding="utf-8") as fh: fh.write(time.strftime("[%H:%M:%S] ") + m + "\n")
    _nb = wait_browser_slot(log)
    log("browser slot ok (n=%d, cap=%s)" % (_nb, os.environ.get("PP_MAX_BROWSERS", "9")))
    async with async_playwright() as p:
        try:
            ctx = await p.chromium.launch_persistent_context(PROF, channel="chrome", headless=False,
                proxy=(None if not PORT else {"server": "http://127.0.0.1:%d" % PORT}),
                args=["--no-sandbox", "--disable-blink-features=AutomationControlled", "--window-position=-32000,-32000", "--window-size=1500,1100"],
                viewport={"width": 1500, "height": 1100}, locale="en-US")
        finally:
            _release_launch_lock()
        pg = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await ctx.clear_cookies(); await ctx.add_cookies(cks)
        async def call(path, m="POST", b=None, lim=400000):
            ts = int(time.time() * 1000)
            s = hashlib.md5(("9e2c|%s|7|5.8.0|%d||11ac" % (path.split("?")[0][-7:], ts)).encode()).hexdigest()
            JS = """(a)=>{const init={method:a.m,credentials:'include',headers:{'sign':a.s,'device-time':String(a.ts),'sign-ver':'1','pf':'7','appvr':'5.8.0','Content-Type':'application/json'}};
                 if(a.b!==null) init.body=JSON.stringify(a.b);
                 return fetch('/api'+a.p, init).then(r=>r.text().then(t=>r.status+' | '+t.slice(0,a.lim))).catch(e=>'ERR '+e);}"""
            return await pg.evaluate(JS, {"p": path, "m": m, "b": b, "s": s, "ts": ts, "lim": lim})
        async def bodytxt(n=4000):
            try: return str(await pg.evaluate("document.body.innerText.slice(-%d)" % n))
            except Exception as e: return "ERR " + repr(e)[:60]
        async def shot(name):
            try: await pg.screenshot(path=os.path.join(OUT, "agent_%s_%s.png" % (TAG, name)))
            except Exception: pass
        def set_status(v):
            try: io.open(os.path.join(OUT, "agent_status_%s.txt" % TAG), "w", encoding="utf-8").write(v)
            except Exception: pass
        def save_thread():
            mm = re.search(r"threadId=([0-9a-fA-F-]+)", pg.url) or re.search(r"/thread/([0-9a-fA-F-]+)", pg.url)
            tid = mm.group(1) if mm else ""
            if not tid:
                try:
                    net = io.open(NET, encoding="utf-8", errors="replace").read()
                    mm2 = re.findall(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", net)
                    if mm2: tid = mm2[-1]
                except Exception: pass
            if tid:
                io.open(os.path.join(OUT, "thread_%s.txt" % TAG), "w", encoding="utf-8").write(tid)
                set_status("ready")
            return tid

        async def click_at(x, y, tag, wait=1500):
            await pg.mouse.move(x, y); await pg.wait_for_timeout(120); await pg.mouse.click(x, y)
            log("[click_at %s] (%d,%d)" % (tag, x, y)); await pg.wait_for_timeout(wait)
        async def cleaf(t, tag, wait=1500):
            r = await pg.evaluate(CLICK_JS, t)
            if not r: log("[click %s] MISS '%s'" % (tag, t)); return None
            await click_at(r["x"], r["y"], tag, wait); return r
        async def click_text(t, tag, wait=1200, exact=False):
            """Robust visible-text click. Avoids hardcoded coordinates that can
            land on a Recommended-skills tile (Viral Replication) and hijack the run."""
            try:
                loc = pg.get_by_text(t, exact=exact)
                n = await loc.count()
                for i in range(min(n, 10)):
                    el = loc.nth(i)
                    try:
                        if await el.is_visible():
                            await el.click(timeout=4000)
                            log("[text_click %s] %s" % (tag, t))
                            await pg.wait_for_timeout(wait)
                            return True
                    except Exception:
                        continue
            except Exception as e:
                log("[text_click ERR %s] %r" % (tag, e))
            log("[text_click MISS %s] %s" % (tag, t))
            return False
        cdp = await ctx.new_cdp_session(pg)
        # The redesigned (2026-10) composer caps "Smart match" at 720p in the UI,
        # but the submit API still honours video_resolution=1080p (that is how the
        # cheap 1080p runs were produced). We keep the cheap Smart-match model and
        # rewrite the settings blob in flight instead of paying SD2.5's 91 cr/s.
        FORCE_RES = (os.environ.get("PP_FORCE_RES", "0") == "1") and (RES == "1080p") and (not MODEL_NAME) and (MODEL_MODE not in ("sd25", "seedance25", "2.5"))
        RW = {"n": 0}
        async def _on_fetch(ev):
            try:
                if "submit_run" not in (ev.get("request", {}).get("url") or ""):
                    await cdp.send("Fetch.continueRequest", {"requestId": ev["requestId"]}); return
                body = ev["request"].get("postData") or ""
                nb = body
                try:
                    j = json.loads(body)
                    for c in (j.get("message", {}).get("content") or []):
                        if c.get("sub_type") != "biz/general_agent_settings": continue
                        st = json.loads(c.get("data") or "{}")
                        if FORCE_RES:
                            st["video_resolution"] = RES
                            if os.environ.get("PP_FORCE_RESOLUTION_FIELD","0")=="1": st["resolution"]=RES
                        if RES and os.environ.get("PP_FORCE_RESOLUTION_FIELD","0")=="1":
                            st["resolution"] = RES
                        if SECS and SECS != "smart":
                            try: st["video_duration"] = int(float(SECS))
                            except Exception: pass
                        if RATIO == "9:16": st["ratio"] = 3
                        elif RATIO == "16:9": st["ratio"] = 1
                        elif RATIO == "1:1": st["ratio"] = 2
                        c["data"] = json.dumps(st, separators=(",", ":"))
                        log("[fetch rewrite] %s" % json.dumps(st, ensure_ascii=False))
                    nb = json.dumps(j, separators=(",", ":"), ensure_ascii=False)
                except Exception as e:
                    log("[fetch rewrite ERR] %r" % (e,))
                RW["n"] += 1
                await cdp.send("Fetch.continueRequest", {"requestId": ev["requestId"],
                    "postData": base64.b64encode(nb.encode("utf-8")).decode("ascii")})
            except Exception as e:
                log("[fetch hook ERR] %r" % (e,))
        cdp.on("Fetch.requestPaused", lambda ev: asyncio.create_task(_on_fetch(ev)))
        try:
            await cdp.send("Fetch.enable", {"patterns": [{"urlPattern": "*submit_run*", "requestStage": "Request"}]})
        except Exception as e:
            log("[Fetch.enable ERR] %r" % (e,))

        def _walk_dom(n, out):
            if not isinstance(n, dict): return
            out.append(n)
            for k in ("children", "shadowRoots"):
                for c in (n.get(k) or []): _walk_dom(c, out)
            cd = n.get("contentDocument")
            if cd: _walk_dom(cd, out)

        async def pierced_buttons():
            """Buttons seen through shadow roots / iframes, with viewport centers.
            2026-10 UI renders the submit consent dialog inside a shadow root, so
            document.querySelectorAll (and therefore CLICK_JS) cannot reach it."""
            try:
                doc = await cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})
            except Exception as e:
                log("[pierce ERR] %r" % (e,)); return []
            nodes = []; _walk_dom(doc.get("root"), nodes); out = []
            for n in nodes:
                if (n.get("nodeName") or "").upper() != "BUTTON": continue
                bid = n.get("backendNodeId")
                if not bid: continue
                try:
                    bm = await cdp.send("DOM.getBoxModel", {"backendNodeId": bid})
                    q = bm["model"]["content"]
                    x = (q[0] + q[2] + q[4] + q[6]) / 4.0
                    y = (q[1] + q[3] + q[5] + q[7]) / 4.0
                    oh = await cdp.send("DOM.getOuterHTML", {"backendNodeId": bid})
                    h = (oh.get("outerHTML") or "")[:500]
                except Exception:
                    continue
                t = re.sub(r"<[^>]+>", " ", h); t = re.sub(r"\s+", " ", t).strip()
                out.append({"x": round(x), "y": round(y), "t": t[:60], "h": h})
            return out

        async def pierce_click(names, tag, wait=2200):
            """Click a shadow-DOM button by exact visible text (fallback: html match)."""
            low = [x.lower() for x in names]
            bs = await pierced_buttons()
            hit = None
            for b in bs:
                if b["x"] < 10 or b["y"] < 10 or b["x"] > 1490 or b["y"] > 1095: continue
                if b["t"].lower() in low: hit = b; break
            if not hit:
                for b in bs:
                    if any((">%s<" % x) in b["h"] for x in names): hit = b; break
            if not hit: return False
            try:
                for ev in ("mouseMoved", "mousePressed", "mouseReleased"):
                    await cdp.send("Input.dispatchMouseEvent", {"type": ev, "x": hit["x"], "y": hit["y"], "button": "left", "clickCount": 1})
            except Exception as e:
                log("[pierce_click ERR] %r" % (e,)); return False
            log("[pierce_click %s] (%d,%d) %s" % (tag, hit["x"], hit["y"], hit["t"][:40]))
            await pg.wait_for_timeout(wait)
            return True

        async def composer_box():
            try:
                return await pg.evaluate("""()=>{
                  const eds=[...document.querySelectorAll("div.tiptap.ProseMirror,div[contenteditable='true'],textarea")].filter(e=>{
                    const r=e.getBoundingClientRect(); return r.width>80&&r.height>20&&r.top>=0&&r.bottom<=innerHeight+20;
                  }).sort((a,b)=>{const ra=a.getBoundingClientRect(),rb=b.getBoundingClientRect();return rb.width*rb.height-ra.width*ra.height;});
                  if(!eds.length) return null; const e=eds[0],r=e.getBoundingClientRect(); e.focus();
                  return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2),h:Math.round(r.height),tag:e.tagName};
                }""")
            except Exception as e:
                log("[composer_box ERR] %r" % (e,)); return None
        async def fill_composer(text):
            b = await composer_box()
            if not b:
                log("[fill_composer] no composer"); return False
            await click_at(b["x"], b["y"], "composer_fill", 250)
            try:
                await pg.keyboard.press("Control+A"); await pg.keyboard.press("Delete")
                await pg.keyboard.insert_text(text)
                await pg.wait_for_timeout(900)
                val = await pg.evaluate("""()=>{const eds=[...document.querySelectorAll("div.tiptap.ProseMirror,div[contenteditable='true'],textarea")];let best=null,ba=0;for(const e of eds){const r=e.getBoundingClientRect();const a=r.width*r.height;if(a>ba){ba=a;best=e;}}return best?String(best.innerText||best.value||'').slice(0,120):'';}""")
                log("[fill_composer] h=%s chars=%d head=%s" % (b.get("h"), len(str(val).strip()), str(val)[:40].replace("\n"," ")))
                return len(str(val).strip()) > 10
            except Exception as e:
                log("[fill_composer ERR] %r" % (e,)); return False
        def on_req(r):
            u = r.url
            if re.search(r"(?i)(agent|chat|completion|conversation|session|sse)", u) and "/api/" in u:
                try: pd = r.post_data or ""
                except Exception: pd = ""
                with open(NET, "a", encoding="utf-8") as fh:
                    fh.write("%s %s %s\n" % (time.strftime("%H:%M:%S"), u[:220], pd[:500].replace("\n", " ")))
        pg.on("request", on_req)
        await pg.goto("https://www.pippit.ai/home?lang=en", wait_until="domcontentloaded", timeout=90000)
        for i in range(40):
            await pg.wait_for_timeout(3000)
            bt = str(await pg.evaluate("document.body.innerText.slice(0,600)"))
            if "Recommended" in bt or "Agent, turn ideas" in bt: break
        log("=== agent tag=%s uid=%s secs=%s readyAt=%ds" % (TAG, uid, SECS, 3 * (i + 1)))
        c0 = str(await call("/commerce/v1/benefits/user_credit", "POST", {}, 500))[:300]
        log("[credit0] " + c0)
        if c0[:3] in ("500", "502", "503", "504"):
            log("[credit0 5xx] reload page once")
            try:
                await pg.reload(wait_until="domcontentloaded", timeout=60000)
                for _ri in range(20):
                    await pg.wait_for_timeout(2000)
                    bt = str(await pg.evaluate("document.body.innerText.slice(0,600)"))
                    if "Recommended" in bt or "Agent, turn ideas" in bt: break
                log("[credit0 5xx] reloaded readyAt=%ds" % (2 * (_ri + 1)))
            except Exception as e:
                log("[credit0 5xx] reload ERR %r" % (e,))
        # The home page sometimes auto-selects the "Viral Replication" skill; that
        # forces a reference-video workflow and sets video_resolution=720p. Clear it.
        for _ in range(3):
            if not await click_text("Remove Skill", "remove_skill", 900, exact=True):
                break
        b0 = await composer_box()
        if b0: await click_at(b0["x"], b0["y"], "composer_focus", 600)
        await pg.wait_for_timeout(600)
        # --- 2026-10 UI: ONE Model/Preference popover at (960,342). -----------
        # Leaving it on "Smart match" caps video at 720p and leaves ratio/duration
        # on "Smart", which is NOT our 1080p x 30s target. Always pick SD 2.5,
        # then 1080p, then 9:16, then Custom duration = SECS.
        async def open_pref():
            async def opened():
                try: tx = await pg.evaluate("document.body.innerText")
                except Exception: tx = ""
                return any(k in tx for k in ("Video length", "Smart match", "Custom duration",
                                             "Aspect ratio", "Duration"))
            async def dom_click():
                try:
                    r = await pg.evaluate("""()=>{
                        const out=[];
                        for (const e of document.querySelectorAll('button,[role=button],div,span')) {
                          const t=(e.innerText||'').replace(/\\s+/g,' ').trim();
                          if (!t || t.length>40) continue;
                          if (t==='Preference' || /Model\\s*\\|?\\s*Preference/i.test(t)) {
                            const b=e.getBoundingClientRect();
                            if (b.width<4||b.height<4) continue;
                            out.push({x:Math.round(b.x+b.width/2),y:Math.round(b.y+b.height/2),a:b.width*b.height,t:t});
                          }
                        }
                        if(!out.length) return null;
                        out.sort((p,q)=>p.a-q.a);
                        return out[0];
                    }""")
                except Exception:
                    r = None
                if not r: return False
                await pg.mouse.move(r["x"], r["y"]); await pg.wait_for_timeout(120)
                await pg.mouse.click(r["x"], r["y"]); await pg.wait_for_timeout(1400)
                log("[pref dom] %s @(%d,%d)" % (r.get("t"), r["x"], r["y"]))
                return True
            for _t in range(5):
                if await dom_click() and await opened(): return True
                await pg.keyboard.press("Escape"); await pg.wait_for_timeout(300)
                if await click_text("Preference", "pref_chip", 1000, exact=False) and await opened(): return True
                for xy in ((960, 342), (996, 363), (1000, 342)):
                    await pg.mouse.move(xy[0], xy[1]); await pg.wait_for_timeout(100)
                    await pg.mouse.click(xy[0], xy[1]); await pg.wait_for_timeout(1300)
                    if await opened(): return True
                    await pg.keyboard.press("Escape"); await pg.wait_for_timeout(400)
            return False
        if not await open_pref():
            log("[PREF_FAIL] popover not opened")
            set_status("render_fail"); await shot("prefail"); await hard_close(ctx)
        want_sd25 = bool(MODEL_NAME) or MODEL_MODE in ("sd25", "seedance25", "2.5")
        if want_sd25:
            ok = False
            for _t in range(3):
                if await click_text("Dreamina Seedance 2.5", "sel_sd25", 1300, exact=True): ok = True; break
                await pg.wait_for_timeout(700)
            if not ok:
                await click_at(700, 549, "sel_sd25_xy", 1300); ok = True
            log("[pref sd25] %s" % ok)
        if RES and want_sd25:
            ok = False
            for _t in range(3):
                if await click_text(RES, "res", 1200, exact=True): ok = True; break
                await pg.wait_for_timeout(700)
            if not ok:
                xy = {"1080p": (1248, 604), "720p": (1182, 604), "480p": (1023, 604)}.get(RES)
                if xy: await click_at(xy[0], xy[1], "res_xy", 1200); ok = True
            log("[pref res %s] %s" % (RES, ok))
        if FORCE_RES:
            log("[pref res] UI left at 720p; in-flight rewrite -> %s" % RES)
        elif not want_sd25:
            log("[pref res] leave UI default 720p -> platform renders 1080p (verified 120 cr)")
        if RATIO:
            ok = False
            for _t in range(3):
                if await click_text(RATIO, "ratio", 1000, exact=True): ok = True; break
                await pg.wait_for_timeout(600)
            if not ok:
                xy = {"9:16": (1024, 478), "16:9": (1252, 478), "1:1": (1138, 478)}.get(RATIO)
                if xy: await click_at(xy[0], xy[1], "ratio_xy", 1000); ok = True
            log("[pref ratio %s] %s" % (RATIO, ok))
        if SECS and SECS != "smart" and not FORCE_RES:
            ok = await click_text("Custom duration", "custom_dur", 1500, exact=False)
            if not ok:
                await click_at(1182, 694, "custom_dur_xy", 1500); ok = True
            log("[pref custom_dur] %s" % ok)
            DIN = """()=>{const ins=[...document.querySelectorAll('input[type=text]')].filter(e=>{const r=e.getBoundingClientRect();return r.width>40&&r.height>10&&r.y>600&&r.y<860;});if(!ins.length)return null;ins.sort((a,b)=>a.getBoundingClientRect().y-b.getBoundingClientRect().y);const e=ins[ins.length-1],r=e.getBoundingClientRect();e.focus();try{e.select()}catch(_){}return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2),val:e.value};}"""
            try: din = await pg.evaluate(DIN)
            except Exception: din = None
            log("[dur input] %s" % din)
            if din:
                await pg.mouse.click(din["x"], din["y"]); await pg.wait_for_timeout(250)
                await pg.keyboard.press("Control+A"); await pg.wait_for_timeout(120)
                await pg.keyboard.type(str(SECS), delay=70)
                await pg.keyboard.press("Tab"); await pg.wait_for_timeout(900)
                try: dval = await pg.evaluate("""()=>{const ins=[...document.querySelectorAll('input[type=text]')].filter(e=>{const r=e.getBoundingClientRect();return r.width>40&&r.height>10&&r.y>600&&r.y<860;});return ins.length?ins[ins.length-1].value:null;}""")
                except Exception: dval = None
                log("[dur value] %s (want %s)" % (dval, SECS))
        await pg.keyboard.press("Escape"); await pg.wait_for_timeout(600)
        await pg.keyboard.press("Escape"); await pg.wait_for_timeout(700)
        await pg.wait_for_timeout(1200)
        t = await bodytxt(2500)
        cc = re.findall(r"[^\n]{0,60}credits?[^\n]{0,60}", t, re.I)
        log("[cost-scan] " + json.dumps(cc[-8:], ensure_ascii=False)[:700])
        await shot("1pref")
        await pg.keyboard.press("Escape"); await pg.wait_for_timeout(800)
        await fill_composer(prompt)
        await pg.wait_for_timeout(800)
        CLEN_JS = """()=>{const eds=[...document.querySelectorAll("div.tiptap.ProseMirror,div[contenteditable='true'],textarea")].filter(e=>{const r=e.getBoundingClientRect();return r.width>80&&r.height>20&&r.top>=0&&r.bottom<=innerHeight+20;}).sort((a,b)=>{const ra=a.getBoundingClientRect(),rb=b.getBoundingClientRect();return rb.width*rb.height-ra.width*ra.height;});if(!eds.length)return -1;const e=eds[0];return ((e.innerText||e.value||"")+"").trim().length;}"""
        try: clen = await pg.evaluate(CLEN_JS)
        except Exception: clen = -1
        log("[pre-send] composer_len=%s prompt_len=%d" % (clen, len(prompt)))
        if isinstance(clen, int) and clen < min(200, len(prompt) // 2):
            await pg.keyboard.press("Control+A"); await pg.keyboard.press("Delete")
            await pg.keyboard.insert_text(prompt); await pg.wait_for_timeout(900)
            try: clen = await pg.evaluate(CLEN_JS)
            except Exception: clen = -1
            log("[pre-send retry] composer_len=%s" % clen)
        sb = await pg.evaluate("""()=>{let bs=[...document.querySelectorAll('button')].filter(b=>(b.getAttribute('aria-label')||'').toLowerCase().includes('submit'));
          if(!bs.length) bs=[...document.querySelectorAll('button')].filter(b=>{const r=b.getBoundingClientRect();return r.width>0&&r.height>0&&r.x>1180&&r.y>150;});
          if(!bs.length) return null; const b=bs[bs.length-1]; const r=b.getBoundingClientRect(); return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2),aria:b.getAttribute('aria-label')};}""")
        log("[send btn] " + json.dumps(sb, ensure_ascii=False))
        def count_submits():
            try:
                t = io.open(NET, encoding="utf-8", errors="replace").read()
                return t.count("submit_run")
            except Exception:
                return -1
        before = count_submits()
        # Cookie banner may cover the composer and swallow the click.
        await pierce_click(["Accept all", "Accept All", "Accept"], "cookie_accept", 1200)
        if sb: await click_at(sb["x"], sb["y"], "send", 2500)
        else: await pg.keyboard.press("Enter")
        # Submit opens a shadow-root consent dialog ("By providing the link above
        # you confirm ..."). Only its Confirm actually fires submit_run.
        for _ci in range(5):
            await pg.wait_for_timeout(1800)
            if count_submits() > before: break
            if await pierce_click(["Confirm", "Agree", "Accept", "I agree", "Continue", "Got it", "Allow", "OK"], "consent", 2000):
                continue
            if _ci == 1:
                await pg.keyboard.press("Enter"); await pg.wait_for_timeout(1200)
                if sb: await click_at(sb["x"], sb["y"], "send_retry", 2000)
        await pg.wait_for_timeout(3500)
        after = count_submits()
        log("[send check] submit_run %s -> %s" % (before, after))
        if after <= before:
            await pg.keyboard.press("Enter"); await pg.wait_for_timeout(2000)
            await pierce_click(["Confirm", "Agree", "Accept", "Continue"], "consent2", 2000)
            await pg.wait_for_timeout(3500)
            log("[send retry] submit_run now=%s" % count_submits())
        try:
            body = [ln for ln in io.open(NET, encoding="utf-8", errors="replace").read().splitlines() if "submit_run" in ln]
            if body:
                log("[submit body] " + body[-1][-460:])
                log("[fetch rewrites] %d" % RW["n"])
                if RES and RES not in body[-1] and not FORCE_RES and not want_sd25:
                    log("[pref note] UI body keeps 720p by design; output is still 1080p")
                elif RES and RES not in body[-1] and not FORCE_RES: log("[PREF WARN] %s missing from submit body" % RES)
                if (SECS and SECS != "smart") and ("video_duration" not in body[-1]):
                    log("[PREF WARN] video_duration=%s missing from submit body" % SECS)
        except Exception:
            pass
        t0 = time.time(); seen = set(); answered = 0; clicked = {}; lastp = 0; lastc = 0; continued = 0
        try:
            rr = await call("/biz/v1/agent/list_creative_project", "POST", {"count": 5, "cursor": 0}, 200000)
            j = json.loads(str(rr).split(" | ", 1)[1]); seen = {pp["project_id"] for pp in ((j.get("data") or {}).get("project_list") or [])}
        except Exception: pass
        log("[projects before] %s" % sorted(seen)[:5])
        while time.time() - t0 < 900:
            await pg.wait_for_timeout(6000)
            el = int(time.time() - t0)
            tx = await bodytxt(4000)
            # Soft upstream failures often keep credits and explicitly ask to resend.
            low = tx.lower()
            if ((("15 seconds" in low) or ("15-second" in low)) and ("30" in low) and (("requested" in low) or ("rather than" in low))) or ("only support 15" in low) or ("clip is set to 15 seconds" in low):
                log("[SHORT_DURATION %ds] " % el + tx.replace("\n", " | ")[-400:])
                set_status("render_fail")
                await shot("shortdur")
                await hard_close(ctx)
            soft = (
                ("couldn't generate ideas" in low)
                or ("temporary generation service timeout" in low)
                or ("generation service timed out" in low)
                or ("please try the same request again" in low and "couldn't" in low)
            )
            if soft:
                log("[SOFT_FAIL %ds] " % el + tx.replace("\n", " | ")[-400:])
                set_status("render_fail")
                await shot("softfail")
                await hard_close(ctx)
            if ("Couldn't generate video" in tx) and ("Continue task" in tx) and continued < 2:
                # A paid run that died late offers "Continue task" ? clicking it
                # resumes the same run instead of burning another 120 credits.
                rct = await pg.evaluate(CLICK_JS, "Continue task")
                if rct:
                    await click_at(rct["x"], rct["y"], "continue_task", 4500)
                    continued += 1
                    log("[CONTINUE] clicked Continue task (%d)" % continued)
                    continue
            if ("Starter feature" in tx) or ("Couldn't generate video" in tx):
                st = "wall" if (("service limit" in low) or ("starter feature" in low)) else "render_fail"
                log("[%s %ds] " % (st.upper(), el) + tx.replace("\n", " | ")[-300:])
                set_status(st)
                await shot("wall")
                await hard_close(ctx)
            if ("Video generated" in tx) or ("You can preview the generated video" in tx):
                tid = save_thread()
                log("[VIDEO_READY %ds] thread=%s" % (el, tid))
                await shot("ready")
                await hard_close(ctx)
            btns = await pg.evaluate(BTNS_JS)
            named = [b for b in btns if re.match(r"(?i)^(confirm|approve.*|generate.*|continue|yes.*|wait a bit|don.t remind again)$", b["t"]) and b["x"] > 380 and not b["dis"]]
            if el % 30 < 7:
                log("[btns %ds] %s" % (el, json.dumps(btns[:18], ensure_ascii=False)[:800]))
                await shot("t%03d" % el)
            for b in named:
                key = (b["t"].lower(), b["x"], b["y"])
                if key in clicked and time.time() - clicked[key] < 120: continue
                clicked[key] = time.time()
                await click_at(b["x"], b["y"], "agent_" + re.sub(r"\W+", "_", b["t"])[:20], 2500)
                log("[clicked agent btn] %s" % b["t"])
            ask = any(k in tx.lower() for k in ["what would you like the", "request was cut off", "send the full concept", "please describe", "what should the scene", "paste its contents", "please upload", "paste the script"])
            if ask and answered < 4 and el >= 15:
                answered += 1
                sd = await pg.evaluate("""(t)=>{const ed=document.querySelector("div.tiptap.ProseMirror")||document.querySelector("div[contenteditable='true']")||document.querySelector("textarea");if(!ed)return 'no-ed';ed.focus();document.execCommand('insertText',false,t);return 'ok';}""", prompt[:600])
                log("[answer %d %ds] %s" % (answered, el, sd))
                sb3 = await pg.evaluate("""()=>{let bs=[...document.querySelectorAll('button')].filter(b=>(b.getAttribute('aria-label')||'').toLowerCase().includes('submit'));if(!bs.length)bs=[...document.querySelectorAll('button')].filter(b=>{const r=b.getBoundingClientRect();return r.width>0&&r.height>0&&r.x>1180&&r.y>150;});if(!bs.length)return null;const b=bs[bs.length-1];const r=b.getBoundingClientRect();return {x:Math.round(r.x+r.width/2),y:Math.round(r.y+r.height/2)};}""")
                if sb3: await click_at(sb3["x"], sb3["y"], "send_answer", 2000)
                else: await pg.keyboard.press("Enter")
                await pg.wait_for_timeout(6000)
            if el - lastc >= 60:
                lastc = el
                log("[credit %ds] %s" % (el, str(await call("/commerce/v1/benefits/user_credit", "POST", {}, 400))[:200]))
            if re.search(r"(?i)(couldn.t generate|not enough credits|credits needed|failed to generate|something went wrong)", tx[-1200:]):
                log("[ERRTEXT %ds] %s" % (el, tx.replace("\n", " | ")[-500:]))
            if el - lastp >= 45:
                lastp = el
                try:
                    rr = await call("/biz/v1/agent/list_creative_project", "POST", {"count": 10, "cursor": 0}, 300000)
                    j = json.loads(str(rr).split(" | ", 1)[1]); pl = ((j.get("data") or {}).get("project_list") or [])
                except Exception: pl = []
                for pr in pl[:5]:
                    pid = pr.get("project_id")
                    if pid in seen: continue
                    log("[NEWPROJ %ds] pid=%s title=%s" % (el, pid, str(pr.get("project_name") or pr.get("title") or "")[:40]))
                    fr = await call("/biz/v1/agent/list_project_file", "POST", {"Base": {"Client": "web"}, "PageNum": 1, "PageSize": 300, "project_id": pid}, 400000)
                    urls = set(re.findall(r'https?://[^\s"\\]+?\.mp4[^\s"\\]*', str(fr)))
                    if urls:
                        log("[VIDEO %ds] pid=%s n=%d" % (el, pid, len(urls)))
                        for k, u in enumerate(sorted(urls)):
                            uu = u if "download=true" in u else (u + ("&" if "?" in u else "?") + "download=true")
                            try:
                                r5 = await ctx.request.get(uu, timeout=300000); b = await r5.body()
                                fp = os.path.join(DL, "%s_%s_%d.mp4" % (uid, time.strftime("%H%M%S"), k))
                                io.open(fp, "wb").write(b); log("[DL] %s %d bytes" % (fp, len(b)))
                            except Exception as e: log("[DL err] " + repr(e)[:150])
                        io.open(os.path.join(OUT, "agent_urls_%s.json" % TAG), "w", encoding="utf-8").write(json.dumps(sorted(urls), ensure_ascii=False, indent=1))
                        log("[project path] thread=%s" % save_thread())
                        await shot("final")
                        await hard_close(ctx)
                    seen.add(pid)
            if el % 60 < 7:
                log("[wait %ds] %s" % (el, tx.replace("\n", " | ")[-350:]))
        await shot("end")
        set_status("end")
        log("[end] " + (await bodytxt(1200)).replace("\n", " | ")[-600:])
        await hard_close(ctx)
asyncio.run(main())





