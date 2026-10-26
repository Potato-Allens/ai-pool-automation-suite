# -*- coding: utf-8 -*-
"""`vmax_verify.py` against the local mock: the 0-SMS corroboration steps.

A 303 with ``code=`` proves the OAuth leg.  It does **not** prove that

  * the cookie session is really authenticated, or
  * the ``accepted_agreement_ids`` posted with the form were persisted for the
    active versions.

Both are now read right after registration (no SMS, no token) and both are
recorded in the evidence file.  ``[2]`` is the interesting one: the mock
silently drops the ids (production failure mode), the probe notices, and the
verifier repairs once with ``POST /api/auth/agreements/accept``.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

os.environ["VMAX_NOPACE"] = "1"

import mock_e2e as M  # noqa: E402
import vmax_client as C  # noqa: E402
import vmax_verify as V  # noqa: E402

PHONE = "13800138000"
fails = []


def check(name, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" | {detail}"))
    if not cond:
        fails.append(name)


def reset(**kw):
    M.Cfg.users = {}
    M.Cfg.codes = {PHONE: "424242"}
    M.Cfg.counter = 0
    M.Cfg.sends = 0
    M.Cfg.register_tries = 0
    M.Cfg.attempts = {}
    M.Cfg.oauth = {}
    M.Cfg.reject_first = 0
    M.Cfg.omit_code = False
    M.Cfg.phone_taken = set()
    M.Cfg.agreements_accepted = kw.get("accepted", True)
    M.Cfg.drop_agreements = kw.get("drop", False)
    M.Cfg.status_calls = 0
    M.Cfg.accepts = 0
    M.Cfg.csrf_seen = ""
    M.Cfg.accept_rejects = 0


def run(tmp, tag):
    out = os.path.join(tmp, "verify_%s.jsonl" % tag)
    # vmax_verify reads sys.argv directly (it is a top-level script), so the
    # suite drives it the same way a shell would.
    argv = ["vmax_verify.py", "--phone", PHONE, "--skip-send-code", "--sms-code", "424242",
            "--base", M.Cfg.base, "--redirect", M.Cfg.base + "/auth/callback",
            "--register-captcha-param", "", "--out", out]
    saved = sys.argv
    sys.argv = argv
    try:
        rc = V.main()
    finally:
        sys.argv = saved
    with open(out, encoding="utf-8") as f:
        rows = [json.loads(line) for line in f if line.strip()]
    return rc, rows[-1]


def main() -> int:
    with M.Server() as srv, tempfile.TemporaryDirectory() as tmp:
        print("[1] a healthy registration: session is real, agreements stuck")
        reset()
        rc, ev = run(tmp, "ok")
        check("exit 0", rc == 0, rc)
        check("the OAuth code was captured", str(ev.get("code", "")).startswith("AUTH"), ev.get("code"))
        check("state echoed", ev.get("state_echoed") is True, ev.get("state_echoed"))
        check("session says authenticated",
              isinstance(ev.get("session"), dict) and ev["session"].get("authenticated") is True,
              ev.get("session"))
        ag = ev.get("agreements")
        check("agreements probed once", M.Cfg.status_calls >= 1, M.Cfg.status_calls)
        check("both active versions read as accepted",
              isinstance(ag, list) and len(ag) == 2 and all(a.get("accepted") for a in ag), ag)
        check("no repair was needed", M.Cfg.accepts == 0, M.Cfg.accepts)
        check("no heal block in the evidence", "agreements_healed" not in ev, list(ev))

        print("[2] the server silently dropped the ids: probe notices, repair runs once")
        reset(drop=True)
        rc, ev = run(tmp, "heal")
        check("exit 0 (still a good account)", rc == 0, rc)
        check("exactly one accept call", M.Cfg.accepts == 1, M.Cfg.accepts)
        heal = ev.get("agreements_healed")
        check("the heal block is in the evidence", isinstance(heal, dict), heal)
        check("the probe really saw the drop first",
              isinstance(heal, dict) and isinstance(heal.get("before"), list)
              and all(a.get("accepted") is False for a in heal["before"]), heal)
        check("accept returned the ids",
              isinstance(heal, dict) and heal.get("accept", {}).get("accepted_ids") == [12, 13], heal)
        ag2 = ev.get("agreements")
        check("after the repair everything reads accepted",
              isinstance(ag2, list) and all(a.get("accepted") for a in ag2), ag2)
        check("the repair carried the CSRF token",
              M.Cfg.csrf_seen == "mock-csrf", M.Cfg.csrf_seen)
        check("the CSRF gate did not reject the repair",
              M.Cfg.accept_rejects == 0, M.Cfg.accept_rejects)

        print("[3] no CSRF token: the repair is refused, the account still verifies")
        reset(drop=True)
        orig_s, orig_t = C.VmaxAuth.session, C.VmaxAuth.csrf_token
        C.VmaxAuth.session = lambda self: {}
        C.VmaxAuth.csrf_token = lambda self: ""
        try:
            rc, ev = run(tmp, "nocsrf")
        finally:
            C.VmaxAuth.session, C.VmaxAuth.csrf_token = orig_s, orig_t
        check("exit 0", rc == 0, rc)
        check("the server refused the repair",
              M.Cfg.accept_rejects == 1 and M.Cfg.accepts == 0,
              (M.Cfg.accept_rejects, M.Cfg.accepts))
        heal = ev.get("agreements_healed") or {}
        check("the refusal is recorded, not raised",
              isinstance(heal, dict) and "error" in (heal.get("accept") or {}), heal)
        still = ev.get("agreements")
        check("and the unsigned state is what ends up in the evidence",
              isinstance(still, list) and all(a.get("accepted") is False for a in still), still)

        print("[3] a broken side probe never kills a paid run")
        reset()
        orig = C.VmaxAuth.agreement_status

        def boom(self, types="terms,privacy"):
            raise RuntimeError("probe exploded")

        C.VmaxAuth.agreement_status = boom
        try:
            rc, ev = run(tmp, "boom")
        finally:
            C.VmaxAuth.agreement_status = orig
        check("exit 0", rc == 0, rc)
        check("the failure is recorded, not raised",
              isinstance(ev.get("agreements"), dict) and "error" in ev["agreements"],
              ev.get("agreements"))
        check("the OAuth code still landed", bool(ev.get("code")), ev.get("code"))

        print("[4] _soft is the contract the probes rely on")
        err = V._soft(lambda: 1 / 0)
        check("_soft returns an error dict", isinstance(err, dict) and "ZeroDivisionError" in err["error"], err)
        ok = V._soft(lambda: [1])
        check("_soft passes values through", ok == [1], ok)

    print("ALL VERIFY TESTS PASS" if not fails else f"FAILURES: {fails}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())
