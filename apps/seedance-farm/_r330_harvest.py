# -*- coding: utf-8 -*-
"""R-WZ-66: 原生 1080p 30s 成品收割 + 目录化 (Pippit 线)
扫描 R329_pippit/final，校验 1080x1920/1920x1080 + 30s + CLEAN，复制到交付库并写 manifest。
"""
import argparse, csv, glob, io, json, os, shutil, subprocess, sys, time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
C = r"<PROJECT_ROOT>"
FINAL = os.path.join(C, "outputs", "R329_pippit", "final")
MANI = os.path.join(C, "outputs", "R329_pippit", "manifest.jsonl")
WORK = os.path.join(C, "work")
LOG = os.path.join(WORK, "_r330_harvest.log")
CAT = os.path.join(WORK, "_r330_catalog.json")
CSVP = os.path.join(WORK, "_r330_catalog.csv")
CREATE_NO_WINDOW = 0x08000000


def log(m):
    line = "[%s] %s" % (time.strftime("%H:%M:%S"), m)
    print(line, flush=True)
    with io.open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def probe(p):
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=codec_name,width,height,avg_frame_rate,bit_rate",
                        "-show_entries", "format=duration,size,bit_rate", "-of", "json", p],
                       capture_output=True, timeout=60, creationflags=CREATE_NO_WINDOW)
    j = json.loads((r.stdout or b"{}").decode("utf-8", "replace"))
    st = (j.get("streams") or [{}])[0]
    fm = j.get("format") or {}
    fps = st.get("avg_frame_rate") or "0/1"
    try:
        n, d = fps.split("/"); fps = round(float(n) / float(d), 3)
    except Exception:
        fps = 0
    return {"w": st.get("width"), "h": st.get("height"), "codec": st.get("codec_name"),
            "fps": fps, "dur": float(fm.get("duration") or 0), "size": int(fm.get("size") or 0),
            "br": int(fm.get("bit_rate") or 0)}


def wm_check(p):
    try:
        fp = os.path.join(WORK, "_wm_check.py")
        r = subprocess.run([sys.executable, fp, p], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=180,
                           creationflags=CREATE_NO_WINDOW)
        lines = (r.stdout or "").strip().splitlines()
        return json.loads(lines[-1]) if lines else {}
    except Exception:
        return {}

def load_manifest():
    rows = {}
    try:
        for line in io.open(MANI, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if r.get("status") != "ok" or not r.get("gate_ok"):
                continue
            if not r.get("final") or not os.path.isfile(r["final"]):
                continue
            rows[r.get("tag") or ""] = r
            if r.get("final"):
                rows["@path:" + os.path.normcase(os.path.abspath(r["final"]))] = r
    except Exception as e:
        log("manifest read err %r" % e)
    return rows


def tag_of(name):
    # S4_37a_snow_fox... -> S4_37a ; U1_0_... -> U1_0
    parts = name.split("_")
    if len(parts) >= 2 and parts[1][:1].isdigit():
        return "_".join(parts[:2])
    return parts[0]


def one_pass(dst, copy, allow_unverified=False):
    m = load_manifest()
    cat = {}
    try:
        cat = json.load(io.open(CAT, encoding="utf-8"))
    except Exception:
        pass
    new = 0
    bad = 0
    for p in sorted(glob.glob(os.path.join(FINAL, "*.mp4"))):
        name = os.path.basename(p)
        tag = tag_of(name)
        info = probe(p)
        if info["w"] not in (1080, 1920) or info["h"] not in (1080, 1920):
            bad += 1; continue
        if abs(info["dur"] - 30.0) > 0.25:
            bad += 1; continue
        r = m.get(tag) or m.get("@path:" + os.path.normcase(os.path.abspath(p)))
        if r is None and not allow_unverified:
            w = wm_check(p)
            if w.get("verdict") == "CLEAN":
                r = {"status": "ok", "gate_ok": True, "wm": w,
                     "ts": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(os.path.getmtime(p))),
                     "prompt": "", "credit": {}}
            else:
                bad += 1; continue
        if r and (r.get("status") != "ok" or not r.get("gate_ok") or r.get("wm", {}).get("verdict") != "CLEAN"):
            bad += 1; continue
        key = name
        if key not in cat:
            entry = {"file": name, "tag": tag, "path": p, "w": info["w"], "h": info["h"],
                     "fps": info["fps"], "dur": info["dur"], "bytes": info["size"], "br": info["br"],
                     "codec": info["codec"], "ts": (r or {}).get("ts"), "uid": (r or {}).get("uid"),
                     "credit": ((r or {}).get("credit") or {}).get("total"),
                     "prompt": ((r or {}).get("prompt") or "")[:200],
                     "wm": ((r or {}).get("wm") or {}).get("verdict"),
                     "gate_ok": (r or {}).get("gate_ok")}
            if copy:
                os.makedirs(dst, exist_ok=True)
                dp = os.path.join(dst, name)
                if not os.path.isfile(dp):
                    tmp = dp + ".part"
                    shutil.copy2(p, tmp)
                    os.replace(tmp, dp)
                entry["delivered"] = dp
            cat[key] = entry
            new += 1
    with io.open(CAT, "w", encoding="utf-8") as f:
        json.dump(cat, f, ensure_ascii=False, indent=1)
    rows = sorted(cat.values(), key=lambda x: (x.get("ts") or ""), reverse=True)
    with io.open(CSVP, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["file", "tag", "uid", "credit", "wm", "gate_ok", "w", "h", "fps", "dur_s", "MB", "Mbps", "codec", "ts", "delivered", "prompt"])
        for x in rows:
            w.writerow([x["file"], x["tag"], x.get("uid"), x.get("credit"), x.get("wm"), x.get("gate_ok"),
                        x["w"], x["h"], x["fps"], round(x["dur"], 3), round(x["bytes"] / 1048576, 2),
                        round(x.get("br", 0) / 1e6, 2), x["codec"], x.get("ts"), x.get("delivered", ""),
                        (x.get("prompt") or "").replace("\n", " ")])
    return len(cat), new, bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dst", default=r"<OUTPUT_DIR>\native")
    ap.add_argument("--copy", action="store_true")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--interval", type=int, default=120)
    ap.add_argument("--allow-unverified", action="store_true")
    a = ap.parse_args()
    while True:
        try:
            total, new, bad = one_pass(a.dst, a.copy, a.allow_unverified)
            log("pass: catalog=%d new=%d invalid=%d dst=%s copy=%s" % (total, new, bad, a.dst, a.copy))
        except Exception as e:
            log("pass err %r" % e)
        if not a.watch:
            break
        time.sleep(max(30, a.interval))


if __name__ == "__main__":
    main()