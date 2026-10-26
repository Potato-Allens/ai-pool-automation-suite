# -*- coding: utf-8 -*-
"""ADB UI driver for the Feishu login / sign-up flow (no frida needed).

    python ui_driver.py --phone 13800138000 --code 123456 --no-launch
    python ui_driver.py --phone 13800138000 --code 123456 --team "Acme Ltd"

It reads the on-screen text with `uiautomator dump` and taps by text, so it
survives resolution changes. Screenshots are written to runs/ui-*.png.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

ADB = os.environ.get("ADB", r"C:\tools\platform-tools\platform-tools\adb.exe")
SERIAL = os.environ.get("ADB_SERIAL", "127.0.0.1:16384")

CLICK_ALIASES = {
    "login_phone": ["Login by phone number", "Phone Login", "手机号登录", "Phone number"],
    "next": ["Next", "下一步"],
    "agree": ["Agree", "同意"],
    "got_it": ["Got It", "知道了", "OK"],
    "skip": ["Skip", "跳过"],
    "done": ["Done", "完成", "Finish"],
    "continue": ["Continue", "继续"],
}


def adb(*args: str, timeout: int = 30) -> str:
    cmd = [ADB, "-s", SERIAL, *args]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    return p.stdout


def dump_ui() -> ET.Element:
    for _ in range(3):
        adb("shell", "uiautomator", "dump", "/sdcard/ui.xml")
        xml = adb("shell", "cat", "/sdcard/ui.xml")
        if xml.strip().startswith("<?xml"):
            break
        time.sleep(1)
    return ET.fromstring(xml)


def nodes(root: ET.Element):
    for n in root.iter("node"):
        text = (n.get("text") or "").strip()
        desc = (n.get("content-desc") or "").strip()
        yield n, text, desc


def bounds(n: ET.Element):
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", n.get("bounds") or "")
    if not m:
        return None
    x1, y1, x2, y2 = map(int, m.groups())
    return (x1 + x2) // 2, (y1 + y2) // 2


def find(root, labels):
    for n, text, desc in nodes(root):
        hay = (text + " " + desc).lower()
        for lab in labels:
            if lab.lower() in hay:
                return n, text or desc
    return None, None


def wait_text(labels, timeout=25, poll=1.0):
    end = time.time() + timeout
    while time.time() < end:
        root = dump_ui()
        n, t = find(root, labels)
        if n is not None:
            return root, n, t
        time.sleep(poll)
    return None, None, None


def tap_node(n: ET.Element):
    b = bounds(n)
    if not b:
        return False
    adb("shell", "input", "tap", str(b[0]), str(b[1]))
    return True


def tap_labels(labels, timeout=8) -> bool:
    root, n, t = wait_text(labels, timeout=timeout)
    if n is None:
        return False
    print(f"  tap {t!r}", flush=True)
    return tap_node(n)


def edit_texts(root):
    out = []
    for n, text, desc in nodes(root):
        if n.get("class", "").endswith("EditText"):
            b = bounds(n)
            if b:
                out.append((b, text, desc))
    return out


def type_into_first_edit(text: str) -> bool:
    root = dump_ui()
    edits = edit_texts(root)
    if not edits:
        return False
    b, _, _ = edits[0]
    adb("shell", "input", "tap", str(b[0]), str(b[1]))
    time.sleep(0.5)
    safe = text.replace(" ", "%s")
    adb("shell", "input", "text", safe)
    return True


def shot(tag: str):
    path = os.path.join("runs", f"ui-{tag}.png")
    os.makedirs("runs", exist_ok=True)
    adb("shell", "screencap", "-p", "/sdcard/ui.png")
    with open(path, "wb") as fh:
        fh.write(subprocess.run([ADB, "-s", SERIAL, "exec-out", "screencap", "-p"],
                                capture_output=True).stdout)
    print(f"  screenshot {path}", flush=True)


def click_text_job(job: str, timeout=6) -> bool:
    return tap_labels(CLICK_ALIASES[job], timeout=timeout)


def run(phone: str, code: str, team: str = "", launch: bool = True) -> int:
    if launch:
        adb("shell", "am", "start", "-n",
            "com.ss.android.lark/com.ss.lark.android.passport.biz.feature.login_new.NewLoginActivity")
        time.sleep(3)
    # dismiss any first-run dialogs
    click_text_job("got_it", 2)
    click_text_job("agree", 2)
    shot("01-login")

    click_text_job("login_phone", 5)
    time.sleep(1.5)
    shot("02-phone")

    if not type_into_first_edit(phone):
        print("[!] phone input not found", file=sys.stderr)
        return 2
    time.sleep(0.8)
    click_text_job("next", 5)
    time.sleep(2.5)
    click_text_job("agree", 4)
    time.sleep(4)
    shot("03-code")

    if code:
        if not type_into_first_edit(code):
            print("[!] code input not found", file=sys.stderr)
            return 3
        time.sleep(5)
        shot("04-after-code")

    # post-login onboarding: Skip -> Done
    if click_text_job("got_it", 3):
        time.sleep(1.5)
    if team:
        # if the team form is shown, fill name and press Next
        type_into_first_edit(team)
        click_text_job("next", 4)
        time.sleep(2)
    click_text_job("skip", 5)
    time.sleep(2)
    shot("05-skip")
    click_text_job("done", 6)
    time.sleep(2)
    shot("06-done")
    print("[*] ui flow finished", flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phone", required=True)
    ap.add_argument("--code", default="")
    ap.add_argument("--team", default="")
    ap.add_argument("--no-launch", action="store_true")
    a = ap.parse_args()
    return run(a.phone, a.code, a.team, launch=not a.no_launch)


if __name__ == "__main__":
    raise SystemExit(main())
