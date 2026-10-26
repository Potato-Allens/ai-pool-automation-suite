# -*- coding: utf-8 -*-
"""RFC 6238 TOTP (Google Authenticator) + otpauth:// URI + QR helpers.

Stdlib only for the crypto; QR rendering uses segno when available and falls
back to a plain otpauth URI so enrolment still works without the dependency.
"""
import base64
import hashlib
import hmac
import secrets
import struct
import time
import urllib.parse


def new_secret(nbytes: int = 20) -> str:
    """Return a base32 secret without padding (what Google Authenticator expects)."""
    return base64.b32encode(secrets.token_bytes(nbytes)).decode("ascii").rstrip("=")


def _key(secret: str) -> bytes:
    s = (secret or "").strip().replace(" ", "").upper()
    pad = "=" * ((8 - len(s) % 8) % 8)
    return base64.b32decode(s + pad, casefold=True)


def hotp(secret: str, counter: int, digits: int = 6) -> str:
    mac = hmac.new(_key(secret), struct.pack(">Q", int(counter)), hashlib.sha1).digest()
    off = mac[-1] & 0x0F
    code = (struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code).zfill(digits)


def totp(secret: str, at: float = None, step: int = 30, digits: int = 6) -> str:
    at = time.time() if at is None else at
    return hotp(secret, int(at // step), digits)


def verify(secret: str, code: str, window: int = 1, step: int = 30, digits: int = 6) -> bool:
    """Constant-time check with +/- window steps of clock drift."""
    code = (code or "").strip().replace(" ", "")
    if not code.isdigit() or len(code) != digits or not secret:
        return False
    now = int(time.time() // step)
    ok = False
    for c in range(now - window, now + window + 1):
        try:
            cand = hotp(secret, c, digits)
        except Exception:
            return False
        if secrets.compare_digest(cand, code):
            ok = True  # keep looping so timing does not leak the offset
    return ok


def otpauth_uri(secret: str, account: str = "admin", issuer: str = "Pippit Pool",
                digits: int = 6, step: int = 30) -> str:
    label = urllib.parse.quote("%s:%s" % (issuer, account), safe=":")
    q = urllib.parse.urlencode({
        "secret": secret, "issuer": issuer, "algorithm": "SHA1",
        "digits": digits, "period": step,
    })
    return "otpauth://totp/%s?%s" % (label, q)


def qr_svg_data_uri(text: str, scale: int = 5) -> str:
    """Return data:image/svg+xml;base64,... or '' when segno is unavailable."""
    try:
        import segno  # type: ignore
    except Exception:
        return ""
    try:
        qr = segno.make(text, error="m")
        svg = qr.svg_inline(scale=scale, border=2, dark="#0b1020", light="#ffffff")
        return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")
    except Exception:
        return ""


if __name__ == "__main__":
    s = new_secret()
    print("secret:", s)
    print("uri:", otpauth_uri(s))
    print("now:", totp(s), "next:", totp(s, time.time() + 30))
    assert verify(s, totp(s))
    print("selftest ok")
