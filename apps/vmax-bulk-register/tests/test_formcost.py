# -*- coding: utf-8 -*-
"""The form-captcha price list has exactly one home (R152).

R151 was a whitelist bug: a runner kept its own tuple of "free" paths, R150 named
a new one, and the judge silently answered wrong.  This suite pins the price
list, the shared summary fields, and -- the point of the round -- that no other
module re-declares its own copy of the buckets.

    python tests/test_formcost.py
"""
from __future__ import annotations

import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import vmax_formcost as F  # noqa: E402

fails = []


def check(name, cond, detail=""):
    print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else f" | {detail}"))
    if not cond:
        fails.append(name)


def read(path):
    # utf-8-sig: vmax_go.py keeps a BOM, and ast.parse would choke on it
    return open(path, "rb").read().decode("utf-8-sig")


def price_tests():
    print("[1] six buckets, one price each")
    free = {"device": 0, "reuse": 0}
    paid = {"fresh": 1, "auto_retry": 1, "device_fallback": 1, "device_retry": 1}
    for kind, want in {**free, **paid}.items():
        got = F.extra_tokens(kind)
        check(f"cost of {kind} is {want}", got == want, got)
    for legacy in (None, "auto", "none"):
        check(f"legacy/unnamed {legacy!r} carries no price (None, not a fake 0)",
              F.extra_tokens(legacy) is None, F.extra_tokens(legacy))
        check(f"but {legacy!r} is still judged free (the old flow reused the token)",
              F.is_free(legacy) is True, F.is_free(legacy))
    check("a label we do not know is None, never 0", F.extra_tokens("who-knows") is None)
    check("an empty string is None, never 0", F.extra_tokens("") is None)
    check("is_free agrees with the price for the free ones",
          all(F.is_free(k) for k in free), [k for k in free if not F.is_free(k)])
    check("is_free says no for the paid ones",
          not any(F.is_free(k) for k in paid), [k for k in paid if F.is_free(k)])
    check("is_free says no for an unknown label", F.is_free("who-knows") is False)
    check("path() defaults to none", F.path({}) == "none" and F.path(None) == "none")
    check("path() reads the row", F.path({"form_captcha": "reuse"}) == "reuse")
    check("path() treats an empty field as none", F.path({"form_captcha": ""}) == "none")


def summary_tests():
    print("[2] the summary every runner shares")
    rows = [{"form_captcha": "device"},
            {"form_captcha": "device"},
            {"form_captcha": "reuse"},
            {"form_captcha": "device_retry"},
            {}]
    counts = F.count_of(rows)
    check("histogram counts every bucket", counts == {"device": 2, "reuse": 1,
                                                      "device_retry": 1, "none": 1}, counts)
    s = F.summarize(counts)
    check("device counted", s["form_device"] == 2, s)
    check("reuse counted apart from device", s["form_reuse"] == 1, s)
    check("fallback is the refused-device half", s["form_fallback"] == 1, s)
    check("extra tokens counts the paid buckets only", s["form_extra_tokens"] == 1, s)
    check("a row that never named its path is unknown, not free",
          s["form_unknown"] == 1, s)
    check("the histogram rides along", s["form_rows"] == counts, s)
    check("invariant: device + reuse + extra + unknown == rows",
          s["form_device"] + s["form_reuse"] + s["form_extra_tokens"]
          + s["form_unknown"] == len(rows), s)
    check("the key set is the documented one",
          set(s) == {"form_device", "form_reuse", "form_fallback",
                     "form_extra_tokens", "form_unknown", "form_rows"}, sorted(s))
    empty = F.summarize({})
    check("nothing to do -> zeros and an empty histogram",
          all(empty[k] == 0 for k in ("form_device", "form_reuse", "form_fallback",
                                      "form_extra_tokens", "form_unknown"))
          and empty["form_rows"] == {}, empty)
    allpaid = F.summarize({k: 1 for k in F.PAID})
    check("every paid bucket costs exactly one",
          allpaid["form_extra_tokens"] == len(F.PAID), allpaid)
    check("fallback never exceeds extra",
          allpaid["form_fallback"] <= allpaid["form_extra_tokens"], allpaid)
    check("free and paid do not overlap",
          not (set(F.FREE) & set(F.PAID)), (F.FREE, F.PAID))
    check("six buckets, no duplicates",
          len(F.FREE) + len(F.PAID) == 6 and len(set(F.FREE + F.PAID)) == 6,
          (F.FREE, F.PAID))


def single_home_tests():
    print("[3] nobody keeps a second copy of the buckets")
    consumers = ["vmax_go.py", "vmax_panel.py", "vmax_ready.py", "vmax_accept.py"]
    for name in consumers:
        text = read(os.path.join(ROOT, name))
        check(f"{name} imports the price list", "import vmax_formcost" in text)
    # A tuple/list of bucket names is a second price list.  Parsed, not grepped:
    # dict literals like {"form_captcha": "reuse"} are data and must not trip it.
    # Only real buckets: argparse choices like ["auto", "reuse", "fresh", "device"]
    # are *policies*, and flagging them would train the reader to ignore this test.
    buckets = set(F.FREE) | set(F.PAID)
    offenders = []
    # Production modules only: a test that spells ["device", "device"] is asserting
    # a value, not declaring a price list.
    for base in (ROOT,):
        for name in sorted(os.listdir(base)):
            if not name.endswith(".py"):
                continue
            if name in ("vmax_formcost.py", "test_formcost.py"):
                continue
            tree = ast.parse(read(os.path.join(base, name)))
            for node in ast.walk(tree):
                if not isinstance(node, (ast.Tuple, ast.List)) or len(node.elts) < 2:
                    continue
                vals = [e.value for e in node.elts
                        if isinstance(e, ast.Constant) and isinstance(e.value, str)]
                if len(vals) == len(node.elts) and set(vals) <= buckets:
                    offenders.append(f"{name}:{node.lineno}: {vals}")
    check("no production module hardcodes its own bucket tuple", offenders == [], offenders[:4])


def main() -> int:
    price_tests()
    summary_tests()
    single_home_tests()
    print("ALL FORMCOST TESTS PASS" if not fails else f"FAILURES: {fails}")
    return 0 if not fails else 1


if __name__ == "__main__":
    raise SystemExit(main())