# -*- coding: utf-8 -*-
"""SQLite storage for the Pippit pool API (keys, jobs, ledger)."""
import hashlib, json, os, secrets, sqlite3, threading, time
HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "api.db")
_local = threading.local()

def conn():
    c = getattr(_local, "c", None)
    if c is None:
        c = sqlite3.connect(DB, timeout=30, isolation_level=None, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA busy_timeout=30000")
        _local.c = c
    return c

SCHEMA = """
CREATE TABLE IF NOT EXISTS keys(
  id TEXT PRIMARY KEY, name TEXT NOT NULL, prefix TEXT NOT NULL, key_hash TEXT UNIQUE NOT NULL,
  active INTEGER NOT NULL DEFAULT 1, quota_credits INTEGER NOT NULL DEFAULT 0,
  used_credits INTEGER NOT NULL DEFAULT 0, created_at REAL NOT NULL, last_used_at REAL
);
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, key_id TEXT NOT NULL, status TEXT NOT NULL, prompt TEXT NOT NULL,
  model TEXT NOT NULL, resolution TEXT NOT NULL, duration REAL NOT NULL, ratio TEXT NOT NULL,
  created_at REAL NOT NULL, started_at REAL, finished_at REAL,
  result_path TEXT, error TEXT, account_uid TEXT, tag TEXT,
  cost_credits INTEGER NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0, tried TEXT NOT NULL DEFAULT '[]',
  cancel INTEGER NOT NULL DEFAULT 0, worker TEXT
);
CREATE INDEX IF NOT EXISTS jobs_status_created ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS jobs_key ON jobs(key_id, created_at);
"""

def init():
    c = conn()
    c.executescript(SCHEMA)
    try:
        c.execute("ALTER TABLE jobs ADD COLUMN tried TEXT NOT NULL DEFAULT '[]'")
    except Exception:
        pass

def sha(tok):
    return hashlib.sha256(tok.encode("utf-8")).hexdigest()

def create_key(name, quota_credits=0):
    tok = "sk-pp-" + secrets.token_urlsafe(32)
    kid = "key_" + secrets.token_hex(8)
    conn().execute("INSERT INTO keys(id,name,prefix,key_hash,quota_credits,created_at) VALUES(?,?,?,?,?,?)",
                   (kid, name, tok[:14], sha(tok), int(quota_credits), time.time()))
    return {"id": kid, "name": name, "key": tok, "prefix": tok[:14], "quota_credits": int(quota_credits)}

def find_key(tok):
    if not tok: return None
    r = conn().execute("SELECT * FROM keys WHERE key_hash=? AND active=1", (sha(tok),)).fetchone()
    if not r: return None
    conn().execute("UPDATE keys SET last_used_at=? WHERE id=?", (time.time(), r["id"]))
    return dict(r)

def list_keys():
    return [dict(r) for r in conn().execute("SELECT id,name,prefix,active,quota_credits,used_credits,created_at,last_used_at FROM keys ORDER BY created_at DESC")]

def revoke_key(kid):
    cur = conn().execute("UPDATE keys SET active=0 WHERE id=?", (kid,))
    return cur.rowcount > 0

def add_job(jid, key_id, prompt, model, resolution, duration, ratio, cost):
    conn().execute("INSERT INTO jobs(id,key_id,status,prompt,model,resolution,duration,ratio,created_at,cost_credits) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (jid, key_id, "queued", prompt, model, resolution, float(duration), ratio, time.time(), int(cost)))

def get_job(jid):
    r = conn().execute("SELECT * FROM jobs WHERE id=?", (jid,)).fetchone()
    return dict(r) if r else None

def list_jobs(key_id=None, limit=50):
    if key_id:
        rows = conn().execute("SELECT * FROM jobs WHERE key_id=? ORDER BY created_at DESC LIMIT ?", (key_id, int(limit)))
    else:
        rows = conn().execute("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (int(limit),))
    return [dict(r) for r in rows]

def claim_job(worker_id):
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        r = c.execute("SELECT id FROM jobs WHERE status='queued' AND cancel=0 ORDER BY created_at LIMIT 1").fetchone()
        if not r:
            c.execute("COMMIT"); return None
        jid = r["id"]
        c.execute("UPDATE jobs SET status='running', started_at=?, worker=?, attempts=attempts+1, error=NULL WHERE id=? AND status='queued'",
                  (time.time(), worker_id, jid))
        c.execute("COMMIT")
        return jid
    except Exception:
        c.execute("ROLLBACK"); raise

def finish_job(jid, status, result_path=None, error=None, account_uid=None, tag=None, cost=0):
    conn().execute("UPDATE jobs SET status=?, finished_at=?, result_path=?, error=?, account_uid=?, tag=?, cost_credits=? WHERE id=?",
                   (status, time.time(), result_path, error, account_uid, tag, int(cost), jid))

def requeue_job(jid, error=None):
    conn().execute("UPDATE jobs SET status='queued', started_at=NULL, worker=NULL, error=? WHERE id=?", (error, jid))

def request_cancel(jid):
    conn().execute("UPDATE jobs SET cancel=1 WHERE id=? AND status='queued'", (jid,))
    conn().execute("UPDATE jobs SET cancel=1 WHERE id=? AND status='running'", (jid,))

def is_canceled(jid):
    r = conn().execute("SELECT cancel FROM jobs WHERE id=?", (jid,)).fetchone()
    return bool(r and r["cancel"])

def counts():
    rows = conn().execute("SELECT status, COUNT(*) n FROM jobs GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}

def bump_usage(key_id, credits):
    conn().execute("UPDATE keys SET used_credits=used_credits+? WHERE id=?", (int(credits), key_id))

def key_by_id(kid):
    r = conn().execute("SELECT * FROM keys WHERE id=?", (kid,)).fetchone()
    return dict(r) if r else None


def add_tried(jid, uid):
    r = conn().execute("SELECT tried FROM jobs WHERE id=?", (jid,)).fetchone()
    cur = []
    if r and r["tried"]:
        try: cur = json.loads(r["tried"])
        except Exception: cur = []
    if uid not in cur:
        cur.append(uid)
    conn().execute("UPDATE jobs SET tried=? WHERE id=?", (json.dumps(cur), jid))

# ---------------------------------------------------------------------------
# Admin console: TOTP enrolment state + revocable session cookies
# ---------------------------------------------------------------------------
ADMIN_SCHEMA = """
CREATE TABLE IF NOT EXISTS kv(
  k TEXT PRIMARY KEY, v TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS admin_sessions(
  tok_hash TEXT PRIMARY KEY, created_at REAL NOT NULL, expires_at REAL NOT NULL, ip TEXT
);
"""

def init_admin():
    c = conn()
    c.executescript(ADMIN_SCHEMA)

def kv_get(k, default=None):
    r = conn().execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default

def kv_set(k, v):
    conn().execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))

def kv_del(k):
    conn().execute("DELETE FROM kv WHERE k=?", (k,))

def admin_session_create(ttl=43200, ip=""):
    tok = secrets.token_urlsafe(32)
    now = time.time()
    conn().execute("INSERT INTO admin_sessions(tok_hash,created_at,expires_at,ip) VALUES(?,?,?,?)",
                   (sha(tok), now, now + float(ttl), ip))
    return tok

def admin_session_valid(tok):
    if not tok:
        return False
    r = conn().execute("SELECT expires_at FROM admin_sessions WHERE tok_hash=?", (sha(tok),)).fetchone()
    if not r:
        return False
    if r["expires_at"] < time.time():
        conn().execute("DELETE FROM admin_sessions WHERE tok_hash=?", (sha(tok),))
        return False
    return True

def admin_session_destroy(tok):
    if tok:
        conn().execute("DELETE FROM admin_sessions WHERE tok_hash=?", (sha(tok),))

def admin_sessions_clear():
    conn().execute("DELETE FROM admin_sessions")

def recent_jobs(limit=20):
    rows = conn().execute("SELECT id,status,model,prompt,created_at,finished_at,error,account_uid "
                          "FROM jobs ORDER BY created_at DESC LIMIT ?", (int(limit),))
    return [dict(r) for r in rows]

def kv_items(prefix=""):
    rows = conn().execute("SELECT k,v FROM kv WHERE k LIKE ? ORDER BY k", (prefix + "%",))
    return {r["k"]: r["v"] for r in rows}

def reap_stuck(timeout_s, status="failed"):
    """Fail jobs stuck in 'running' longer than timeout_s (worker died mid-render)."""
    now = time.time()
    cur = conn().execute(
        "UPDATE jobs SET status=?, finished_at=?, error=? "
        "WHERE status='running' AND started_at IS NOT NULL AND started_at < ?",
        (status, now, "worker timeout: no result in %d min" % (timeout_s // 60), now - float(timeout_s)))
    return cur.rowcount

def release_job(jid):
    """Put a claimed job back in the queue WITHOUT consuming an attempt.

    Used when the worker has no credit-worthy account right now: the job should
    wait for capacity instead of failing three times in a row.
    """
    conn().execute(
        "UPDATE jobs SET status='queued', started_at=NULL, worker=NULL, "
        "attempts=CASE WHEN attempts>0 THEN attempts-1 ELSE 0 END "
        "WHERE id=? AND status='running'", (jid,))