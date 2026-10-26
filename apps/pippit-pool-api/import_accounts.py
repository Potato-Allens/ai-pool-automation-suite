# -*- coding: utf-8 -*-
"""import_accounts.py - import account files into a running pool API.

Examples:
  python import_accounts.py --glob "..\\work\\funded_*.json"
  python import_accounts.py --file ..\\outputs\\evidence_r323\\pp_acc_XXX.json
"""
import argparse, glob, json, os, sys, requests

BASE = os.path.dirname(os.path.abspath(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default="http://127.0.0.1:8788")
    ap.add_argument("--token-file", default=os.path.join(BASE, "data", "admin_token.txt"))
    ap.add_argument("--token", default="")
    ap.add_argument("--glob", default="")
    ap.add_argument("--file", action="append", default=[])
    ap.add_argument("--no-bootstrap", action="store_true")
    ap.add_argument("--min-credit", type=int, default=0)
    a = ap.parse_args()

    tok = a.token or open(a.token_file, encoding="utf-8").read().strip()
    paths = list(a.file)
    if a.glob:
        paths += sorted(glob.glob(a.glob))
    if not paths:
        print("no input files")
        return 2

    r = requests.post(a.api.rstrip("/") + "/admin/accounts/import",
                      headers={"X-Admin-Token": tok},
                      json={"paths": paths, "bootstrap": not a.no_bootstrap,
                            "min_credit": a.min_credit}, timeout=1800)
    print("HTTP", r.status_code)
    j = r.json()
    print("imported", j.get("imported"), "ok", j.get("ok"))
    for x in (j.get("accounts") or [])[:30]:
        print(x)
    return 0 if r.status_code == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
