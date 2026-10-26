# -*- coding: utf-8 -*-
"""The cost gate: every runner must expose *and forward* --form-captcha.

R145 caught `vmax_ready`, R146 caught `vmax_panel`.  Both bugs looked the same
from the outside: the wrapper ran, the batch "succeeded", and each card silently
paid a second captcha token for the form.  Nothing in the 32 suites failed,
because every other suite calls one layer below the wrapper.

This suite is the systemic check, in three layers:

    [1] CLI surface    -- `--form-captcha {auto,reuse,fresh,device}` is in --help
    [2] it is real     -- a bogus value is rejected by argparse (exit 2), so the
                          flag is an argument, not a doc string
    [3] it is wired    -- the value reaches the code that spends the token:
                          register_one's namespace (go, panel) or the child
                          command line (ready -> day), and the default is the
                          documented one (auto everywhere, device in onecard)

`vmax_reset` is the deliberate exception: `/api/auth/reset-password/send-code`
does not accept a trusted device (R142), so it must *not* grow this flag -- that
is asserted too, otherwise someone will "fix" the asymmetry later.
"""
from __future__ import annotations

import ast
import argparse
import os
import re
import subprocess
import sys
import tempfile
from types import SimpleNamespace

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import vmax_accept  # noqa: E402
import vmax_go  # noqa: E402
import vmax_panel  # noqa: E402

PY = sys.executable
POLICIES = {"auto", "reuse", "fresh", "device"}
# tool -> expected default.  onecard is the only one that defaults to device:
# it is a per-card experiment, not a bulk runner, so the cheap path is the point.
RUNNERS = {
    "vmax_register.py": "auto",
    "vmax_go.py": "auto",
    "vmax_batch.py": "auto",
    "vmax_day.py": "auto",
    "vmax_ready.py": "auto",
    "vmax_accept.py": "auto",
    "vmax_panel.py": "auto",
    "vmax_onecard.py": "device",
}
fails = []


def check(name, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" | {detail}"))
    if not cond:
        fails.append(name)


def run(args, timeout=90):
    return subprocess.run([PY, "-u"] + args, cwd=ROOT, capture_output=True,
                          text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)


def help_text(tool):
    cp = run([tool, "--help"])
    return cp, " ".join((cp.stdout or "").split())


def flag_default(tool):
    """Read add_argument("--form-captcha", default=..., choices=[...]) via AST."""
    path = os.path.join(ROOT, tool)
    tree = ast.parse(open(path, encoding="utf-8-sig").read())
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "add_argument"):
            continue
        if not node.args or not isinstance(node.args[0], ast.Constant):
            continue
        if node.args[0].value != "--form-captcha":
            continue
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        default = getattr(kw.get("default"), "value", None)
        choices = [getattr(e, "value", None) for e in getattr(kw.get("choices"), "elts", [])]
        return default, choices
    return None, []


def main() -> int:
    print("[1] every bulk runner exposes --form-captcha with the four policies")
    for tool, _default in RUNNERS.items():
        cp, flat = help_text(tool)
        # the metavar appears twice (usage line inside [...], options block) and the
        # usage copy is followed by "]"; take every occurrence and accept any match.
        found = any(set(m.group(1).strip("{}").split(",")) == POLICIES
                    for m in re.finditer(r"--form-captcha (\{[^}]*\})", flat))
        check("%s: --help carries --form-captcha {%s}" % (tool, ",".join(sorted(POLICIES))),
              cp.returncode == 0 and found, (cp.returncode, flat[:160]))

    print("[2] the flag is a real argument: a bogus value is refused")
    for tool in RUNNERS:
        cp = run([tool, "--form-captcha", "bogus"])
        err = (cp.stderr or "") + (cp.stdout or "")
        check("%s: bogus policy -> exit 2" % tool,
              cp.returncode == 2 and "invalid choice" in err,
              (cp.returncode, err.strip().splitlines()[-1][:120] if err.strip() else ""))

    print("[3] the default is the documented one (auto everywhere, device in onecard)")
    for tool, default in RUNNERS.items():
        got, choices = flag_default(tool)
        check("%s: default=%s choices=4" % (tool, default),
              got == default and set(choices) == POLICIES, (got, choices))

    print("[4] go: the value lands in register_one's namespace (and defaults to auto)")
    seen = []

    def fake_register(ns):
        seen.append(ns.form_captcha)
        return {"phone": ns.phone, "status": "ok", "uid": "u1"}

    ns_go = SimpleNamespace(
        platform="dreammaker", captcha="param", captcha_param="X", captcha_attempts=1,
        queue_dir="tokens", queue_wait=0, assist_dir="assist", stdin=False, headed=False,
        sms="file:local", sms_dir="sms", sms_timeout=1, sms_resends=0, proxy=None,
        form_captcha="device", debug_dir=None, base="http://127.0.0.1:1",
        redirect="http://127.0.0.1:1/auth/callback", allow_virtual=False, journal=None)
    old_reg = vmax_go.register_one
    vmax_go.register_one = fake_register
    try:
        vmax_go.run_one({"phone": "13800000001"}, ns_go)
        ns_go2 = SimpleNamespace(**{k: v for k, v in vars(ns_go).items()
                                    if k != "form_captcha"})
        vmax_go.run_one({"phone": "13800000002"}, ns_go2)
    finally:
        vmax_go.register_one = old_reg
    check("go: --form-captcha device reaches register_one", seen[:1] == ["device"], seen)
    check("go: an old namespace still means auto", seen[1:2] == ["auto"], seen)

    print("[5] accept: the value lands on the vmax_register child command line")
    cmds = []

    def fake_run(cmd, echo_to=None):
        cmds.append(list(cmd))
        return 0, ""

    a = SimpleNamespace(phone="13800000003", sms="file:local", captcha="param",
                        queue_dir="tokens", sms_resends=0, form_captcha="device",
                        captcha_param="X", max_age=None, base="http://127.0.0.1:1",
                        redirect="http://127.0.0.1:1/auth/callback",
                        platform="dreammaker", proxy=None)
    old_run = vmax_accept._run
    vmax_accept._run = fake_run
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as td:
        os.chdir(td)
        try:
            vmax_accept.run_live(a)
            a2 = SimpleNamespace(**vars(a))
            a2.form_captcha = "fresh"
            vmax_accept.run_live(a2)
        finally:
            os.chdir(cwd)
            vmax_accept._run = old_run
    def _flag_value(cmd):
        return cmd[cmd.index("--form-captcha") + 1] if "--form-captcha" in cmd else None
    check("accept: device is forwarded to vmax_register", _flag_value(cmds[0]) == "device",
          cmds[0] if cmds else "no command")
    check("accept: the value is not hardcoded (fresh stays fresh)",
          _flag_value(cmds[1]) == "fresh", cmds[1] if len(cmds) > 1 else "no command")

    print("[6] ready: --dry-run shows the policy on the day command it would run")
    with tempfile.TemporaryDirectory() as td:
        cards = os.path.join(td, "cards.csv")
        with open(cards, "w", encoding="utf-8", newline="") as f:
            f.write("phone,username,password,proxy\n13800000004,,,\n")
        base_cmd = ["vmax_ready.py", "--input", cards, "--out", os.path.join(td, "out"),
                    "--dry-run"]
        cp_dev = run(base_cmd + ["--form-captcha", "device"])
        cp_auto = run(base_cmd)
        check("ready: device is on the day command",
              cp_dev.returncode == 0 and "--form-captcha device" in cp_dev.stdout,
              (cp_dev.returncode, cp_dev.stdout[-300:]))
        check("ready: without the flag the day command stays auto (no flag)",
              cp_auto.returncode == 0 and "--form-captcha" not in cp_auto.stdout,
              cp_auto.stdout[-300:])
        check("ready: the dry run created nothing", not os.path.exists(os.path.join(td, "out")))

    print("[7] panel: the value lands in register_one's namespace")
    class P(vmax_panel.Panel):
        name = "fake"

        def balance(self):
            return 1.0

        def acquire(self, service, country, max_price=None):
            return {"handle": "h1", "phone": "13800000005", "price": 0.0}

        def done(self, handle):
            pass

        def cancel(self, handle):
            pass

    got = []

    def fake_reg(ns):
        got.append(ns.form_captcha)
        return {"phone": ns.phone, "status": "ok", "uid": "u", "form_captcha": "device"}

    ns_panel = argparse.Namespace(
        _panel=P(), out=None, count=1, service="vobilemax", country="china",
        max_price=None, delay=0, max_fail=0, max_minutes=0, panel_recheck=5,
        panel_recheck_seconds=300.0, username=None, password=None, captcha="queue",
        captcha_param="", captcha_attempts=6, queue_dir="tokens", queue_wait=0,
        assist_dir="assist", stdin=False, headed=False, sms_dir="sms", sms_timeout=1,
        sms_resends=0, proxy=None, form_captcha="device", debug_dir=None, base=None,
        redirect=None, allow_virtual=False, platform="dreammaker", invite="")
    r = vmax_panel.run(ns_panel, register=fake_reg)
    check("panel: --form-captcha device reaches register_one", got == ["device"], got)
    check("panel: the summary records the policy and the ride",
          r["summary"].get("form_captcha") == "device" and r["summary"].get("form_device") == 1,
          r["summary"])

    print("[8] vmax_reset must NOT grow the flag (reset send-code refuses a device)")
    cp, flat = help_text("vmax_reset.py")
    check("reset: --help has no --form-captcha", cp.returncode == 0
          and "--form-captcha" not in flat, flat[:160])
    src = open(os.path.join(ROOT, "vmax_reset.py"), encoding="utf-8").read()
    check("reset: the docstring says why (device is not accepted here)",
          "device" in src and "send-code" in src, src[:160])

    print("ALL ENTRY-COST TESTS PASS" if not fails else f"FAILURES: {fails}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
