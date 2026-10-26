# -*- coding: utf-8 -*-
"""Offline checks for the panel-backed bulk runner (no panel, no credit, no SMS).

Every scenario drives the real ``vmax_panel.run``: the panel is faked, the
registration is injected, and the assertions are about *money* -- how many
numbers were bought, what was appended, and which stop rule fired.

[13]/[14] extend that to the *form* half of the bill: ``--form-captcha device``
has to survive the panel wrapper and actually reach ``POST /register`` as an
empty ``captcha_verify_param`` (1 token + 1 SMS per bought number instead of 2).
[14] is a real end-to-end run against ``tests/mock_e2e.py`` -- panel buy -> code ->
register -> 303 -> token -- so the pass-through is proven on the wire, not only
on the namespace.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import mock_e2e as M  # noqa: E402
import vmax_panel as P  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" | {detail}"))
    if not cond:
        fails.append(name)


class FakePanel(P.Panel):
    name = "fake"

    def __init__(self, phones, price=1.0, fail_balance_at=None):
        self.phones = list(phones)
        self.price = price
        self.fail_balance_at = fail_balance_at
        self.bought = 0
        self.finished = []
        self.cancelled = []
        self.probes = 0

    def balance(self):
        self.probes += 1
        if self.fail_balance_at is not None and self.probes >= self.fail_balance_at:
            raise P.PanelError("HTTP 502 from panel")
        return 12.5

    def acquire(self, service, country, max_price=None):
        if not self.phones:
            raise P.PanelError("no numbers in stock")
        self.bought += 1
        phone = self.phones.pop(0)
        return {"handle": "h%d" % self.bought, "phone": phone, "price": self.price}

    def done(self, handle):
        self.finished.append(handle)

    def cancel(self, handle):
        self.cancelled.append(handle)


def make_ns(panel, out=None, count=1, **kw):
    base = dict(_panel=panel, out=out, count=count, service="vobilemax",
                country="china", max_price=None, delay=0, max_fail=0,
                max_minutes=0, panel_recheck=5, panel_recheck_seconds=300.0,
                username=None, password=None, captcha="queue", captcha_param="",
                captcha_attempts=6, queue_dir="tokens", queue_wait=0,
                assist_dir="assist", stdin=False, headed=False,
                sms_dir="sms_inbox", sms_timeout=180, sms_resends=1,
                proxy=None, form_captcha="auto", debug_dir=None,
                base=None, redirect=None, allow_virtual=False,
                platform="dreammaker", invite="", dry_run=False,
                dry_phone="", dry_code="")
    base.update(kw)
    return argparse.Namespace(**base)


def make_register(rows):
    """Fake register_one: *rows* maps phone -> dict / exception."""
    calls = []

    def reg(ns_one):
        phone = ns_one.phone
        calls.append(phone)
        item = rows.get(phone, {"status": "ok"})
        if isinstance(item, BaseException):
            raise item
        row = dict(item)
        row.setdefault("phone", phone)
        row.setdefault("status", "ok")
        if row["status"] == "ok":
            row.setdefault("uid", "uid-" + phone[-4:])
            row.setdefault("access_token", "tok")
        return row

    return reg, calls


def reset_mock():
    """Back to a blank mock server: counters, codes, bans, agreements."""
    M.Cfg.codes.clear()
    M.Cfg.users.clear()
    M.Cfg.attempts.clear()
    M.Cfg.last_send.clear()
    M.Cfg.oauth.clear()
    M.Cfg.phone_taken.clear()
    M.Cfg.send_code_fail.clear()
    M.Cfg.counter = 0
    M.Cfg.sends = 0
    M.Cfg.register_tries = 0
    M.Cfg.login_tries = 0
    M.Cfg.cooldown = 0.0
    M.Cfg.reject_first = 0
    M.Cfg.rate_limit_after = 0
    M.Cfg.omit_code = False
    M.Cfg.drop_agreements = False


def read_rows(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def main():
    print("[0] the CLI surface keeps its defaults")
    ns = P.build_parser().parse_args([])
    check("--count 1", ns.count == 1, ns.count)
    check("--panel-recheck 5", ns.panel_recheck == 5, ns.panel_recheck)
    check("--panel-recheck-seconds 300.0", ns.panel_recheck_seconds == 300.0,
          ns.panel_recheck_seconds)
    check("--max-fail 0 (off)", ns.max_fail == 0, ns.max_fail)
    check("--out defaults to None (no resume file, no summary)", ns.out is None, ns.out)
    check("--form-captcha defaults to auto (an old command line keeps its cost)",
          ns.form_captcha == "auto", ns.form_captcha)
    ns_dev = P.build_parser().parse_args(["--form-captcha", "device"])
    check("--form-captcha device is accepted", ns_dev.form_captcha == "device",
          ns_dev.form_captcha)

    print("[1] _load_done counts only parseable rows and keeps the ok phones")
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "results.jsonl")
        with open(out, "w", encoding="utf-8") as f:
            f.write(json.dumps({"phone": "13800000001", "status": "ok"}) + "\n")
            f.write(json.dumps({"phone": "13800000002", "status": "error"}) + "\n")
            f.write("not json at all\n")
        done, rows = P._load_done(out)
        check("one ok phone", done == {"13800000001"}, done)
        check("two parseable rows", rows == 2, rows)
        check("missing file -> empty", P._load_done(os.path.join(td, "nope.jsonl")) == (set(), 0))
        check("no --out -> empty", P._load_done(None) == (set(), 0))

    print("[2] a met target spends nothing")
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "results.jsonl")
        with open(out, "w", encoding="utf-8") as f:
            f.write(json.dumps({"phone": "13800000001", "status": "ok"}) + "\n")
        panel = FakePanel(["13900000001"])
        reg, calls = make_register({})
        r = P.run(make_ns(panel, out=out, count=1), register=reg)
        check("exit 4", r["exit"] == 4, r["exit"])
        check("0 purchase", panel.bought == 0, panel.bought)
        check("0 registration", calls == [], calls)
        check("nothing appended", len(read_rows(out)) == 1, read_rows(out))
        check("summary says remaining_at_start=0",
              r["summary"]["remaining_at_start"] == 0, r["summary"])

    print("[3] happy path: two purchases, two rows, one summary")
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "run", "results.jsonl")
        panel = FakePanel(["13900000001", "13900000002"])
        reg, calls = make_register({})
        r = P.run(make_ns(panel, out=out, count=2), register=reg)
        rows = read_rows(out)
        check("exit 0", r["exit"] == 0, r["exit"])
        check("two purchases", panel.bought == 2 and len(calls) == 2, (panel.bought, calls))
        check("two rows on disk", len(rows) == 2, rows)
        check("both ok", all(x["status"] == "ok" for x in rows), rows)
        check("the row names the panel and its price",
              rows[0]["panel"] == "fake" and rows[0]["panel_price"] == 1.0
              and rows[0]["panel_handle"] == "h1", rows[0])
        check("activations were finished, not cancelled",
              panel.finished == ["h1", "h2"] and panel.cancelled == [],
              (panel.finished, panel.cancelled))
        check("summary next to the results file",
              os.path.exists(out + ".summary.json"), out)
        check("summary counts purchases and target",
              r["summary"]["bought"] == 2 and r["summary"]["ok"] == 2
              and r["summary"]["target_met"] is True, r["summary"])
        check("summary carries the exit code",
              r["summary"]["exit_code"] == 0, r["summary"].get("exit_code"))

    print("[4] --count is a target: rows already ok count toward it")
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "results.jsonl")
        with open(out, "w", encoding="utf-8") as f:
            f.write(json.dumps({"phone": "13800000009", "status": "ok"}) + "\n")
        panel = FakePanel(["13900000001", "13900000002"])
        reg, calls = make_register({})
        r = P.run(make_ns(panel, out=out, count=2), register=reg)
        check("only one more was bought", panel.bought == 1, panel.bought)
        check("the resumed phone was not re-registered",
              "13800000009" not in calls, calls)
        check("exit 0", r["exit"] == 0, r["exit"])
        check("resume is visible in the summary",
              r["summary"]["prior_ok"] == 1 and r["summary"]["remaining_at_start"] == 1,
              r["summary"])

    print("[5] an unexpected exception is a row, not a crash")
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "results.jsonl")
        panel = FakePanel(["13900000001"])
        reg, _ = make_register({"13900000001": RuntimeError("browser died")})
        r = P.run(make_ns(panel, out=out, count=1), register=reg)
        rows = read_rows(out)
        check("exit 1", r["exit"] == 1, r["exit"])
        check("the failed row is on disk", len(rows) == 1 and rows[0]["status"] == "error", rows)
        check("the error is kept", "browser died" in rows[0]["error"], rows[0])
        check("a fresh number is cancelled so we are not charged",
              panel.cancelled == ["h1"] and panel.finished == [],
              (panel.cancelled, panel.finished))
        check("summary counts the failure", r["summary"]["fail"] == 1, r["summary"])

    print("[6] phone_taken is a wasted purchase, and a bad pool is exit 7")
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "results.jsonl")
        panel = FakePanel(["13900000001"])
        err = P.RegisterError("send-code rejected: 已注册", kind="phone_taken",
                              result={"phone": "13900000001"})
        reg, _ = make_register({"13900000001": err})
        r = P.run(make_ns(panel, out=out, count=1), register=reg)
        rows = read_rows(out)
        check("the classified bucket survives",
              rows[0]["register_error_kind"] == "phone_taken", rows)
        check("counted as taken, not as a failure",
              r["summary"]["taken"] == 1 and r["summary"]["fail"] == 0, r["summary"])
        check("a pool of taken numbers exits 7", r["exit"] == 7, r["exit"])
        check("the activation is cancelled", panel.cancelled == ["h1"], panel.cancelled)

    print("[7] 429 aborts before the next purchase")
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "results.jsonl")
        panel = FakePanel(["13900000001", "13900000002", "13900000003"])
        reg, calls = make_register({"13900000001": {
            "status": "error", "error": "register failed: 429", "register_error_kind": "rate_limited"}})
        r = P.run(make_ns(panel, out=out, count=3), register=reg)
        check("exit 3", r["exit"] == 3, r["exit"])
        check("exactly one purchase", panel.bought == 1, panel.bought)
        check("one row", len(read_rows(out)) == 1, read_rows(out))
        check("summary marks the abort",
              r["summary"]["aborted_on_429"] is True, r["summary"])

    print("[8] the panel probe stops the run before the next purchase")
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "results.jsonl")
        panel = FakePanel(["13900000001", "13900000002", "13900000003"],
                          fail_balance_at=2)
        reg, calls = make_register({})
        r = P.run(make_ns(panel, out=out, count=3, panel_recheck=1), register=reg)
        check("exit 9", r["exit"] == 9, r["exit"])
        check("two purchases, the third number untouched",
              panel.bought == 2 and len(calls) == 2, (panel.bought, calls))
        check("two rows are kept", len(read_rows(out)) == 2, read_rows(out))
        check("two probes ran", r["summary"]["probes"] == 2, r["summary"])
        check("the reason is recorded and named",
              r["summary"]["stopped_on_panel_down"] is True
              and "502" in (r["summary"]["panel_down"] or ""), r["summary"])

    print("[9] an empty panel is a channel problem, not a failure loop")
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "results.jsonl")
        panel = FakePanel([])
        reg, calls = make_register({})
        r = P.run(make_ns(panel, out=out, count=3), register=reg)
        check("exit 9", r["exit"] == 9, r["exit"])
        check("0 purchase, 0 registration", panel.bought == 0 and calls == [],
              (panel.bought, calls))
        check("no row is invented", not os.path.exists(out), out)
        check("the panel error is the reason",
              "no numbers in stock" in (r["summary"]["panel_down"] or ""), r["summary"])

    print("[10] the same number twice is never paid for twice")
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "results.jsonl")
        panel = FakePanel(["13900000001", "13900000001"])
        reg, calls = make_register({})
        r = P.run(make_ns(panel, out=out, count=2), register=reg)
        check("one registration only", calls == ["13900000001"], calls)
        check("the duplicate is cancelled", panel.cancelled == ["h2"], panel.cancelled)
        check("one row", len(read_rows(out)) == 1, read_rows(out))
        check("the duplicate is counted", r["summary"]["panel_dupes"] == 1, r["summary"])
        check("target unmet -> exit 1", r["exit"] == 1, r["exit"])

    print("[11] --max-fail stops the run")
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "results.jsonl")
        panel = FakePanel(["13900000001", "13900000002", "13900000003"])
        bad = {"status": "error", "error": "code wrong", "register_error_kind": "code_wrong"}
        reg, calls = make_register({"13900000001": bad, "13900000002": bad, "13900000003": bad})
        r = P.run(make_ns(panel, out=out, count=3, max_fail=2), register=reg)
        check("exit 5", r["exit"] == 5, r["exit"])
        check("stopped after two failures", panel.bought == 2 and len(calls) == 2,
              (panel.bought, calls))
        check("summary marks it", r["summary"]["stopped_on_max_fail"] is True, r["summary"])
        check("error kinds are bucketed",
              r["summary"]["error_kinds"].get("code_wrong") == 2, r["summary"])

    print("[12] without --out nothing is written but the run still works")
    panel = FakePanel(["13900000001"])
    reg, calls = make_register({})
    r = P.run(make_ns(panel, out=None, count=1), register=reg)
    check("exit 0", r["exit"] == 0, r["exit"])
    check("the row is still returned in memory",
          len(r["results"]) == 1 and r["results"][0]["status"] == "ok", r["results"])
    check("no summary path", r["summary"]["out"] is None, r["summary"])

    print("[13] --form-captcha reaches register_one, and the summary audits the cost")
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "results.jsonl")
        panel = FakePanel(["13900000001", "13900000002"])
        seen = []

        def reg(ns_one):
            seen.append(ns_one.form_captcha)
            return {"phone": ns_one.phone, "status": "ok",
                    "uid": "u" + ns_one.phone[-2:], "form_captcha": "device"}

        r = P.run(make_ns(panel, out=out, count=2, form_captcha="device"), register=reg)
        check("the policy is on every namespace register_one sees",
              seen == ["device", "device"], seen)
        check("every row says device",
              [x.get("form_captcha") for x in read_rows(out)] == ["device", "device"],
              read_rows(out))
        check("summary records the policy", r["summary"]["form_captcha"] == "device",
              r["summary"])
        check("summary counts the device rides", r["summary"]["form_device"] == 2,
              r["summary"])
        check("a fallback would be counted separately",
              r["summary"]["form_fallback"] == 0, r["summary"])
        check("device rides are the whole bill (reuse 0, extra 0)",
              r["summary"].get("form_reuse") == 0
              and r["summary"].get("form_extra_tokens") == 0, r["summary"])

        out2 = os.path.join(td, "results2.jsonl")

        def reg2(ns_one):
            return {"phone": ns_one.phone, "status": "ok", "uid": "u",
                    "form_captcha": "device_fallback"}

        r2 = P.run(make_ns(FakePanel(["13900000003"]), out=out2, count=1,
                           form_captcha="device"), register=reg2)
        check("a refused device is visible as an extra token",
              r2["summary"]["form_device"] == 0 and r2["summary"]["form_fallback"] == 1,
              r2["summary"])
        check("...and it is counted as a real extra token",
              r2["summary"].get("form_extra_tokens") == 1, r2["summary"])

    print("[14] real end-to-end on the mock: --form-captcha device = 1 token + 1 SMS")
    os.environ["VMAX_NOPACE"] = "1"
    with tempfile.TemporaryDirectory() as td, M.Server():
        reset_mock()
        M.Cfg.codes_path = os.path.join(td, "codes.jsonl")
        M.Cfg.sms_dir = ""
        phone = "13700137191"
        out = os.path.join(td, "results.jsonl")
        rc = P.main(["--provider", "mock", "--dry-run",
                     "--dry-phone", phone, "--dry-code", "%06d" % 1,
                     "--count", "1", "--out", out,
                     "--base", M.Cfg.base,
                     "--redirect", M.Cfg.base + "/auth/callback",
                     "--captcha", "param", "--captcha-param", M.Cfg.token,
                     "--form-captcha", "device",
                     "--sms-timeout", "15", "--sms-resends", "0"])
        rows = read_rows(out) if os.path.exists(out) else []
        check("exit 0", rc == 0, rc)
        check("one row, ok", len(rows) == 1 and rows[0].get("status") == "ok", rows[:1])
        check("the row rode the device path",
              rows[0].get("form_captcha") == "device", rows[:1])
        dev = (rows[0] or {}).get("device") or {}
        check("device.ok and signup-check said no captcha",
              dev.get("ok") is True
              and (dev.get("signup_check") or {}).get("requires_captcha") is False, dev)
        check("exactly one SMS", M.Cfg.sends == 1, M.Cfg.sends)
        check("exactly one /register write", M.Cfg.register_tries == 1,
              M.Cfg.register_tries)
        check("the register answered 303 with a code",
              ((rows[0] or {}).get("register") or {}).get("status") == 303,
              (rows[0] or {}).get("register"))
        summ = {}
        if os.path.exists(out + ".summary.json"):
            summ = json.load(open(out + ".summary.json", encoding="utf-8"))
        check("summary: policy device, one device ride",
              summ.get("form_captcha") == "device" and summ.get("form_device") == 1,
              summ)
        check("the panel row also records what was bought",
              (rows[0] or {}).get("panel") == "mock"
              and (rows[0] or {}).get("panel_handle") == "mock-1", (rows[0] or {}))

        print("     control: the same run without the flag must not claim the device path")
        reset_mock()
        out2 = os.path.join(td, "results2.jsonl")
        rc2 = P.main(["--provider", "mock", "--dry-run",
                      "--dry-phone", "13700137192", "--dry-code", "%06d" % 1,
                      "--count", "1", "--out", out2,
                      "--base", M.Cfg.base,
                      "--redirect", M.Cfg.base + "/auth/callback",
                      "--captcha", "param", "--captcha-param", M.Cfg.token,
                      "--sms-timeout", "15", "--sms-resends", "0"])
        rows2 = read_rows(out2) if os.path.exists(out2) else []
        check("control exit 0", rc2 == 0, rc2)
        # R150: the reuse path is named instead of leaving the field absent, so
        # "device" vs "paid" is decidable from the row alone.
        check("control row says reuse, not device",
              (rows2[0] if rows2 else {}).get("form_captcha") == "reuse"
              and "device" not in (rows2[0] if rows2 else {}), rows2[:1])
        summ2 = {}
        if os.path.exists(out2 + ".summary.json"):
            summ2 = json.load(open(out2 + ".summary.json", encoding="utf-8"))
        check("control summary: policy auto, 0 device rides",
              summ2.get("form_captcha") == "auto" and summ2.get("form_device") == 0,
              summ2)

    print("ALL PANEL TESTS PASS" if not fails else f"FAILURES: {fails}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
