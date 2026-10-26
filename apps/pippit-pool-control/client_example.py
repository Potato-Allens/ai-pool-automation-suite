# -*- coding: utf-8 -*-
"""Minimal client for the Pippit pool API (OpenAI-style).

  python client_example.py --check          # key + quota + model list
  python client_example.py --prompt-file ..\\prompt_prod_coffee6_30.txt --out out.mp4
"""
import argparse, io, json, os, sys, time, urllib.request

BASE = os.environ.get("PP_BASE", "http://127.0.0.1:8787")
KEY = os.environ.get("PP_KEY") or io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                                      "client_main_key.txt"), encoding="ascii").read().strip()


def req(path, method="GET", body=None, raw=False, timeout=300):
    h = {"Authorization": "Bearer " + KEY}
    data = None
    if body is not None:
        h["Content-Type"] = "application/json"
        data = json.dumps(body).encode()
    r = urllib.request.Request(BASE + path, data=data, headers=h, method=method)
    with urllib.request.urlopen(r, timeout=timeout) as x:
        b = x.read()
        return b if raw else json.loads(b)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--prompt", default="")
    ap.add_argument("--prompt-file", default="")
    ap.add_argument("--out", default="out.mp4")
    a = ap.parse_args()
    if a.check or not (a.prompt or a.prompt_file):
        print("models:", [m["id"] for m in req("/v1/models")["data"]])
        print("usage :", json.dumps(req("/v1/usage"), ensure_ascii=False))
        return
    prompt = a.prompt or io.open(a.prompt_file, encoding="utf-8").read().strip()
    j = req("/v1/videos/generations", "POST",
            {"prompt": prompt, "model": "seedance-2.5", "resolution": "1080p",
             "duration": 30, "ratio": "9:16"})
    print("submitted", j["id"], j["status"], flush=True)
    while True:
        time.sleep(20)
        s = req("/v1/videos/generations/" + j["id"])
        print("%s %s%%" % (s["status"], s["progress"]), flush=True)
        if s["status"] in ("succeeded", "failed", "canceled"):
            break
    if s["status"] != "succeeded":
        print("error:", s.get("error")); sys.exit(1)
    blob = req("/v1/videos/generations/%s/content" % j["id"], raw=True, timeout=900)
    io.open(a.out, "wb").write(blob)
    print("saved", a.out, len(blob), "bytes")


if __name__ == "__main__":
    main()
