# -*- coding: utf-8 -*-
"""SMS Gate - configuration."""
from __future__ import annotations
import os, secrets
from pathlib import Path

ROOT = Path(os.environ.get("SMSGATE_ROOT", "/opt/smsgate")).resolve()
DATA_DIR = Path(os.environ.get("SMSGATE_DATA", str(ROOT / "data"))).resolve()
DB_PATH = Path(os.environ.get("SMSGATE_DB", str(DATA_DIR / "app.db"))).resolve()
LOG_DIR = Path(os.environ.get("SMSGATE_LOGS", str(ROOT / "logs"))).resolve()
TEMPLATE_DIR = ROOT / "app" / "templates"
STATIC_DIR = ROOT / "app" / "static"
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

HOST = os.environ.get("SMSGATE_HOST", "127.0.0.1")
PORT = int(os.environ.get("SMSGATE_PORT", "8861"))

SITE_NAME = os.environ.get("SMSGATE_NAME", "SMS Gate")
CARD_COOKIE = "sg_card"
ADMIN_COOKIE = "sg_admin"
CARD_SESSION_TTL = int(os.environ.get("SMSGATE_CARD_TTL", str(2 * 3600)))
ADMIN_SESSION_TTL = int(os.environ.get("SMSGATE_ADMIN_TTL", str(8 * 3600)))
TRUSTED_PROXY = os.environ.get("SMSGATE_TRUST_PROXY", "127.0.0.1")

POLL_INTERVAL = int(os.environ.get("SMSGATE_POLL_INTERVAL", "15"))
NUMBER_TTL_MIN = int(os.environ.get("SMSGATE_NUMBER_TTL", "20"))


def _load_or_create(name: str, nbytes: int = 32) -> str:
    p = DATA_DIR / name
    try:
        s = p.read_text(encoding="utf-8").strip()
        if s:
            return s
    except Exception:
        pass
    s = secrets.token_hex(nbytes)
    p.write_text(s, encoding="utf-8", newline="\n")
    try:
        os.chmod(p, 0o600)
    except Exception:
        pass
    return s


SECRET = _load_or_create("secret.key")
FERNET_KEY = _load_or_create("fernet.key")
SETUP_TOKEN = _load_or_create("setup.token", 16)