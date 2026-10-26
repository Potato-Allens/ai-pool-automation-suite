# -*- coding: utf-8 -*-
"""Aliyun CaptchaV3 INPAINTING solver for auth.vobilemax.cn.

The site uses Aliyun 验证码2.0 (scene leukcmsx for dreammaker / 17rt8hvk for max).
The client must return `captcha_verify_param` = base64(JSON{certifyId, sceneId,
isSign, securityToken}) which the backend re-validates server side, so the puzzle
must really be solved.

Strategy
--------
* The background is the original image with the puzzle patch *inpainted out*, so
  the patch location is a low-detail island inside a textured scene. We score
  every horizontal offset with  mean(Laplacian energy inside piece mask) /
  mean(energy in a 1..5 px ring around the mask)  and drag to the minimum.
* One browser context == one device token. Aliyun risk control degrades a
  context after a failed attempt, so by default we burn a fresh context per try
  (about 2 s each) instead of retrying inside the popup.

Modes
-----
    auto   : heuristic + retries in fresh contexts
    manual : headed window, human drags the slider, we capture the param

CLI
---
    python vmax_captcha.py solve --platform dreammaker [--manual] [--headed]
    python vmax_captcha.py image bg.png piece.png      # offline scoring debug
"""
from __future__ import annotations

import argparse
import io
import json
import os
import random
import sys
import time
from typing import Optional

import numpy as np
from PIL import Image, ImageFilter

try:
    from playwright.sync_api import sync_playwright
except Exception:  # pragma: no cover
    sync_playwright = None

PAD = 8
MAX_LEFT = 260  # puzzle element travel: image width 300 - slider width 40


# --------------------------------------------------------------------------- #
# image scoring
# --------------------------------------------------------------------------- #
def piece_meta(piece_rgba: Image.Image):
    a = np.array(piece_rgba)[:, :, 3]
    ys, xs = np.nonzero(a > 10)
    if len(xs) == 0:
        raise ValueError("empty piece alpha")
    x0, y0 = int(xs.min()), int(ys.min())
    x1, y1 = int(xs.max() + 1), int(ys.max() + 1)
    return x0, y0, x1 - x0, y1 - y0


def score_offsets(bg: Image.Image, piece: Image.Image):
    """v2 scorer: robust ring median + quadrant balance + border guard.

    v1 used mean(ring) on edge-padded energy, which made pure-white / flat
    backgrounds win (their padded ring collapsed to ~0).  v2:
      * keeps the ring inside the image (drops candidates whose ring is <60 %
        in-bounds or whose piece crosses the border)
      * uses the median ring energy instead of the mean
      * splits the ring into 4 quadrants and penalises candidates where one
        side is texture-free (a real inpainted hole is surrounded by texture)
    """
    A = np.array(piece)
    x0, y0, w, h = piece_meta(piece)
    m = A[y0:y0 + h, x0:x0 + w, 3] > 10

    gray = np.array(bg.convert("L")).astype(np.float32)
    lap = np.abs(np.array(Image.fromarray(gray.astype(np.uint8)).filter(
        ImageFilter.Kernel((3, 3), [0, -1, 0, -1, 4, -1, 0, -1, 0], 1, 0))).astype(np.float32))
    energy = _uniform(lap, 3)
    H, W = energy.shape

    P = 6
    mp = np.zeros((h + 2 * P, w + 2 * P), bool)
    mp[P:P + h, P:P + w] = m
    ring = _dilate(mp, 5) & ~_dilate(mp, 1)
    ry, rx = np.nonzero(ring)
    ry = ry - P
    rx = rx - P

    rows = []
    MARGIN = 5  # a real hole is always surrounded by original content
    for x in range(MARGIN, W - w - MARGIN + 1):
        if y0 + h > H:
            continue
        win = energy[y0:y0 + h, x:x + w]
        inner = float(win[m].mean())
        yy = ry + y0
        xx = rx + x
        ok = (yy >= 0) & (yy < H) & (xx >= 0) & (xx < W)
        if ok.sum() < 0.6 * ring.sum():
            continue
        vals = energy[yy[ok], xx[ok]]
        rmed = float(np.median(vals))
        ang = np.arctan2(yy[ok] - (y0 + h / 2.0), xx[ok] - (x + w / 2.0))
        quads = []
        complete = True
        for q in range(4):
            sel = (ang >= (-np.pi + q * np.pi / 2)) & (ang < (-np.pi + (q + 1) * np.pi / 2))
            if sel.sum() <= 5:
                complete = False
                break
            quads.append(float(np.median(vals[sel])))
        if not complete:
            # ring clipped by the image border -> the "smooth" reading is an
            # artefact of the missing side, not an inpainted hole
            continue
        qmed = float(np.median(quads))
        qmin = min(quads)
        penalty = 0.0 if qmed <= 0 else max(0.0, (0.35 * qmed - qmin) / (qmed + 1e-6))
        rows.append((x, inner / (rmed + 1e-3) * (1.0 + 1.5 * penalty)))
    if not rows:
        raise RuntimeError("no valid candidate")
    rows.sort(key=lambda r: r[1])
    return rows, (x0, y0, w, h)


def _uniform(a: np.ndarray, size: int) -> np.ndarray:
    from scipy.ndimage import uniform_filter
    return uniform_filter(a, size=size)


def _dilate(m: np.ndarray, it: int) -> np.ndarray:
    from scipy.ndimage import binary_dilation
    return binary_dilation(m, iterations=it)


def annotate(bg: Image.Image, piece: Image.Image, x: int, path: str):
    from PIL import ImageDraw
    x0, y0, w, h = piece_meta(piece)
    big = bg.resize((600, 600), Image.LANCZOS)
    d = ImageDraw.Draw(big)
    d.rectangle([x * 2, y0 * 2, (x + w) * 2, (y0 + h) * 2], outline=(0, 255, 0), width=2)
    big.save(path)


def _grid(bg: Image.Image, y0: int, y1: int, path: str):
    from PIL import ImageDraw
    S = 3
    big = bg.resize((300 * S, 300 * S), Image.LANCZOS)
    d = ImageDraw.Draw(big)
    for gx in range(0, 301, 10):
        d.line([gx * S, 0, gx * S, 300 * S], fill=(255, 80, 80) if gx % 50 == 0 else (90, 90, 90))
        if gx % 50 == 0:
            d.text((gx * S + 2, 2), str(gx), fill=(255, 80, 80))
    for gy in range(0, 301, 50):
        d.line([0, gy * S, 300 * S, gy * S], fill=(90, 90, 90))
        d.text((2, gy * S + 1), str(gy), fill=(90, 90, 90))
    d.rectangle([0, y0 * S, 300 * S, y1 * S], outline=(0, 255, 0), width=2)
    big.save(path)


# --------------------------------------------------------------------------- #
# browser driver
# --------------------------------------------------------------------------- #
class InpaintingSolver:
    def __init__(self, headless: bool = True, proxy: Optional[str] = None,
                 debug_dir: Optional[str] = None, channel: str = "chrome"):
        if sync_playwright is None:
            raise RuntimeError("playwright is required")
        self.headless = headless
        self.proxy = proxy
        self.debug_dir = debug_dir
        self.channel = channel
        self._pw = None
        self._browser = None
        self.log: list[dict] = []
        if debug_dir:
            os.makedirs(debug_dir, exist_ok=True)

    # -- lifecycle --
    def __enter__(self):
        self._pw = sync_playwright().start()
        args = ["--disable-blink-features=AutomationControlled"]
        try:
            self._browser = self._pw.chromium.launch(channel=self.channel, headless=self.headless, args=args)
        except Exception:
            self._browser = self._pw.chromium.launch(headless=self.headless, args=args)
        return self

    def __exit__(self, *exc):
        try:
            if self._browser:
                self._browser.close()
        finally:
            if self._pw:
                self._pw.stop()

    def _new_context(self):
        kw = dict(locale="zh-CN", viewport={"width": 1440, "height": 900},
                  timezone_id="Asia/Shanghai")
        if self.proxy:
            kw["proxy"] = {"server": self.proxy}
        return self._browser.new_context(**kw)

    # -- one attempt in one fresh context --
    def _attempt(self, login_url: str, tag: str):
        ctx = self._new_context()
        page = ctx.new_page()
        inits: list[str] = []
        vers: list[str] = []

        def on_resp(resp):
            if "captcha-open.aliyuncs.com" in resp.url and resp.request.method == "POST":
                try:
                    body = resp.text()
                except Exception:
                    return
                if "INPAINTING" in body:
                    inits.append(body)
                if "VerifyResult" in body:
                    vers.append(body)

        page.on("response", on_resp)
        # never let the page really send an SMS; we send it ourselves
        page.route("**/register/send-code", lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"ok":true,"message":"blocked-by-tool","code":null}'))
        try:
            page.goto(login_url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1200)
            page.click('button[data-view-target="register"]')
            page.wait_for_timeout(250)
            page.fill("#register-phone", "1234")
            page.click('form#register-form button[data-send-code]')
            t0 = time.time()
            while time.time() - t0 < 15 and not inits:
                page.wait_for_timeout(250)
            if not inits:
                return None, {"tag": tag, "error": "no captcha init"}
            data = json.loads(inits[-1])
            bg_bytes = ctx.request.get("https://static-captcha.aliyuncs.com/" + data["Image"]).body()
            pz_bytes = ctx.request.get("https://static-captcha.aliyuncs.com/" + data["PuzzleImage"]).body()
            bg = Image.open(io.BytesIO(bg_bytes)).convert("RGB")
            pz = Image.open(io.BytesIO(pz_bytes)).convert("RGBA")
            rows, meta = score_offsets(bg, pz)
            # the slider only travels 0..MAX_LEFT, so anything beyond it is a
            # false positive (usually the flat image border) - take the best
            # candidate that is actually reachable.
            reachable = [r for r in rows if r[0] <= MAX_LEFT]
            target = (reachable or rows)[0][0]
            if self.debug_dir:
                bg.save(os.path.join(self.debug_dir, f"{tag}_bg.png"))
                pz.save(os.path.join(self.debug_dir, f"{tag}_pz.png"))
                annotate(bg, pz, target, os.path.join(self.debug_dir, f"{tag}_annot.png"))
            got = self._drag(page, target)
            if got is None:
                try:
                    rec_hint = page.inner_text("body")[:300]
                except Exception:
                    rec_hint = ""
            else:
                rec_hint = ""
            v0 = len(vers)
            t0 = time.time()
            while time.time() - t0 < 12 and len(vers) <= v0:
                page.wait_for_timeout(250)
            verify = json.loads(vers[-1]) if len(vers) > v0 else None
            ok = bool(verify and verify.get("Result", {}).get("VerifyResult") is True)
            param = ""
            for _ in range(20):
                param = page.evaluate(
                    "()=>[...document.querySelectorAll('input[name=captcha_verify_param]')]"
                    ".map(e=>e.value).filter(Boolean)[0]||''")
                if param:
                    break
                page.wait_for_timeout(250)
            rec = {"tag": tag, "certify_id": data["CertifyId"], "piece": meta,
                   "pick": target, "left": got, "ok": ok, "top": rows[:5],
                   "verify": verify.get("Result") if verify else None}
            if rec_hint:
                rec["page_text"] = rec_hint
            return (param if ok and param else None), rec
        finally:
            ctx.close()

    @staticmethod
    def _drag(page, target: float) -> Optional[float]:
        # the widget renders asynchronously; a missing slider usually means
        # "not painted yet", not "different captcha type"
        try:
            page.wait_for_selector("#aliyunCaptcha-sliding-slider", timeout=5000, state="visible")
        except Exception:
            pass
        sl = page.query_selector("#aliyunCaptcha-sliding-slider")
        if sl:
            try:
                sl.scroll_into_view_if_needed(timeout=2000)
            except Exception:
                pass
        box = sl.bounding_box() if sl else None
        if not box:
            return None
        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        page.mouse.move(cx + random.uniform(-1.5, 1.5), cy + random.uniform(-1.5, 1.5))
        page.wait_for_timeout(random.randint(70, 140))
        page.mouse.down()
        page.wait_for_timeout(random.randint(60, 120))
        cur = cx
        for _ in range(1200):
            left = page.evaluate(
                "()=>parseFloat(document.querySelector('#aliyunCaptcha-puzzle')?.style.left)||0")
            if left >= target - 0.4:
                break
            cur += max(0.4, min(4.0, (target - left) * 0.28 + 0.6))
            page.mouse.move(cur, cy + random.uniform(-1.2, 1.2), steps=1)
            page.wait_for_timeout(random.randint(12, 26))
        page.wait_for_timeout(random.randint(150, 320))
        left = page.evaluate(
            "()=>parseFloat(document.querySelector('#aliyunCaptcha-puzzle')?.style.left)||0")
        page.mouse.up()
        return left

    # -- human / VLM assisted --
    def solve_assisted(self, login_url: str, out_dir: str, timeout: int = 600,
                       use_stdin: bool = False) -> str:
        """Render a 10 px grid, let a human (or a VLM) type the target x, then drag.

        The helper writes <out_dir>/grid.png and <out_dir>/state.json, then polls
        <out_dir>/target.txt.  One captcha instance == one drag, so a wrong target
        costs one instance.
        """
        os.makedirs(out_dir, exist_ok=True)
        ctx = self._new_context()
        page = ctx.new_page()
        inits: list[str] = []
        vers: list[str] = []

        def on_resp(resp):
            if "captcha-open.aliyuncs.com" in resp.url and resp.request.method == "POST":
                try:
                    body = resp.text()
                except Exception:
                    return
                if "INPAINTING" in body:
                    inits.append(body)
                if "VerifyResult" in body:
                    vers.append(body)

        page.on("response", on_resp)
        page.route("**/register/send-code", lambda route: route.fulfill(
            status=200, content_type="application/json", body='{"ok":true}'))
        try:
            page.goto(login_url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1200)
            page.click('button[data-view-target="register"]')
            page.wait_for_timeout(250)
            page.fill("#register-phone", "1234")
            page.click('form#register-form button[data-send-code]')
            seen = 0
            deadline = time.time() + timeout
            while time.time() < deadline:
                if not inits:
                    page.wait_for_timeout(250)
                    continue
                if len(inits) == seen:
                    page.wait_for_timeout(250)
                    continue
                seen = len(inits)
                data = json.loads(inits[-1])
                bg = Image.open(io.BytesIO(ctx.request.get(
                    "https://static-captcha.aliyuncs.com/" + data["Image"]).body())).convert("RGB")
                pz = Image.open(io.BytesIO(ctx.request.get(
                    "https://static-captcha.aliyuncs.com/" + data["PuzzleImage"]).body())).convert("RGBA")
                x0, y0, w, h = piece_meta(pz)
                bg.save(os.path.join(out_dir, "bg.png"))
                pz.save(os.path.join(out_dir, "pz.png"))
                _grid(bg, y0, y0 + h, os.path.join(out_dir, "grid.png"))
                with open(os.path.join(out_dir, "state.json"), "w", encoding="utf-8") as f:
                    json.dump({"certify_id": data["CertifyId"], "piece": [x0, y0, w, h]}, f)
                if use_stdin:
                    print(f"[assist] piece y={y0}..{y0 + h} w={w}; grid: {out_dir}/grid.png")
                    target = int(input("[assist] target x: ").strip())
                else:
                    tf = os.path.join(out_dir, "target.txt")
                    target = None
                    while time.time() < deadline:
                        if os.path.exists(tf):
                            try:
                                target = int(float(open(tf).read().strip()))
                                break
                            except Exception:
                                pass
                        time.sleep(0.4)
                    if target is None:
                        raise RuntimeError("assist: no target.txt")
                    os.remove(tf)
                got = self._drag(page, min(target, MAX_LEFT))
                v0 = len(vers)
                t0 = time.time()
                while time.time() - t0 < 12 and len(vers) <= v0:
                    page.wait_for_timeout(250)
                ok = len(vers) > v0 and '"VerifyResult":true' in vers[-1]
                rec = {"target": target, "left": got, "ok": ok, "piece": [x0, y0, w, h],
                       "certify_id": data["CertifyId"]}
                self.log.append(rec)
                print(f"[assist] target={target} left={got} ok={ok}", flush=True)
                if ok:
                    for _ in range(20):
                        param = page.evaluate(
                            "()=>[...document.querySelectorAll('input[name=captcha_verify_param]')]"
                            ".map(e=>e.value).filter(Boolean)[0]||''")
                        if param:
                            return param
                        page.wait_for_timeout(250)
                page.wait_for_timeout(600)
            raise RuntimeError("assist: timed out")
        finally:
            ctx.close()

    # -- public API --
    def solve(self, login_url: str, max_attempts: int = 6, manual: bool = False,
              manual_timeout: int = 300) -> str:
        if manual:
            return self.solve_manual(login_url, timeout=manual_timeout)
        last = None
        for i in range(1, max_attempts + 1):
            param, rec = self._attempt(login_url, f"a{i}")
            rec["attempt"] = i
            self.log.append(rec)
            print(f"[captcha] attempt {i}: pick={rec.get('pick')} left={rec.get('left')} "
                  f"ok={rec.get('ok')} piece={rec.get('piece')}", flush=True)
            if param:
                return param
            last = rec
        raise RuntimeError(f"captcha not solved after {max_attempts} contexts: {last}")

    def solve_manual(self, login_url: str, timeout: int = 300) -> str:
        ctx = self._new_context()
        page = ctx.new_page()
        try:
            page.goto(login_url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(1000)
            page.click('button[data-view-target="register"]')
            print("[captcha] manual mode: drag the slider in the opened window...", flush=True)
            page.fill("#register-phone", "1234")
            page.click('form#register-form button[data-send-code]')
            t0 = time.time()
            while time.time() - t0 < timeout:
                param = page.evaluate(
                    "()=>[...document.querySelectorAll('input[name=captcha_verify_param]')]"
                    ".map(e=>e.value).filter(Boolean)[0]||''")
                if param:
                    print("[captcha] manual solve captured", flush=True)
                    return param
                page.wait_for_timeout(500)
            raise RuntimeError("manual captcha timed out")
        finally:
            ctx.close()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _cli():
    ap = argparse.ArgumentParser(description="Aliyun INPAINTING captcha solver (vobilemax)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("solve", help="solve one captcha and print captcha_verify_param")
    s.add_argument("--platform", default="dreammaker", choices=["dreammaker", "max"])
    s.add_argument("--manual", action="store_true", help="headed window, human solves")
    s.add_argument("--headed", action="store_true", help="show the auto solver window")
    s.add_argument("--attempts", type=int, default=6)
    s.add_argument("--proxy", default=None)
    s.add_argument("--debug-dir", default=None)
    s.add_argument("--out", default=None)
    s.add_argument("--login-url", default=None, help="override the login URL")
    s.add_argument("--assist", action="store_true", help="render grid, wait for target.txt")
    s.add_argument("--assist-dir", default="assist")
    s.add_argument("--stdin", action="store_true", help="read target x from stdin")

    i = sub.add_parser("image", help="score an offline bg/piece pair")
    i.add_argument("bg")
    i.add_argument("piece")
    i.add_argument("--annot", default=None)

    a = ap.parse_args()
    if a.cmd == "image":
        bg = Image.open(a.bg).convert("RGB")
        pz = Image.open(a.piece).convert("RGBA")
        rows, meta = score_offsets(bg, pz)
        print("piece", meta)
        print("top10", [(x, round(v, 3)) for x, v in rows[:10]])
        if a.annot:
            annotate(bg, pz, rows[0][0], a.annot)
        return

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from vmax_client import VmaxAuth, pkce_pair
    verifier, challenge = pkce_pair()
    url = a.login_url or VmaxAuth.login_url(challenge, state="cap" + str(int(time.time())))
    with InpaintingSolver(headless=not (a.headed or a.manual), proxy=a.proxy,
                          debug_dir=a.debug_dir) as sv:
        if a.assist:
            param = sv.solve_assisted(url, a.assist_dir, use_stdin=a.stdin)
        else:
            param = sv.solve(url, max_attempts=a.attempts, manual=a.manual)
    print(param)
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump({"captcha_verify_param": param, "log": sv.log}, f, ensure_ascii=False, indent=1)


if __name__ == "__main__":
    _cli()
