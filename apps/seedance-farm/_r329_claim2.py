# -*- coding: utf-8 -*-
"""_r329_claim2.py <acct.json> [port] [prof] - 领 4 个免费任务积分(10005/10010/10008/10011) + 读余额"""
import asyncio, json, io, os, sys, time, hashlib
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
from playwright.async_api import async_playwright
C = r"<PROJECT_ROOT>"
OUT = os.path.join(C, "outputs", "evidence_r323")
LOG = os.path.join(OUT, "pp_claim2.log")
def log(*a):
    m = " ".join(str(x) for x in a); print(time.strftime("[%H:%M:%S] ") + m, flush=True)
    open(LOG, "a", encoding="utf-8").write(time.strftime("[%H:%M:%S] ") + m + "\n")
TASKS = (10005, 10010, 10008, 10011)
async def main():
    f = sys.argv[1]
    PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 21100
    PROF = sys.argv[3] if len(sys.argv) > 3 else os.path.join(C, "work", "pp_prof_claim2")
    d = json.load(io.open(f, encoding="utf-8-sig")); ck = d.get("cookies") or {}
    cks = [{"name": k, "value": v, "domain": ".pippit.ai", "path": "/", "secure": True, "httpOnly": False} for k, v in ck.items()]
    async with async_playwright() as pw:
        ctx = await pw.chromium.launch_persistent_context(PROF, channel="chrome", headless=False,
            proxy=(None if not PORT else {"server": "http://127.0.0.1:%d" % PORT}),
            args=["--no-sandbox","--disable-blink-features=AutomationControlled","--window-position=-32000,-32000","--window-size=1500,1100"],
            viewport={"width":1500,"height":1100}, locale="en-US")
        pg = ctx.pages[0] if ctx.pages else await ctx.new_page()
        await ctx.clear_cookies(); await ctx.add_cookies(cks)
        async def call(path, m="POST", b=None, lim=700):
            ts = int(time.time()*1000)
            s = hashlib.md5(("9e2c|%s|7|5.8.0|%d||11ac" % (path.split("?")[0][-7:], ts)).encode()).hexdigest()
            JS = """(a)=>{const init={method:a.m,credentials:'include',headers:{'sign':a.s,'device-time':String(a.ts),'sign-ver':'1','pf':'7','appvr':'5.8.0','Content-Type':'application/json'}};
                 if(a.b!==null) init.body=JSON.stringify(a.b);
                 return fetch('/api'+a.p, init).then(r=>r.text().then(t=>r.status+' | '+t.slice(0,a.lim))).catch(e=>'ERR '+e);}"""
            return await pg.evaluate(JS, {"p": path, "m": m, "b": b, "s": s, "ts": ts, "lim": lim})
        await pg.goto("https://www.pippit.ai/home?lang=en", wait_until="domcontentloaded", timeout=90000)
        await pg.wait_for_timeout(12000)
        hdr = str(await pg.evaluate("document.body.innerText.slice(0,400)")).replace(chr(10), " | ")
        log("[before] " + hdr[:200])
        log("[tasks] " + str(await call("/biz/v1/reward_task/list?task_type=1&version=v2", "GET", None, 1500))[:600])
        for t in TASKS:
            r1 = await call("/biz/v1/reward_task/finish", "POST", {"task_id": t})
            await pg.wait_for_timeout(500)
            r2 = await call("/biz/v1/reward_task/claim", "POST", {"task_id": t})
            log("  task %s finish=%s claim=%s" % (t, str(r1)[:90], str(r2)[:150]))
            await pg.wait_for_timeout(600)
        await pg.wait_for_timeout(2500)
        hdr2 = str(await pg.evaluate("document.body.innerText.slice(0,400)")).replace(chr(10), " | ")
        log("[after] " + hdr2[:200])
        await ctx.close()
asyncio.run(main())
