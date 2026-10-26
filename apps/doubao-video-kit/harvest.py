#!/usr/bin/env python3
# -*- coding: utf-8 -*-
r"""harvest.py - 从豆包会话里收割所有视频并去水印下载

用法:
  python harvest.py --conversation 38448538091490818 --out downloads
  python harvest.py --url https://www.doubao.com/chat/38448538091490818 --list
  python harvest.py --conversation 3844... --cookies C:\path\cookies.json

依赖: 标准库即可 (urllib)。cookies.json 形如 {"sessionid":"...", ...}
"""
import argparse, json, os, re, sys, time, urllib.request, urllib.error
from urllib.parse import urlencode

BASE = "https://www.doubao.com"
Q = {"aid": 582478, "device_platform": "desktop", "version_code": 20800,
     "pkg_type": "release_version", "language": "zh", "region": "CN", "samantha_web": 1}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36")


def load_cookies(path=None):
    path = path or os.environ.get("DOUBAO_COOKIES")
    if not path:
        for c in ("cookies.json", os.path.join("work", "cookies.json")):
            if os.path.exists(c):
                path = c
                break
    if not path or not os.path.exists(path):
        sys.exit("找不到 cookies.json，请用 --cookies 指定")
    d = json.load(open(path, encoding="utf-8"))
    if isinstance(d, dict):
        return "; ".join(f"{k}={v}" for k, v in d.items() if v)
    return d  # 已是 cookie 字符串


def post(path, body, cookie, timeout=40):
    req = urllib.request.Request(
        BASE + path + "?" + urlencode(Q), data=json.dumps(body).encode(),
        headers={"content-type": "application/json", "user-agent": UA,
                 "referer": "https://www.doubao.com/chat/",
                 "origin": "https://www.doubao.com", "cookie": cookie},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return json.loads(e.read().decode("utf-8", "replace"))


def collect_videos(conversation_id, cookie, pages=6, page_size=25):
    """翻页抓消息，抽出所有 creation 视频"""
    found = {}
    for p in range(pages):
        idx = list(range(p * page_size, (p + 1) * page_size))
        j = post("/alice/message/index_list",
                 {"conversation_id": conversation_id,
                  "message_index_list": idx, "is_reverse": False}, cookie)
        if j.get("code") != 0:
            print("  ! index_list code", j.get("code"), j.get("msg"))
            break
        ml = j.get("data", {}).get("message_list") or []
        for m in ml:
            for vid, meta in iter_creations(m.get("content")):
                found[vid] = meta
        if len(ml) < page_size:
            break
    return found


def iter_creations(content):
    if not content:
        return
    try:
        blocks = json.loads(content) if isinstance(content, str) else content
    except Exception:
        return
    if not isinstance(blocks, list):
        return
    for b in blocks:
        c = b.get("content")
        if isinstance(c, str):
            try:
                c = json.loads(c)
            except Exception:
                continue
        if not isinstance(c, dict):
            continue
        for cr in c.get("creations") or []:
            v = cr.get("video") or {}
            vid = v.get("vid")
            if vid:
                yield vid, {"duration": v.get("duration"), "width": v.get("width"),
                            "height": v.get("height"), "creation_id": cr.get("id")}


def unwatermarked(vids, cookie):
    j = post("/creativity/resource/get_without_watermark", {"vid": list(vids)}, cookie)
    if j.get("code") != 0:
        sys.exit(f"去水印失败: {j.get('code')} {j.get('msg')}")
    return j["data"]["download_video"]


def download(url, out, cookie=None):
    os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
    h = {"user-agent": UA, "referer": "https://www.doubao.com/"}
    if cookie:
        h["cookie"] = cookie
    tmp = out + ".part"
    got = 0
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers=h)
            with urllib.request.urlopen(req, timeout=180) as r, open(tmp, "wb") as f:
                total = int(r.headers.get("content-length") or 0)
                while True:
                    chunk = r.read(262144)
                    if not chunk:
                        break
                    f.write(chunk)
                    got += len(chunk)
                    if total:
                        print(f"\r  {got*100//total:3d}%  {got}/{total}", end="", flush=True)
            print()
            os.replace(tmp, out)
            return got
        except Exception as e:
            print(f"\n  重试 {attempt+1}/3: {e}")
            time.sleep(2)
    if os.path.exists(tmp):
        os.remove(tmp)
    raise SystemExit("下载失败")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--conversation", help="会话 id，如 38448538091490818")
    ap.add_argument("--url", help="或直接给 chat 链接")
    ap.add_argument("--cookies", help="cookies.json 路径")
    ap.add_argument("--out", default="downloads")
    ap.add_argument("--list", action="store_true", help="只列 vid 不下载")
    ap.add_argument("--pages", type=int, default=6)
    a = ap.parse_args()

    cid = a.conversation
    if not cid and a.url:
        m = re.search(r"/chat/(\d+)", a.url)
        cid = m.group(1) if m else None
    if not cid:
        sys.exit("需要 --conversation 或 --url")

    ck = load_cookies(a.cookies)
    print(f"会话 {cid} 扫描中...")
    vids = collect_videos(cid, ck, pages=a.pages)
    if not vids:
        print("没有找到视频（该会话可能没有产物）")
        return
    print(f"发现 {len(vids)} 条视频:")
    for vid, m in vids.items():
        print(f"  {vid}  {m.get('width')}x{m.get('height')}  {m.get('duration')}s  creation={m.get('creation_id')}")
    if a.list:
        return

    info = unwatermarked(vids.keys(), ck)
    for vid, meta in info.items():
        url = meta["download_url"]
        ext = "mp4" if meta.get("video_type", "mp4") == "mp4" else "bin"
        out = os.path.join(a.out, f"{vid}.{ext}")
        print(f"下载 {vid} ...")
        n = download(url, out, ck)
        print(f"  -> {out}  {n} bytes  {meta.get('duration')}s")


if __name__ == "__main__":
    main()
