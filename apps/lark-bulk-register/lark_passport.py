# -*- coding: utf-8 -*-
"""Feishu passport protocol client (native app flavour).

Captured 2026-10-18 from Feishu 8.2.6 (com.ss.android.lark) on Android 15.
The passport API is plain JSON over HTTPS - no request signature, no captcha.

    from lark_passport import Passport
    p = Passport()
    r = p.credential("13800138000")          # -> verify_list + x-flow-key
    p.apply_code()                            # send SMS
    r = p.verify_code("123456")               # -> next_step ...
    r = p.drive(r, Profile(tenant_name="Acme"))  # create team / set name / info

Endpoints (all POST JSON unless noted):
    /accounts/login/credential
    /accounts/auth/code/apply
    /accounts/auth/code/verify
    /accounts/register/prepare_tenant      (GET)
    /accounts/tenant/prepare
    /accounts/tenant/create
    /accounts/tenant/set_name
    /accounts/tenant/information
"""
from __future__ import annotations

import json
import random
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

import requests

HOST = "https://accounts.feishu.cn"
UA = "okhttp/3.12.13.18"
API_VERSION = "1.0.30"

# action values from the web bundle enum
ACTION_CREATE = 0
ACTION_UG_REGISTER = 1
ACTION_LARK_REGISTER = 2

VERIFY_STEPS = ("verify_list", "verify_code_mobile", "verify_pwd", "verify_otp", "verify_mo")
TERMINAL_STEPS = ("ok", "enter_app", "finish", "done", "close_all", "close", None)
TEAM_STEPS = ("create_tenant_new", "tenant_create", "register", "dispatch_register")


def fmt_phone(phone: str) -> str:
    """'+86190 3030 8410' - the exact grouping the app sends."""
    p = re.sub(r"\D", "", phone)
    if p.startswith("86") and len(p) == 13:
        p = p[2:]
    if len(p) == 11:
        return "+86" + p[:3] + " " + p[3:7] + " " + p[7:]
    return "+86" + p


def rand_device_id() -> str:
    return str(random.randint(7_000_000_000_000_000_000, 7_999_999_999_999_999_999))


@dataclass
class Profile:
    """What to fill into the server-driven onboarding steps."""
    user_name: str = ""
    tenant_name: str = ""
    staff_size: str = "MDES00000013"      # 1-9 people
    industry_type: str = "MDID00000157"   # Education
    region_code: str = ""
    flow_type: str = "register"
    is_doubao: bool = False
    trusted_mail_in: bool = False


@dataclass
class Step:
    next_step: str
    step_info: dict = field(default_factory=dict)
    resp: dict = field(default_factory=dict)


class Passport:
    def __init__(self, device_id: str | None = None, front_uuid: str | None = None,
                 unit: str = "eu_nc", locale: str = "en_US", timeout: int = 30,
                 proxy: str | None = None, host: str = HOST):
        self.device_id = device_id or rand_device_id()
        self.front_uuid = front_uuid or str(uuid.uuid4())
        self.unit = unit
        self.locale = locale
        self.timeout = timeout
        self.host = host.rstrip("/")
        self.flow_key: str | None = None
        self.s = requests.Session()
        # ignore WinINET/registry proxy (127.0.0.1:8899 MITM breaks TLS)
        self.s.trust_env = False
        self.s.headers.update({"User-Agent": UA, "Accept-Encoding": "gzip"})
        if proxy:
            self.s.proxies.update({"http": proxy, "https": proxy})

    # ---------------- transport ----------------
    def _headers(self) -> dict[str, str]:
        dev = (
            f"device_id={self.device_id};rust_version=eb5b81ca_8.2.13;"
            "device_name=Samsung%20SM-A5360;device_os=Android%2015;device_model=SM-A5360;"
            "package_name=com.ss.android.lark;package_brand=feishu;lark_version=8.2.6;"
            "channel=tengxun_1161_x64;tt_app_id=1161;"
        )
        h = {
            "Content-Type": "application/json;charset=utf-8",
            "X-Api-Version": API_VERSION,
            "X-App-Id": "1",
            "X-Device-Info": dev,
            "X-Front-Uuid": self.front_uuid,
            "X-Locale": self.locale,
            "X-Passport-Device-IDs": "",
            "X-Passport-Ka-Package": "0",
            "X-Passport-Uni-Did": self.device_id,
            "X-Passport-Unit": self.unit,
            "X-Request-ID": str(uuid.uuid4()),
            "X-Terminal-Type": "3",
            "x-tt-trace-log": "02",
        }
        if self.flow_key:
            h["X-Flow-Key"] = self.flow_key
        return h

    def call(self, method: str, path: str, body: Any = None,
             params: dict | None = None) -> dict:
        r = self.s.request(method, self.host + path, json=body, params=params,
                           headers=self._headers(), timeout=self.timeout)
        fk = r.headers.get("x-flow-key")
        if fk:
            self.flow_key = fk
            m = re.search(r"z00([a-z]+)", fk)
            if m and m.group(1) and m.group(1) != self.unit:
                self.unit = m.group(1)
        try:
            data = r.json()
        except Exception:
            data = {"_http": r.status_code, "_text": r.text[:2000]}
        data.setdefault("_http", r.status_code)
        data["_flow_key"] = self.flow_key
        data.setdefault("_set_cookie", [v for k, v in r.headers.items() if k.lower() == "set-cookie"])
        return data

    # ---------------- endpoints ----------------
    def credential(self, phone_or_email: str, action: int = ACTION_UG_REGISTER,
                   credential_type: int = 1) -> dict:
        return self.call("POST", "/accounts/login/credential", {
            "credential": fmt_phone(phone_or_email) if credential_type == 1 else phone_or_email,
            "credential_type": credential_type,
            "action": action,
            "use_package_domain": True,
            "from_source": 9,
            "support_qr_code": True,
        })

    def apply_code(self, flow_type: str = "login") -> dict:
        return self.call("POST", "/accounts/auth/code/apply",
                         {"code_type": 0, "flow_type": flow_type})

    def verify_code(self, code: str, flow_type: str = "login",
                    register_source: str = "") -> dict:
        return self.call("POST", "/accounts/auth/code/verify",
                         {"code": code, "flow_type": flow_type,
                          "register_source": register_source})

    def prepare_tenant(self) -> dict:
        return self.call("GET", "/accounts/register/prepare_tenant")

    def tenant_prepare(self, data: dict | None = None) -> dict:
        body = {"app_id": 1}
        body.update(data or {})
        return self.call("POST", "/accounts/tenant/prepare", body)

    def tenant_create(self, data: dict) -> dict:
        body = {"app_id": 1}
        body.update(data)
        return self.call("POST", "/accounts/tenant/create", body)

    def tenant_set_name(self, name: str, flow_type: str = "register",
                        opt_in: bool = True) -> dict:
        return self.call("POST", "/accounts/tenant/set_name",
                         {"app_id": 1, "opt_in": opt_in, "flow_type": flow_type, "name": name})

    def register_information(self, data: dict) -> dict:
        body = {"app_id": 1}
        body.update(data)
        body.setdefault("redirect_uri", "")
        return self.call("POST", "/accounts/tenant/information", body)

    # ---------------- driver ----------------
    def run_step(self, step: str, step_info: dict, prof: Profile) -> dict:
        si = step_info or {}
        if step == "prepare_tenant":
            return self.prepare_tenant()
        if step == "tenant_prepare":
            return self.tenant_prepare({
                "flow_type": si.get("flow_type") or prof.flow_type,
                "tenant_name": prof.tenant_name,
                "staff_size": prof.staff_size,
                "trusted_mail_in": prof.trusted_mail_in,
            })
        if step == "simple_register_entry":
            cb = si.get("callback_create") or {}
            csi = dict(cb.get("step_info") or {})
            csi.pop("pre_create_tenant_and_doubao", None)
            csi.setdefault("flow_type", "simple_register")
            return self.tenant_create(csi)
        if step in TEAM_STEPS:
            body = {
                "flow_type": si.get("flow_type") or prof.flow_type,
                "tenant_name": prof.tenant_name,
                "staff_size": prof.staff_size,
                "industry_type": prof.industry_type,
                "trusted_mail_in": prof.trusted_mail_in,
            }
            if prof.user_name:
                body["user_name"] = prof.user_name
            if prof.region_code:
                body["region_code"] = prof.region_code
            if prof.is_doubao:
                body["is_doubao"] = True
            # merge server hints (some steps require extra tokens)
            for k in ("callback_create", "pre_create_tenant_and_doubao", "invite_code"):
                if k in si:
                    body[k] = si[k]
            return self.tenant_create(body)
        if step in ("set_name", "dispatch_set_name"):
            name = prof.user_name or si.get("name") or si.get("user_name") or ""
            return self.tenant_set_name(name, flow_type=si.get("flow_type") or prof.flow_type)
        if step in ("set_personal_info", "enter_user_info", "user_info"):
            body = dict(si)
            if prof.user_name:
                body["user_name"] = prof.user_name
            body.setdefault("flow_type", prof.flow_type)
            return self.register_information(body)
        raise ValueError(f"unhandled next_step: {step!r}")

    def drive(self, resp: dict, prof: Profile, max_steps: int = 8,
              logger=None) -> tuple[dict, list[Step]]:
        """Follow next_step from a credential/verify response until done."""
        history: list[Step] = []
        for _ in range(max_steps):
            data = resp.get("data") or {}
            step = data.get("next_step")
            si = data.get("step_info") or {}
            history.append(Step(step, si, resp))
            if logger:
                logger(f"step={step} flow_key={self.flow_key}")
            if step in TERMINAL_STEPS or step in VERIFY_STEPS or step == "show_dialog":
                return resp, history
            try:
                resp = self.run_step(step, si, prof)
            except ValueError:
                return resp, history
        return resp, history


# ---------------- helpers ----------------
def is_banned(resp: dict) -> bool:
    txt = json.dumps(resp, ensure_ascii=False).lower()
    return "banned" in txt



def tenant_created(resp: dict) -> bool:
    """True when a tenant/create (or its callback) response produced a real tenant."""
    if resp.get("code", resp.get("biz_code", 0)) not in (0, None):
        return False
    d = resp.get("data") or {}
    if d.get("tenant_id") or d.get("tenant") or d.get("tenant_info"):
        return True
    step = str(d.get("next_step") or "")
    if step in ("ok", "enter_app", "finish", "done", "success", "close_all"):
        return True
    if step == "show_dialog":
        txt = json.dumps(d, ensure_ascii=False).lower()
        return ("target_tenant_id" in txt) or ("tenant_id" in txt)
    return False


def classify(resp: dict) -> str:
    if is_banned(resp):
        return "banned"
    decision = (resp.get("data") or {}).get("decision")
    if resp.get("code") == 4233 or (decision and "verify_center" in str(decision)):
        return "captcha"
    code = resp.get("code", resp.get("biz_code", 0))
    data = resp.get("data") or {}
    step = data.get("next_step")
    if code not in (0, None):
        return f"error:{code}"
    if step in VERIFY_STEPS:
        si = data.get("step_info") or {}
        vc = si.get("verify_code_mobile") or {}
        return "verify:" + (vc.get("flow_type") or "login")
    if step in TEAM_STEPS or step in ("prepare_tenant", "set_name", "set_personal_info"):
        return "register"
    return str(step or "ok")
# ---------------- CLI ----------------
def _main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Feishu passport probe / single-account driver")
    ap.add_argument("--phone", required=True)
    ap.add_argument("--stage", default="credential",
                    choices=["credential", "apply", "verify", "drive", "prepare"])
    ap.add_argument("--code", default="")
    ap.add_argument("--team-name", default="")
    ap.add_argument("--user-name", default="")
    ap.add_argument("--staff-size", default="MDES00000013")
    ap.add_argument("--industry", default="MDID00000157")
    ap.add_argument("--region", default="")
    ap.add_argument("--doubao", action="store_true")
    ap.add_argument("--action", type=int, default=ACTION_UG_REGISTER)
    ap.add_argument("--device-id", default="")
    ap.add_argument("--unit", default="eu_nc")
    ap.add_argument("--proxy", default="")
    a = ap.parse_args()

    p = Passport(device_id=a.device_id or None, unit=a.unit, proxy=a.proxy or None)
    if a.stage == "credential":
        out = p.credential(a.phone, action=a.action)
        print("class:", classify(out))
    elif a.stage == "apply":
        cred = p.credential(a.phone, action=a.action)
        print("credential:", classify(cred))
        out = p.apply_code()
    elif a.stage == "verify":
        out = p.verify_code(a.code)
        print("class:", classify(out))
    elif a.stage == "prepare":
        cred = p.credential(a.phone, action=a.action)
        print("credential:", classify(cred))
        out = p.prepare_tenant()
    else:
        cred = p.credential(a.phone, action=a.action)
        print("credential:", classify(cred))
        resp = cred
        if classify(cred).startswith("verify"):
            if not a.code:
                p.apply_code()
                print("SMS requested; rerun with --code or use batch_register.py")
                return 0
            resp = p.verify_code(a.code)
            print("verify:", classify(resp))
        prof = Profile(user_name=a.user_name, tenant_name=a.team_name,
                       staff_size=a.staff_size, industry_type=a.industry,
                       region_code=a.region, is_doubao=a.doubao)
        out, hist = p.drive(resp, prof, logger=lambda m: print("  " + m))
        print("steps:", [s.next_step for s in hist])
    print(json.dumps(out, ensure_ascii=False, indent=1)[:4000])
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
