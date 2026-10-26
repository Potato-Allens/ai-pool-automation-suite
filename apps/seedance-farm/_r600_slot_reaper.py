# -*- coding: utf-8 -*-
"""_r600_slot_reaper.py - reclaim surplus agent_slots held by a live line.
Keeps at most (running prod children + 1) slots per owner; deletes older surplus (>8min)."""
import io,os,sys,time,collections
sys.stdout.reconfigure(encoding="utf-8",errors="replace")
import psutil
C=r"<PROJECT_ROOT>"
SL=os.path.join(C,"work","agent_slots")
def prods_of(pid):
    n=0
    try:
        for ch in psutil.Process(pid).children(recursive=True):
            cl=" ".join(ch.cmdline() or [])
            if "_r329_prod" in cl or "_r329_agent" in cl: n+=1
    except Exception: pass
    return n
def tick():
    files=[os.path.join(SL,f) for f in os.listdir(SL) if f.endswith(".lock")]
    g=collections.defaultdict(list)
    for fp in files:
        try:
            pid=int((io.open(fp,encoding="utf-8",errors="replace").read() or "0").strip())
            age=time.time()-os.path.getmtime(fp)
        except Exception: continue
        g[pid].append((os.path.getmtime(fp),fp,age))
    killed=[]
    for pid,items in g.items():
        if not psutil.pid_exists(pid) or pid<=0:
            for _,fp,_ in items:
                try: os.remove(fp); killed.append((pid,os.path.basename(fp),"dead"))
                except Exception: pass
            continue
        keep=prods_of(pid)+1
        items.sort(reverse=True)
        surplus=items[keep:]
        for _,fp,age in surplus:
            if age<480: continue
            try: os.remove(fp); killed.append((pid,os.path.basename(fp),"surplus age=%d"%int(age)))
            except Exception: pass
    return killed
if __name__=="__main__":
    once="--once" in sys.argv
    while True:
        k=tick()
        if k: print(time.strftime("[%H:%M:%S]")+" reclaimed "+str(k),flush=True)
        if once: break
        time.sleep(60)
