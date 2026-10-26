#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
doubao_video.py - 豆包视频能力工具包 (v1.0.0)

只读优先：quota / play / dl 不消耗生成额度；gen 默认 dry-run，只有显式 --send 才会提交。

子命令:
  quota                       查询今日剩余视频生成次数
  play  --vid VID             取播放直链(720p)
  dl    --vid VID... --out DIR 无水印下载(可断点/重试)
  dl    --url URL   --out DIR 直接下载直链
  gen   --prompt "..." ...     走 chat 通道提交生成(默认只打印 body)
  cookies                     检查 cookie 来源与可用性(不打印明文)

cookie 加载优先级:
  1. --cookies PATH            支持 flat JSON / list JSON / Cookie 头 / Netscape 文本
  2. 环境变量 DOUBAO_COOKIES_JSON (路径或 JSON 文本)
  3. 环境变量 DOUBAO_COOKIE      (原始 "a=b; c=d")
  4. Windows DPAPI 自动解密     (--from-browser / 默认自动尝试; 可用 --no-browser 关闭)
"""
from __future__ import annotations

import argparse
import base64
import copy
import ctypes
import ctypes.wintypes as wt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import unquote, urlparse

__version__ = "1.0.0"

try:
    import requests
except ImportError:  # pragma: no cover
    sys.stderr.write("缺少依赖 requests，请先执行: pip install requests\n")
    raise SystemExit(2)

BASE_URL = os.environ.get("DOUBAO_BASE_URL", "https://www.doubao.com").rstrip("/")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
DEFAULT_BOT_ID = "7338286299411103781"
VIDEO_ABILITY_TYPE = 17

EP_QUOTA = "/samantha/video/query_video_gen_info"
EP_PLAY = "/samantha/video/get_play_info"
EP_NOWM = "/creativity/resource/get_without_watermark"
EP_COMPLETION = "/samantha/chat/completion"

ERROR_HINTS = {
    "710010202": "系统错误。常见原因: vid 传成了字符串(必须是数组) / vid 无效 / 缺少登录 cookie。",
    "671000000": "unknown error。vid 不存在或无权限解析；请确认 vid 来自真实生成结果。",
    "710022002": "触发风控限流。停止请求，冷却 10-30 分钟，降低频率，不要并发。",
    "710020702": "参数或登录态异常。检查 cookie 是否过期、请求头是否完整。",
    "11902": "canvas workspace 不存在。gen 走 chat 通道时通常不需要 workspace。",
}


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def eprint(*a: Any, **kw: Any) -> None:
    print(*a, file=sys.stderr, **kw)


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def mask(value: str, keep: int = 3) -> str:
    s = str(value or "")
    if len(s) <= keep + 2:
        return "*" * len(s)
    return s[:keep] + "..." + s[-2:]


def sanitize_name(name: str) -> str:
    s = re.sub(r"[^A-Za-z0-9._-]+", "_", str(name or "")).strip("._-")
    return s[:120] or "download"


def human_size(n: Optional[int]) -> str:
    if n is None:
        return "?"
    f = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if f < 1024 or unit == "GB":
            return f"{f:.2f} {unit}" if unit != "B" else f"{int(f)} B"
        f /= 1024
    return f"{f:.2f} GB"


def sha256_file(path: Path, limit: Optional[int] = None) -> str:
    h = hashlib.sha256()
    left = limit
    with path.open("rb") as f:
        while True:
            n = 1024 * 1024 if left is None else min(1024 * 1024, left)
            if n <= 0:
                break
            b = f.read(n)
            if not b:
                break
            h.update(b)
            if left is not None:
                left -= len(b)
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# Cookie: 数据结构 / 解析 / DPAPI
# --------------------------------------------------------------------------- #
@dataclass
class Cookie:
    name: str
    value: str
    domain: str = ""
    path: str = "/"
    expires: float = 0.0
    source: str = ""

    def expired(self) -> bool:
        return bool(self.expires) and self.expires < time.time()


@dataclass
class CookieBundle:
    cookies: list[Cookie] = field(default_factory=list)
    source: str = ""

    def header(self, host: str = "www.doubao.com") -> str:
        out: list[str] = []
        seen: set[str] = set()
        for c in self.cookies:
            if not c.name or c.name in seen:
                continue
            if c.expired():
                continue
            if c.name.startswith("__") or c.name.startswith("LARK_") or c.name.startswith("feishu"):
                continue
            if c.domain:
                d = c.domain.lstrip(".").lower()
                if "doubao.com" not in d and "doubao" not in d:
                    continue
            seen.add(c.name)
            out.append(f"{c.name}={c.value}")
        return "; ".join(out)

    def doubao_cookies(self) -> list[Cookie]:
        res = []
        for c in self.cookies:
            if not c.domain or "doubao" in c.domain.lower():
                res.append(c)
        return res


def _cookie_from_dict(d: dict[str, Any], source: str = "") -> Optional[Cookie]:
    name = d.get("name") or d.get("Name") or d.get("key")
    if not name:
        return None
    value = d.get("value", d.get("Value", ""))
    exp = d.get("expirationDate", d.get("expires", d.get("expiry", 0))) or 0
    try:
        exp = float(exp)
    except Exception:
        exp = 0.0
    return Cookie(str(name), str(value), str(d.get("domain", d.get("host", "")) or ""),
                  str(d.get("path", "/") or "/"), exp, source)


def parse_cookie_text(text: str, source: str = "") -> list[Cookie]:
    text = (text or "").strip()
    if not text:
        return []
    # JSON
    if text[0] in "[{":
        try:
            obj = json.loads(text)
            return parse_cookie_json(obj, source)
        except Exception:
            pass
    # Netscape cookies.txt
    if "\t" in text and any(line.count("\t") >= 6 for line in text.splitlines()):
        out: list[Cookie] = []
        for line in text.splitlines():
            if not line.strip() or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) >= 7:
                out.append(Cookie(parts[5], parts[6], parts[0], parts[2], float(parts[4] or 0), source))
        if out:
            return out
    # raw header "a=b; c=d"
    out = []
    for part in text.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        out.append(Cookie(k.strip(), v.strip(), "", "/", 0, source))
    return out


def parse_cookie_json(obj: Any, source: str = "") -> list[Cookie]:
    out: list[Cookie] = []
    if isinstance(obj, dict):
        if "cookies" in obj and isinstance(obj["cookies"], list):
            for item in obj["cookies"]:
                if isinstance(item, dict):
                    c = _cookie_from_dict(item, source)
                    if c:
                        out.append(c)
            return out
        for k, v in obj.items():
            if isinstance(v, (str, int, float)):
                out.append(Cookie(str(k), str(v), "", "/", 0, source))
        return out
    if isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                c = _cookie_from_dict(item, source)
                if c:
                    out.append(c)
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                out.append(Cookie(str(item[0]), str(item[1]), "", "/", 0, source))
    return out


def load_cookie_file(path: Path) -> CookieBundle:
    raw = path.read_bytes()
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        text = raw.decode("utf-16", errors="replace")
    else:
        text = raw.decode("utf-8-sig", errors="replace")
    cookies = parse_cookie_text(text, str(path))
    if not cookies:
        raise SystemExit(f"无法从 {path} 解析出 cookie")
    return CookieBundle(cookies, f"file:{path}")


def _dpapi_unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("DPAPI 仅支持 Windows")

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    def blob(b: bytes) -> DATA_BLOB:
        buf = ctypes.create_string_buffer(b, len(b))
        return DATA_BLOB(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    blobin = blob(data)
    blobout = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
            ctypes.byref(blobin), None, None, None, None, 0, ctypes.byref(blobout)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(blobout.pbData, blobout.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blobout.pbData)


def _decrypt_v10(enc: bytes, key: bytes, host_key: str = "") -> str:
    """Chrome/豆包 v10/v11 cookie 解密。

    Chrome 130+ 在明文前追加 32 字节 SHA256(host_key)（host 绑定），
    若匹配则去掉；否则按原始明文处理。
    """
    if enc[:3] == b"v20":
        raise RuntimeError("v20 (App-Bound) cookie 无法离线解密，请改用 --cookies 导出或关闭浏览器绑定")
    if enc[:3] not in (b"v10", b"v11"):
        try:
            return enc.decode("utf-8")
        except Exception:
            return ""
    nonce, ct = enc[3:15], enc[15:]
    pt: bytes
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        pt = AESGCM(key).decrypt(nonce, ct, None)
    except ImportError:
        try:
            from Crypto.Cipher import AES
            pt = AES.new(key, AES.MODE_GCM, nonce=nonce).decrypt_and_verify(ct[:-16], ct[-16:])
        except Exception as e:
            raise RuntimeError(f"AES-GCM 解密失败: {e}") from e
    if host_key:
        h = hashlib.sha256(host_key.encode("utf-8")).digest()
        if pt.startswith(h):
            pt = pt[32:]
    return pt.decode("utf-8", errors="replace")

def _read_sqlite_cookies(db: Path) -> list[tuple[str, str, bytes, str, int]]:
    """返回 (host_key, name, encrypted_value, value, expires_utc)。优先 immutable 只读。"""
    tmpdir = None
    try:
        uri = "file:" + db.as_posix().replace("'", "%27") + "?mode=ro&immutable=1"
        con = sqlite3.connect(uri, uri=True, timeout=3)
        rows = con.execute(
            "select host_key,name,encrypted_value,value,expires_utc from cookies").fetchall()
        con.close()
        if rows:
            return rows
    except Exception:
        pass
    try:
        tmpdir = tempfile.mkdtemp(prefix="dbcookie_")
        dst = Path(tmpdir) / "Cookies"
        shutil.copy2(db, dst)
        for suffix in ("-wal", "-shm"):
            src = Path(str(db) + suffix)
            if src.exists():
                try:
                    shutil.copy2(src, Path(str(dst) + suffix))
                except Exception:
                    pass
        con = sqlite3.connect(str(dst), timeout=5)
        rows = con.execute(
            "select host_key,name,encrypted_value,value,expires_utc from cookies").fetchall()
        con.close()
        return rows
    finally:
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


def _candidate_browser_roots() -> list[Path]:
    roots: list[Path] = []
    la = os.environ.get("LOCALAPPDATA")
    if not la:
        return roots
    la = Path(la)
    roots += [
        la / "Doubao" / "User Data",
        la / "Google" / "Chrome" / "User Data",
        la / "Microsoft" / "Edge" / "User Data",
        la / "BraveSoftware" / "Brave-Browser" / "User Data",
    ]
    # 便携版/重定向环境兜底
    roots += [
        Path(r"<HOME>\AppData\Local\Doubao\User Data"),
        Path(r"<HOME>\AppData\Local\Google\Chrome\User Data"),
    ]
    out, seen = [], set()
    for r in roots:
        if r.exists() and str(r).lower() not in seen:
            seen.add(str(r).lower())
            out.append(r)
    return out


def _profile_cookie_dbs(root: Path) -> list[Path]:
    dbs: list[Path] = []
    candidates = [root]
    try:
        candidates += [p for p in root.iterdir() if p.is_dir()]
    except OSError:
        pass
    for p in candidates:
        for sub in ("Network/Cookies", "Cookies"):
            q = p / sub
            if q.exists():
                dbs.append(q)
    return dbs


def load_cookies_dpapi(profile: Optional[str] = None) -> CookieBundle:
    if os.name != "nt":
        raise RuntimeError("DPAPI 自动解密仅支持 Windows")
    roots = [Path(profile)] if profile else _candidate_browser_roots()
    if not roots:
        raise RuntimeError("未找到浏览器 User Data 目录，请用 --cookies 指定 cookie JSON")

    best: Optional[tuple[int, float, Path, list[tuple[str, str, bytes, str, int]], bytes]] = None
    for root in roots:
        state = root / "Local State"
        if not state.exists():
            # 允许 profile 直接指向包含 Local State 的目录
            if (root.parent / "Local State").exists():
                state = root.parent / "Local State"
            else:
                continue
        try:
            ls = json.loads(state.read_text(encoding="utf-8"))
            enc_key = base64.b64decode(ls["os_crypt"]["encrypted_key"])
            if enc_key[:5] != b"DPAPI":
                continue
            key = _dpapi_unprotect(enc_key[5:])
        except Exception as e:
            eprint(f"[cookie] 跳过 {state}: {e}")
            continue
        for db in _profile_cookie_dbs(root):
            try:
                rows = _read_sqlite_cookies(db)
            except Exception as e:
                if os.environ.get("DOUBAO_DEBUG"): eprint(f"[cookie] 读取失败 {db}: {e}")
                continue
            doubao = [r for r in rows if "doubao" in (r[0] or "").lower()]
            if not doubao:
                continue
            score = len(doubao)
            mtime = db.stat().st_mtime
            # 以 Cookies 库的更新时间为主：最新登录的 profile 通常就是当前账号
            if best is None or (mtime, score) > (best[1], best[0]):
                best = (score, mtime, db, doubao, key)

    if not best:
        raise RuntimeError("DPAPI 未找到豆包 cookie；请确认豆包客户端已登录，或改用 --cookies")

    score, _, db, rows, key = best
    cookies: list[Cookie] = []
    failed = 0
    for host, name, enc, plain, exp in rows:
        try:
            val = plain or _decrypt_v10(enc or b"", key, host)
        except Exception:
            failed += 1
            continue
        exp_s = 0.0
        if exp:
            # Chrome expires_utc: 1601-01-01 起的微秒
            exp_s = exp / 1_000_000 - 11644473600
        cookies.append(Cookie(name, val, host, "/", exp_s, str(db)))
    if not cookies:
        raise RuntimeError(f"DPAPI 解出 0 条 cookie（{db}），可能有 v20 绑定")
    src = f"dpapi:{db} (doubao={score}, failed={failed})"
    return CookieBundle(cookies, src)


def resolve_cookies(args: argparse.Namespace) -> CookieBundle:
    explicit = getattr(args, "cookies", None)
    if explicit:
        p = Path(explicit)
        if p.exists():
            return load_cookie_file(p)
        # 允许直接传 JSON / Cookie 头
        cs = parse_cookie_text(explicit, "cli")
        if cs:
            return CookieBundle(cs, "cli")

    env_json = os.environ.get("DOUBAO_COOKIES_JSON")
    if env_json:
        p = Path(env_json)
        if p.exists():
            return load_cookie_file(p)
        cs = parse_cookie_text(env_json, "env:DOUBAO_COOKIES_JSON")
        if cs:
            return CookieBundle(cs, "env:DOUBAO_COOKIES_JSON")

    env_raw = os.environ.get("DOUBAO_COOKIE")
    if env_raw:
        cs = parse_cookie_text(env_raw, "env:DOUBAO_COOKIE")
        if cs:
            return CookieBundle(cs, "env:DOUBAO_COOKIE")

    if not getattr(args, "no_browser", False):
        try:
            return load_cookies_dpapi(getattr(args, "profile", None))
        except Exception as e:
            raise SystemExit(
                "未能加载 cookie。\n"
                f"  DPAPI: {e}\n"
                "  请任选一种方式:\n"
                "    1) --cookies path\\to\\cookies.json\n"
                "    2) set DOUBAO_COOKIES_JSON=path\\to\\cookies.json\n"
                "    3) set DOUBAO_COOKIE=sessionid=...; ttwid=...\n"
                "    4) --profile \"C:\\...\\Doubao\\User Data\\Profile 16\" --from-browser")
    raise SystemExit("缺少 cookie：请用 --cookies / DOUBAO_COOKIES_JSON / DOUBAO_COOKIE 提供")

# --------------------------------------------------------------------------- #
# HTTP 客户端
# --------------------------------------------------------------------------- #
class DoubaoError(RuntimeError):
    def __init__(self, message: str, code: Optional[str] = None, payload: Any = None):
        super().__init__(message)
        self.code = code
        self.payload = payload


class Client:
    def __init__(self, bundle: CookieBundle, timeout: float = 30.0, retries: int = 3,
                 verbose: bool = False):
        self.bundle = bundle
        self.timeout = timeout
        self.retries = max(0, retries)
        self.verbose = verbose
        self.s = requests.Session()
        self.s.headers.update({
            "user-agent": UA,
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN,zh;q=0.9",
            "origin": BASE_URL,
            "referer": BASE_URL + "/chat/",
            "cookie": bundle.header(),
        })

    def _log(self, *a: Any) -> None:
        if self.verbose:
            eprint("[http]", *a)

    def post_json(self, path: str, payload: dict[str, Any], retries: Optional[int] = None) -> dict[str, Any]:
        url = path if path.startswith("http") else BASE_URL + path
        n = self.retries if retries is None else max(0, retries)
        last: Optional[Exception] = None
        for attempt in range(n + 1):
            try:
                r = self.s.post(url, json=payload, timeout=self.timeout)
                self._log("POST", url, r.status_code, f"{len(r.content)}B")
                if r.status_code in (429, 500, 502, 503, 504) and attempt < n:
                    wait = min(15, 1.6 ** attempt)
                    eprint(f"[retry] HTTP {r.status_code}，{wait:.1f}s 后重试 ({attempt + 1}/{n})")
                    time.sleep(wait)
                    continue
                if r.status_code >= 400:
                    raise DoubaoError(f"HTTP {r.status_code}: {r.text[:300]}")
                try:
                    j = r.json()
                except Exception:
                    raise DoubaoError(f"响应不是 JSON: {r.text[:300]}")
                code = j.get("code")
                if code not in (0, None):
                    code_s = str(code)
                    hint = ERROR_HINTS.get(code_s, "")
                    raise DoubaoError(
                        f"接口返回 code={code} msg={j.get('msg', '')}" + (f"\n提示: {hint}" if hint else ""),
                        code_s, j)
                return j
            except DoubaoError:
                raise
            except (requests.Timeout, requests.ConnectionError) as e:
                last = e
                if attempt >= n:
                    break
                wait = min(10, 1.5 ** attempt)
                eprint(f"[retry] 网络错误 {type(e).__name__}，{wait:.1f}s 后重试 ({attempt + 1}/{n})")
                time.sleep(wait)
        raise DoubaoError(f"请求失败: {last}")


# --------------------------------------------------------------------------- #
# 业务接口
# --------------------------------------------------------------------------- #
def api_quota(client: Client) -> dict[str, Any]:
    j = client.post_json(EP_QUOTA, {})
    return j.get("data") or {}


def api_play(client: Client, vid: str) -> dict[str, Any]:
    j = client.post_json(EP_PLAY, {"vid": vid})
    return j.get("data") or {}


def api_nowm(client: Client, vids: Iterable[str]) -> dict[str, Any]:
    vids = list(vids)
    if not vids:
        raise DoubaoError("vid 列表为空")
    j = client.post_json(EP_NOWM, {"vid": vids})
    return j.get("data") or {}


def extract_play_urls(data: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    infos = data.get("play_infos") or []
    if not infos and isinstance(data.get("play_info"), dict):
        infos = [data["play_info"]]
    for it in infos:
        if not isinstance(it, dict):
            continue
        d = str(it.get("definition") or "")
        if it.get("main"):
            out.append({"definition": d or "?", "url": it["main"], "role": "main"})
        if it.get("backup"):
            out.append({"definition": d or "?", "url": it["backup"], "role": "backup"})
    return out


# --------------------------------------------------------------------------- #
# 下载
# --------------------------------------------------------------------------- #
def _content_range_total(v: Optional[str]) -> Optional[int]:
    if not v:
        return None
    m = re.search(r"/(\d+)\s*$", v)
    return int(m.group(1)) if m else None


def download_file(url: str, dest: Path, *, retries: int = 3, timeout: float = 60.0,
                  resume: bool = True, force: bool = False, referer: str = BASE_URL + "/chat/") -> dict[str, Any]:
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_suffix(dest.suffix + ".part")

    if dest.exists() and dest.stat().st_size > 0 and not force:
        meta = {"path": str(dest), "size": dest.stat().st_size, "skipped": True,
                "sha256": sha256_file(dest)}
        print(f"[skip] 已存在 {dest} ({human_size(meta['size'])}), 用 --force 覆盖")
        return meta

    headers = {"user-agent": UA, "referer": referer, "accept": "*/*"}
    last_err: Optional[Exception] = None

    for attempt in range(max(0, retries) + 1):
        pos = part.stat().st_size if (resume and part.exists()) else 0
        h = dict(headers)
        if pos:
            h["range"] = f"bytes={pos}-"
        try:
            r = requests.get(url, headers=h, stream=True, timeout=(15, timeout))
            if r.status_code == 416:
                # 已下完
                if part.exists():
                    os.replace(part, dest)
                    break
            if r.status_code not in (200, 206):
                raise DoubaoError(f"下载 HTTP {r.status_code}: {r.text[:200]}")
            total = _content_range_total(r.headers.get("content-range"))
            if total is None:
                cl = r.headers.get("content-length")
                total = (pos + int(cl)) if (cl and r.status_code == 206) else (int(cl) if cl else None)
            mode = "ab" if (r.status_code == 206 and pos > 0) else "wb"
            if mode == "wb":
                pos = 0
            got = pos
            t0 = time.monotonic()
            last_pct = -1
            with part.open(mode) as f:
                for chunk in r.iter_content(chunk_size=256 * 1024):
                    if not chunk:
                        continue
                    f.write(chunk)
                    got += len(chunk)
                    if total:
                        pct = int(got * 100 / total)
                        if pct != last_pct and (pct % 5 == 0 or pct >= 99):
                            last_pct = pct
                            speed = got / max(1e-6, time.monotonic() - t0)
                            eprint(f"\r  {pct:3d}%  {human_size(got)}/{human_size(total)}  {human_size(int(speed))}/s",
                                   end="")
            eprint()
            if total and got < total:
                raise DoubaoError(f"下载不完整: {got}/{total} bytes")
            os.replace(part, dest)
            break
        except Exception as e:
            last_err = e
            if attempt >= retries:
                raise
            wait = min(20, 2 ** attempt)
            eprint(f"[retry] 下载失败 {type(e).__name__}: {e}; {wait}s 后重试 ({attempt + 1}/{retries})")
            time.sleep(wait)

    if not dest.exists():
        raise DoubaoError(f"下载失败: {last_err}")

    meta = {
        "path": str(dest),
        "size": dest.stat().st_size,
        "sha256": sha256_file(dest),
        "url": url,
        "downloaded_at": now_iso(),
    }
    print(f"[ok] {dest}  {human_size(meta['size'])}  sha256={meta['sha256'][:16]}...")
    return meta


def download_vids(client: Client, vids: list[str], out_dir: Path, *, force: bool = False,
                  retries: int = 3, timeout: float = 60.0) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    try:
        data = api_nowm(client, vids)
    except DoubaoError as e:
        eprint(f"[warn] 批量无水印接口失败: {e}；改为逐条尝试")
        data = {}
    dv = data.get("download_video") or {}
    for vid in vids:
        info = dv.get(vid) or dv.get(str(vid)) or {}
        url = info.get("download_url")
        watermark = "none" if data.get("without_watermark") else "unknown"
        if not url:
            try:
                single = api_nowm(client, [vid])
                info = (single.get("download_video") or {}).get(vid) or info
                url = info.get("download_url")
                if url:
                    watermark = "none" if single.get("without_watermark") else "unknown"
            except DoubaoError:
                pass
        if not url:
            eprint(f"[warn] {vid} 无水印接口未返回直链，回退 get_play_info（可能带水印）")
            try:
                p = api_play(client, vid)
                urls = extract_play_urls(p)
                if urls:
                    url = urls[0]["url"]
                    watermark = "unknown(fallback)"
            except DoubaoError as e:
                eprint(f"[warn] 回退失败: {e}")
        if not url:
            results.append({"vid": vid, "ok": False, "error": "no download url"})
            continue
        dest = out_dir / f"{sanitize_name(vid)}.mp4"
        try:
            meta = download_file(url, dest, retries=retries, timeout=timeout, force=force)
        except Exception as e:
            results.append({"vid": vid, "ok": False, "error": str(e)})
            continue
        rec = {
            "vid": vid, "ok": True, "path": str(dest), "size": meta.get("size"),
            "sha256": meta.get("sha256"), "duration": info.get("duration"),
            "video_type": info.get("video_type"), "watermark": watermark,
            "download_url": url, "downloaded_at": now_iso(),
        }
        side = dest.with_suffix(".json")
        side.write_text(json.dumps(rec, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[ok] {vid}  dur={info.get('duration', '?')}s  type={info.get('video_type', '?')}  wm={watermark}")
        results.append(rec)
    return results


# --------------------------------------------------------------------------- #
# gen: 拼 body（默认 dry-run）
# --------------------------------------------------------------------------- #
def find_body_template(explicit: Optional[str]) -> Path:
    if explicit:
        p = Path(explicit)
        if not p.exists():
            raise SystemExit(f"模板不存在: {p}")
        return p
    env = os.environ.get("DOUBAO_BODY_TEMPLATE")
    if env and Path(env).exists():
        return Path(env)
    here = Path(__file__).resolve().parent
    seeds = [Path.cwd(), here, *here.parents, *Path.cwd().parents]
    cands: list[Path] = []
    for base in seeds[:12]:
        cands += [base / "work" / "captured_body.json", base / "captured_body.json",
                  base / "template_body.json", base / "doubao-video-kit" / "template_body.json"]
    for c in cands:
        if c.exists():
            return c
    raise SystemExit(
        "找不到 body 模板 captured_body.json。\n"
        "  用 --template 指定，或设置 DOUBAO_BODY_TEMPLATE，\n"
        "  或在工作目录的 work/captured_body.json 放置一份 chat/completion 请求模板。")


def build_gen_body(template: dict[str, Any], *, prompt: str, duration: int, ratio: str,
                   model: str, resolution: str, camera: Optional[str] = None,
                   conversation_id: str = "", bot_id: str = DEFAULT_BOT_ID,
                   local_conversation_id: str = "") -> dict[str, Any]:
    body = copy.deepcopy(template)
    body.setdefault("messages", [])
    if not body["messages"]:
        body["messages"] = [{}]
    msg = body["messages"][0]

    text = f"生成视频：{prompt}"
    msg["local_message_id"] = str(uuid.uuid4())
    msg["content_block"] = [{
        "block_type": 10000,
        "content": {
            "text_block": {"text": text, "icon_url": "", "icon_url_dark": "", "summary": ""},
            "pc_event_block": "",
        },
        "block_id": str(uuid.uuid4()),
        "parent_id": "",
        "meta_info": [],
        "extra_info": {},
        "append_fields": [],
    }]
    msg["message_status"] = 0

    cm = body.setdefault("client_meta", {})
    cm["local_conversation_id"] = local_conversation_id or f"local_{uuid.uuid4().hex[:16]}"
    cm["conversation_id"] = conversation_id or ""
    cm["bot_id"] = str(bot_id)
    cm.setdefault("last_section_id", "")
    cm["last_message_index"] = None
    cm.setdefault("local_permissions", [])

    opt = body.setdefault("option", {})
    opt["unique_key"] = str(uuid.uuid4())
    opt["create_time_ms"] = int(time.time() * 1000)
    opt["need_create_conversation"] = not bool(conversation_id)
    opt["conversation_init_option"] = {"need_ack_conversation": True}
    opt["is_regen"] = False
    opt["from_suggest"] = False
    opt["answer_with_suggest"] = False
    opt["message_from"] = 0
    opt["start_seq"] = 0

    ability_params: dict[str, Any] = {
        "ratio": ratio,
        "model": model,
        "duration": int(duration),
        "resolution": resolution,
        "input_box_content": {
            "user_input_content": prompt,
            "reply_message_format": "生成视频：%s",
        },
    }
    if camera:
        ability_params["camera_movement"] = camera

    body["chat_ability"] = json.dumps(
        {
            "ability_type": VIDEO_ABILITY_TYPE,
            "ability_param": json.dumps(ability_params, ensure_ascii=False, separators=(",", ":")),
        },
        ensure_ascii=False, separators=(",", ":"))

    ext = body.setdefault("ext", {})
    ext["answer_with_suggest"] = "0"
    ext["agent_mode"] = ext.get("agent_mode", "2")
    ext["is_finish"] = "1"
    body.setdefault("user_context", [])
    return body


def submit_gen(client: Client, body: dict[str, Any], *, wait_seconds: int = 80,
               sse_out: Optional[Path] = None) -> dict[str, Any]:
    url = BASE_URL + EP_COMPLETION
    headers = {
        "accept": "text/event-stream",
        "content-type": "application/json",
        "cache-control": "no-cache",
    }
    eprint("[send] 正在提交 /samantha/chat/completion ...")
    r = client.s.post(url, headers=headers, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                      stream=True, timeout=(20, 120))
    if r.status_code >= 400:
        raise DoubaoError(f"HTTP {r.status_code}: {r.text[:400]}")
    found: dict[str, Any] = {"vids": [], "task_ids": [], "events": []}
    t0 = time.monotonic()
    raw_lines: list[str] = []
    for line in r.iter_lines(decode_unicode=True):
        if line is None:
            continue
        raw_lines.append(line)
        if line:
            print(line[:1000])
            for v in re.findall(r"v0[0-9a-z]{20,}", line):
                if v not in found["vids"]:
                    found["vids"].append(v)
            for t in re.findall(r'"(?:task_id|creation_task_id)"\s*:\s*"?([0-9A-Za-z_-]{6,})', line):
                if t not in found["task_ids"]:
                    found["task_ids"].append(t)
            if '"event_type"' in line or "event:" in line:
                found["events"].append(line[:300])
        if time.monotonic() - t0 > wait_seconds:
            eprint(f"[send] 已读取 {wait_seconds}s，停止等待（任务可能仍在服务端生成）")
            break
    if sse_out:
        sse_out.parent.mkdir(parents=True, exist_ok=True)
        sse_out.write_text("\n".join(raw_lines), encoding="utf-8")
        eprint(f"[send] SSE 原始输出: {sse_out}")
    return found


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--cookies", help="cookie 文件或 JSON/原始 Cookie 头")
    p.add_argument("--from-browser", action="store_true", help="强制使用 DPAPI 从浏览器/客户端读取")
    p.add_argument("--profile", help="浏览器 User Data 或 Profile 目录（配合 DPAPI）")
    p.add_argument("--no-browser", action="store_true", help="禁用 DPAPI 自动读取")
    p.add_argument("--timeout", type=float, default=30.0, help="HTTP 超时秒数 (默认 30)")
    p.add_argument("--retries", type=int, default=3, help="网络重试次数 (默认 3)")
    p.add_argument("--json", action="store_true", help="输出原始 JSON")
    p.add_argument("-v", "--verbose", action="store_true")


def make_client(args: argparse.Namespace) -> Client:
    bundle = resolve_cookies(args)
    if getattr(args, "verbose", False):
        eprint(f"[cookie] 来源={bundle.source} 条数={len(bundle.cookies)} "
               f"doubao={len(bundle.doubao_cookies())}")
    if not bundle.header():
        raise SystemExit("cookie 为空，无法请求")
    return Client(bundle, timeout=args.timeout, retries=args.retries, verbose=args.verbose)


def cmd_quota(args: argparse.Namespace) -> int:
    c = make_client(args)
    d = api_quota(c)
    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
        return 0
    remain = d.get("remain_count")
    print(f"剩余次数     : {remain}")
    print(f"有生成中任务 : {'是' if d.get('has_generating_task') else '否'}")
    print(f"内测用户     : {'是' if d.get('is_beta_user') else '否'}")
    if isinstance(remain, int):
        print(f"今日可用产能 : {remain} 条 x 10s = {remain * 10}s（5s 与 10s 同价，建议一律 10s）")
    return 0


def cmd_play(args: argparse.Namespace) -> int:
    c = make_client(args)
    d = api_play(c, args.vid)
    urls = extract_play_urls(d)
    if args.json:
        print(json.dumps(d, ensure_ascii=False, indent=2))
        return 0
    if not urls:
        eprint("没有解析到播放直链")
        return 3
    print(f"vid={args.vid}  view_count={d.get('view_count', '?')}")
    for u in urls:
        print(f"[{u['role']:6s}] {u['definition']:>5s}  {u['url']}")
    return 0


def cmd_dl(args: argparse.Namespace) -> int:
    c = make_client(args)
    out_dir = Path(args.out or "downloads")
    out_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []

    if args.url:
        for url in args.url:
            name = args.name if args.name and len(args.url) == 1 else ""
            if not name:
                path = unquote(urlparse(url).path)
                base = Path(path).name
                name = base if base.lower().endswith((".mp4", ".mov", ".webm")) else \
                    f"url_{hashlib.sha1(url.encode()).hexdigest()[:10]}.mp4"
            dest = out_dir / sanitize_name(name)
            try:
                meta = download_file(url, dest, retries=args.retries, timeout=max(60.0, args.timeout),
                                     force=args.force)
                results.append({"ok": True, "path": str(dest), **meta})
            except Exception as e:
                results.append({"ok": False, "url": url, "error": str(e)})
    elif args.vid:
        vids = [v.strip() for v in args.vid if v and v.strip()]
        results = download_vids(c, vids, out_dir, force=args.force,
                                retries=args.retries, timeout=max(60.0, args.timeout))
    else:
        eprint("请提供 --vid 或 --url")
        return 2

    ok = sum(1 for r in results if r.get("ok"))
    print(f"完成: {ok}/{len(results)}")
    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if ok == len(results) and ok else 4


def cmd_gen(args: argparse.Namespace) -> int:
    template = json.loads(find_body_template(args.template).read_text(encoding="utf-8-sig"))
    body = build_gen_body(
        template, prompt=args.prompt, duration=args.duration, ratio=args.ratio,
        model=args.model, resolution=args.resolution, camera=args.camera,
        conversation_id=args.conversation_id or "", bot_id=args.bot_id,
        local_conversation_id=args.local_conversation_id or "")
    payload = json.dumps(body, ensure_ascii=False, indent=2)

    if args.save_body:
        p = Path(args.save_body)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(body, ensure_ascii=False, indent=2), encoding="utf-8")
        eprint(f"[body] 已写入 {p}")

    if args.dry_run or not args.send:
        eprint("[dry-run] 未发送任何请求；确认无误后加 --send 才会真正消耗 1 次额度")
        print(payload)
        return 0

    c = make_client(args)
    q = api_quota(c)
    remain = q.get("remain_count")
    if q.get("has_generating_task"):
        raise SystemExit("当前有视频正在生成，串行限制：等 has_generating_task=false 再提交")
    if isinstance(remain, int) and remain <= 0:
        raise SystemExit("今日额度已用完（明天刷新，不累计）")
    eprint(f"[send] 本次将消耗 1 次额度，当前剩余 {remain}; 模型={args.model} "
           f"{args.duration}s {args.ratio} {args.resolution}")
    if not args.yes and sys.stdin.isatty():
        if input("确认发送? 输入 y 继续: ").strip().lower() not in ("y", "yes"):
            eprint("已取消")
            return 1
    sse_out = Path(args.sse_out) if args.sse_out else None
    found = submit_gen(c, body, wait_seconds=args.wait, sse_out=sse_out)
    print(json.dumps(found, ensure_ascii=False, indent=2))
    if found["vids"]:
        eprint(f"[send] 捕获到 vid: {', '.join(found['vids'])}")
        eprint(f"[next] python {Path(__file__).name} dl --vid {' '.join(found['vids'])} --out {args.out or 'downloads'}")
    else:
        eprint("[send] 本次 SSE 未捕获 vid；用 quota 查看 has_generating_task，或在客户端/会话记录里取 vid")
    return 0


def cmd_cookies(args: argparse.Namespace) -> int:
    b = resolve_cookies(args)
    items = b.doubao_cookies()
    print(f"来源: {b.source}")
    print(f"总数: {len(b.cookies)}  豆包相关: {len(items)}  可发送: {len(b.header().split('; ')) if b.header() else 0}")
    keynames = {"sessionid", "sessionid_ss", "sid_guard", "sid_tt", "ttwid", "odin_tt", "uid_tt", "multi_sids"}
    for c in items:
        mark = " *key*" if c.name in keynames else ""
        print(f"  {c.name:<28s} {mask(c.value):<12s} domain={c.domain or '-'}{mark}")
    if args.json:
        print(json.dumps([{"name": c.name, "domain": c.domain, "expired": c.expired()}
                          for c in items], ensure_ascii=False, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="doubao_video.py",
        description="豆包视频能力工具包：查额度 / 取播放直链 / 无水印下载 / 生成(默认 dry-run)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  python doubao_video.py quota --cookies work\\cookies.json\n"
            "  python doubao_video.py play --vid v0db5eg... --cookies work\\cookies.json\n"
            "  python doubao_video.py dl --vid v0db5eg... --out downloads --cookies work\\cookies.json\n"
            "  python doubao_video.py dl --url \"https://v26-vdl.doubao.com/...\" --out downloads\n"
            "  python doubao_video.py gen --prompt \"柯基在客厅跳舞\" --duration 10 --ratio 9:16\n"
            "  python doubao_video.py gen --prompt \"...\" --send --yes\n"
        ))
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("quota", help="查询剩余生成次数（只读）")
    add_common(p); p.set_defaults(func=cmd_quota)

    p = sub.add_parser("play", help="取播放直链（只读）")
    add_common(p); p.add_argument("--vid", required=True); p.set_defaults(func=cmd_play)

    p = sub.add_parser("dl", help="无水印下载 / 直链下载")
    add_common(p)
    p.add_argument("--vid", nargs="+", help="一个或多个 vid")
    p.add_argument("--url", nargs="+", help="一个或多个直链")
    p.add_argument("--out", default="downloads", help="输出目录 (默认 downloads)")
    p.add_argument("--name", help="--url 单条时的文件名")
    p.add_argument("--force", action="store_true", help="已存在也重新下载")
    p.set_defaults(func=cmd_dl)

    p = sub.add_parser("gen", help="提交生成（默认 dry-run）")
    add_common(p)
    p.add_argument("--prompt", required=True)
    p.add_argument("--duration", type=int, choices=[5, 10], default=10)
    p.add_argument("--ratio", default="9:16", choices=["1:1", "3:4", "4:3", "9:16", "16:9", "21:9"])
    p.add_argument("--model", default="seedance_v2.0", choices=["seedance_v2.0", "seedance_v2.0_mini"])
    p.add_argument("--resolution", default="720p", choices=["720p"])
    p.add_argument("--camera", choices=["fixed"], help="固定镜头")
    p.add_argument("--conversation-id", dest="conversation_id", default="")
    p.add_argument("--bot-id", dest="bot_id", default=DEFAULT_BOT_ID)
    p.add_argument("--local-conversation-id", dest="local_conversation_id", default="")
    p.add_argument("--template", help="chat/completion body 模板 (默认自动找 work/captured_body.json)")
    p.add_argument("--save-body", help="把最终 body 写入文件")
    p.add_argument("--send", action="store_true", help="真的发送（会消耗 1 次额度）")
    p.add_argument("--dry-run", action="store_true", help="只打印 body，不发送（默认行为）")
    p.add_argument("--yes", action="store_true", help="跳过交互确认")
    p.add_argument("--sse-out", help="保存 SSE 原始输出")
    p.set_defaults(func=cmd_gen)

    p = sub.add_parser("cookies", help="检查 cookie 加载（不打印明文）")
    add_common(p); p.set_defaults(func=cmd_cookies)
    return ap


def main(argv: Optional[list[str]] = None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except DoubaoError as e:
        eprint(f"[error] {e}")
        return 3
    except KeyboardInterrupt:
        eprint("\n[abort] 用户中断")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
