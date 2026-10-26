# -*- coding: utf-8 -*-
"""Batch Feishu registration / team creation driver.

Inputs
    --phones phones.txt          one phone per line ("13800138000" or "phone,code")
    --provider d1jiema           pull numbers from d1jiema (--count, --keyword)
    --manual                     ask for the SMS code on stdin (real SIM cards)

Examples
    python batch_register.py --phones phones.txt --manual --team-name "Cloud{i} Ltd"
    python batch_register.py --provider d1jiema --token XXX --count 5 --keyword 飞书 \
        --team-name "Acme{i}" --out accounts.jsonl
    python batch_register.py --phones phones.txt --dry-run        # credential only

Every attempt is appended to --out (JSONL) and runs/<phone>-<ts>.json.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import string
import sys
import time
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import lark_passport as lp  # noqa: E402

try:
    import d1jiema  # noqa: E402
except Exception:  # pragma: no cover
    d1jiema = None


def rand_name(rng: random.Random) -> str:
    a = "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
    b = "伟芳娜秀英敏静丽强磊军洋勇艳杰娟涛明超霞平刚桂英"
    return rng.choice(a) + rng.choice(b) + (rng.choice(b) if rng.random() < 0.5 else "")


def rand_tenant(rng: random.Random) -> str:
    a = "云启星辰智联远航纵横博远嘉禾鼎晟弘毅新程未来光年"
    b = "科技信息网络数据智能"
    return rng.choice(a) + rng.choice(a) + rng.choice(b) + "有限公司"


def expand(tpl: str, i: int, rng: random.Random) -> str:
    return (tpl.replace("{i}", str(i))
               .replace("{rand}", "".join(rng.choice(string.ascii_lowercase + string.digits) for _ in range(6))))


def load_phones(path: str) -> list[tuple[str, str]]:
    out = []
    with open(path, encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in re.split(r"[,\s]+", line) if p.strip()]
            phone = parts[0]
            code = parts[1] if len(parts) > 1 else ""
            out.append((phone, code))
    return out


class Runner:
    def __init__(self, args):
        self.args = args
        self.rng = random.Random(args.seed) if args.seed else random.Random()
        self.out = args.out
        os.makedirs(args.log_dir, exist_ok=True)

    def log(self, tag: str, msg: str, verbose=True):
        line = f"[{tag}] {msg}"
        if verbose:
            print(line, flush=True)
        with open(os.path.join(self.args.log_dir, f"{tag}.log"), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def emit(self, rec: dict):
        with open(self.out, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def get_code(self, phone: str, prefill: str = "") -> str:
        if prefill:
            return prefill
        if self.args.manual:
            return input(f"SMS code for {phone}: ").strip()
        if not d1jiema:
            raise RuntimeError("d1jiema module missing")
        kw = tuple(k for k in self.args.keywords.split(",") if k)
        logger = lambda m: self.log(phone, m, self.args.verbose)
        return d1jiema.poll_code(phone, keywords=kw, timeout=self.args.sms_timeout,
                                 token=self.args.token or None, logger=logger)

    def run_one(self, phone: str, prefill: str, idx: int) -> dict:
        t0 = time.time()
        rng = self.rng
        tenant = expand(self.args.team_name, idx, rng) if self.args.team_name else rand_tenant(rng)
        user = expand(self.args.user_name, idx, rng) if self.args.user_name else rand_name(rng)
        prof = lp.Profile(user_name=user, tenant_name=tenant,
                          staff_size=self.args.staff_size,
                          industry_type=self.args.industry,
                          region_code=self.args.region,
                          is_doubao=self.args.doubao)
        p = lp.Passport(device_id=None if self.args.new_device else self.args.device_id or None,
                        host=self.args.host)
        rec = {"phone": phone, "tenant": tenant, "user": user, "device_id": p.device_id,
               "ts": int(t0), "ok": False, "stage": "credential"}
        try:
            is_email = "@" in phone
            ctype = 2 if is_email else 1
            cred = p.credential(phone, action=self.args.action, credential_type=ctype)
            stage = lp.classify(cred)
            rec["stage"] = "credential"
            rec["class"] = stage
            rec["flow_key"] = p.flow_key
            self.log(phone, f"credential -> {stage} next={(cred.get('data') or {}).get('next_step')}")
            if stage.startswith("error"):
                rec["error"] = cred
                return self.finish(rec, t0)
            if stage == "captcha":
                rec["captcha"] = True
                return self.finish(rec, t0)
            if stage == "banned":
                rec["banned"] = True
                rec["detail"] = (cred.get("data") or {}).get("step_info", {}).get("subtitle", "")
                return self.finish(rec, t0)
            if self.args.dry_run:
                rec["ok"] = True
                rec["dry_run"] = True
                return self.finish(rec, t0)

            if stage.startswith("verify"):
                rec["stage"] = "apply"
                ap = p.apply_code()
                self.log(phone, f"apply -> {ap.get('code')} {(ap.get('data') or {}).get('next_step')}")
                if ap.get("code") not in (0, None):
                    rec["error"] = ap
                    return self.finish(rec, t0)
                code = self.get_code(phone, prefill)
                self.log(phone, f"code={code}")
                rec["stage"] = "verify"
                vr = p.verify_code(code)
                vstage = lp.classify(vr)
                rec["verify_class"] = vstage
                self.log(phone, f"verify -> {vstage}")
                if vstage == "banned":
                    rec["banned"] = True
                    rec["detail"] = (vr.get("data") or {}).get("step_info", {}).get("subtitle", "")
                    return self.finish(rec, t0)
                if vstage.startswith("error"):
                    rec["error"] = vr
                    return self.finish(rec, t0)
                resp = vr
            else:
                resp = cred

            rec["stage"] = "onboarding"
            final, history = p.drive(resp, prof, logger=lambda m: self.log(phone, m, self.args.verbose))
            rec["steps"] = [s.next_step for s in history]
            rec["final_step"] = (final.get("data") or {}).get("next_step")
            rec["final_code"] = final.get("code", final.get("biz_code"))
            if lp.is_banned(final):
                rec["banned"] = True
            elif rec["final_code"] not in (0, None):
                rec["error"] = final
            else:
                rec["ok"] = True
            rec["last"] = final
            return self.finish(rec, t0)
        except Exception as exc:
            rec["error"] = repr(exc)
            rec["trace"] = traceback.format_exc()[-1500:]
            self.log(phone, f"EXC {exc!r}")
            return self.finish(rec, t0)

    def finish(self, rec: dict, t0: float) -> dict:
        rec["elapsed"] = round(time.time() - t0, 1)
        self.emit(rec)
        with open(os.path.join(self.args.log_dir, f"{rec['phone']}-{rec['ts']}.json"), "w", encoding="utf-8") as fh:
            json.dump(rec, fh, ensure_ascii=False, indent=1)
        return rec


def build_phones(args) -> list[tuple[str, str]]:
    if args.phones:
        return load_phones(args.phones)
    if args.provider == "d1jiema" and d1jiema:
        out = []
        for i in range(args.count):
            ph = d1jiema.get_phone(keyword=args.keyword, card_type=args.card_type,
                                   token=args.token or None)
            out.append((ph, ""))
            print(f"[provider] {i+1}/{args.count} -> {ph}", flush=True)
            time.sleep(0.5)
        return out
    raise SystemExit("need --phones or --provider d1jiema")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--phones", default="")
    ap.add_argument("--provider", default="", choices=["", "d1jiema"])
    ap.add_argument("--count", type=int, default=1)
    ap.add_argument("--token", default=os.environ.get("D1JIEMA_TOKEN", ""))
    ap.add_argument("--keyword", default="飞书")
    ap.add_argument("--keywords", default="飞书,Feishu,Lark")
    ap.add_argument("--card-type", default="实卡")
    ap.add_argument("--manual", action="store_true", help="ask for SMS code on stdin")
    ap.add_argument("--dry-run", action="store_true", help="credential call only, no SMS")
    ap.add_argument("--team-name", default="")
    ap.add_argument("--user-name", default="")
    ap.add_argument("--staff-size", default="MDES00000013")
    ap.add_argument("--industry", default="MDID00000157")
    ap.add_argument("--region", default="")
    ap.add_argument("--doubao", action="store_true", help="send is_doubao=true")
    ap.add_argument("--action", type=int, default=lp.ACTION_UG_REGISTER)
    ap.add_argument("--device-id", default="")
    ap.add_argument("--host", default=lp.HOST, help="accounts.larksuite.com for Lark email signup")
    ap.add_argument("--new-device", action="store_true", help="random fingerprint per account")
    ap.add_argument("--delay", type=float, default=2.0)
    ap.add_argument("--sms-timeout", type=int, default=180)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", default="accounts.jsonl")
    ap.add_argument("--log-dir", default="runs")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    phones = build_phones(args)
    if args.limit:
        phones = phones[: args.limit]
    print(f"[*] {len(phones)} phone(s), out={args.out}", flush=True)

    r = Runner(args)
    ok = banned = fail = 0
    for i, (ph, code) in enumerate(phones, 1):
        print(f"\n=== [{i}/{len(phones)}] {ph}", flush=True)
        rec = r.run_one(ph, code, i)
        ok += bool(rec.get("ok"))
        banned += bool(rec.get("banned"))
        fail += not rec.get("ok") and not rec.get("banned")
        if i < len(phones):
            time.sleep(args.delay)
    print(f"\n[*] done: ok={ok} banned={banned} failed={fail} -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
