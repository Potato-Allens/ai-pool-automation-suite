# -*- coding: utf-8 -*-
"""db.py - SQLite persistence for the Pippit account pool / video API."""
import hashlib, json, os, secrets, sqlite3, threading, time, uuid

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
os.makedirs(DATA, exist_ok=True)
DB_PATH = os.path.join(DATA, "pool.db")
LOCK = threading.RLock()
_conn = None


def conn():
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=60)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.execute("PRAGMA synchronous=NORMAL")
    return _conn


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
  uid TEXT PRIMARY KEY,
  tag TEXT,
  cookies TEXT NOT NULL,
  ports TEXT NOT NULL,
  workspace_id TEXT,
  space_id TEXT,
  credit INTEGER DEFAULT 0,
  status TEXT DEFAULT 'ready',
  cooldown_until REAL DEFAULT 0,
  last_error TEXT,
  jobs_done INTEGER DEFAULT 0,
  jobs_failed INTEGER DEFAULT 0,
  last_used REAL DEFAULT 0,
  created_at REAL
);
CREATE TABLE IF NOT EXISTS api_keys (
  id TEXT PRIMARY KEY,
  key_hash TEXT UNIQUE NOT NULL,
  prefix TEXT,
  name TEXT,
  created_at REAL,
  expires_at REAL,
  quota INTEGER DEFAULT 0,
  used INTEGER DEFAULT 0,
  active INTEGER DEFAULT 1
);
CREATE TABLE IF NOT EXISTS jobs (
  id TEXT PRIMARY KEY,
  key_id TEXT,
  prompt TEXT,
  model TEXT,
  seconds INTEGER,
  resolution TEXT,
  ratio TEXT,
  status TEXT DEFAULT 'queued',
  progress INTEGER DEFAULT 0,
  stage TEXT,
  account_uid TEXT,
  thread_id TEXT,
  run_id TEXT,
  asset_id TEXT,
  video_path TEXT,
  remote_url TEXT,
  error TEXT,
  created_at REAL,
  started_at REAL,
  completed_at REAL,
  cost INTEGER,
  attempts INTEGER DEFAULT 0,
  retry_after REAL DEFAULT 0,
  width INTEGER,
  height INTEGER,
  duration REAL,
  bytes INTEGER
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  job_id TEXT,
  ts REAL,
  msg TEXT
);
CREATE TABLE IF NOT EXISTS settings (
  k TEXT PRIMARY KEY,
  v TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_key ON jobs(key_id, created_at);
"""


def init():
    with LOCK:
        c = conn()
        c.executescript(SCHEMA)
        for col, typ in (("retry_after","REAL DEFAULT 0"),("width","INTEGER"),("height","INTEGER"),("duration","REAL"),("bytes","INTEGER"),
                         ("ref_image","TEXT"),("ref_video","TEXT"),("ref_audio","TEXT"),
                         ("media_deleted_at","REAL")):
            try:
                c.execute("ALTER TABLE jobs ADD COLUMN %s %s" % (col, typ))
            except Exception:
                pass
        c.commit()


def execute(sql, args=()):
    with LOCK:
        c = conn()
        cur = c.execute(sql, args)
        c.commit()
        return cur


def query(sql, args=()):
    with LOCK:
        return [dict(r) for r in conn().execute(sql, args).fetchall()]


def queryone(sql, args=()):
    with LOCK:
        r = conn().execute(sql, args).fetchone()
        return dict(r) if r else None


def now():
    return time.time()


# ---------------- keys ----------------
def _hash_key(raw):
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_key(name="default", quota=0, expires_in_days=None):
    kid = "key_" + secrets.token_hex(8)
    raw = "sk-pp-" + secrets.token_urlsafe(32)
    exp = now() + expires_in_days * 86400 if expires_in_days else None
    execute("INSERT INTO api_keys(id,key_hash,prefix,name,created_at,expires_at,quota,used,active) "
            "VALUES(?,?,?,?,?,?,?,0,1)",
            (kid, _hash_key(raw), raw[:12], name, now(), exp, int(quota or 0)))
    return kid, raw


def verify_key(raw):
    if not raw:
        return None
    row = queryone("SELECT * FROM api_keys WHERE key_hash=? AND active=1", (_hash_key(raw),))
    if not row:
        return None
    if row.get("expires_at") and row["expires_at"] < now():
        return None
    return row


def list_keys():
    return query("SELECT id,prefix,name,created_at,expires_at,quota,used,active FROM api_keys ORDER BY created_at DESC")


def revoke_key(kid):
    execute("UPDATE api_keys SET active=0 WHERE id=?", (kid,))
    return True


def bump_key_usage(kid):
    execute("UPDATE api_keys SET used=used+1 WHERE id=?", (kid,))


def running_for_key(kid):
    r = queryone("SELECT COUNT(*) n FROM jobs WHERE key_id=? AND status IN ('queued','running')", (kid,))
    return int(r["n"]) if r else 0


# ---------------- accounts ----------------
def upsert_account(uid, tag, cookies, ports, workspace_id=None, space_id=None,
                   credit=0, status="ready", last_error=None):
    uid = str(uid)
    old = queryone("SELECT uid FROM accounts WHERE uid=?", (uid,))
    if old:
        execute("UPDATE accounts SET tag=?,cookies=?,ports=?,workspace_id=COALESCE(?,workspace_id),"
                "space_id=COALESCE(?,space_id),credit=?,status=?,last_error=? WHERE uid=?",
                (tag, json.dumps(cookies, ensure_ascii=False), json.dumps(ports),
                 workspace_id, space_id, int(credit), status, last_error, uid))
    else:
        execute("INSERT INTO accounts(uid,tag,cookies,ports,workspace_id,space_id,credit,status,"
                "cooldown_until,jobs_done,jobs_failed,last_used,created_at) VALUES(?,?,?,?,?,?,?,?,0,0,0,0,?)",
                (uid, tag, json.dumps(cookies, ensure_ascii=False), json.dumps(ports),
                 workspace_id, space_id, int(credit), status, now()))
    return uid


def list_accounts():
    rows = query("SELECT * FROM accounts ORDER BY credit DESC, last_used ASC")
    for r in rows:
        r["ports"] = json.loads(r["ports"] or "[]")
        try:
            r["cookies"] = json.loads(r["cookies"] or "{}")
        except Exception:
            r["cookies"] = {}
    return rows


def pick_account(min_credit):
    """Atomically pick a ready account with enough credit; mark it busy."""
    with LOCK:
        c = conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            r = c.execute("SELECT * FROM accounts WHERE status='ready' AND credit>=? "
                          "AND cooldown_until<=? ORDER BY credit DESC, last_used ASC LIMIT 1",
                          (int(min_credit), now())).fetchone()
            if not r:
                c.commit()
                return None
            c.execute("UPDATE accounts SET status='busy' WHERE uid=?", (r["uid"],))
            c.commit()
            d = dict(r)
            d["ports"] = json.loads(d["ports"] or "[]")
            d["cookies"] = json.loads(d["cookies"] or "{}")
            return d
        except Exception:
            c.rollback()
            raise


def release_account(uid, credit=None, status="ready", cooldown=0, error=None, done=False, failed=False):
    sets = ["status=?", "cooldown_until=?", "last_used=?"]
    args = [status, now() + float(cooldown or 0), now()]
    if credit is not None:
        sets.append("credit=?")
        args.append(int(credit))
    if error is not None:
        sets.append("last_error=?")
        args.append(str(error)[:500])
    if done:
        sets.append("jobs_done=jobs_done+1")
    if failed:
        sets.append("jobs_failed=jobs_failed+1")
    args.append(str(uid))
    execute("UPDATE accounts SET %s WHERE uid=?" % ",".join(sets), tuple(args))


def set_account_credit(uid, credit, status=None):
    if status:
        execute("UPDATE accounts SET credit=?,status=? WHERE uid=?", (int(credit), status, str(uid)))
    else:
        execute("UPDATE accounts SET credit=? WHERE uid=?", (int(credit), str(uid)))


# ---------------- jobs ----------------
def create_job(key_id, prompt, model, seconds, resolution, ratio, cost, refs=None):
    refs = refs or {}
    jid = "video_" + secrets.token_hex(12)
    execute("INSERT INTO jobs(id,key_id,prompt,model,seconds,resolution,ratio,status,progress,stage,"
            "created_at,cost,attempts,ref_image,ref_video,ref_audio) "
            "VALUES(?,?,?,?,?,?,?, 'queued',0,'queued',?,?,0,?,?,?)",
            (jid, key_id, prompt, model, int(seconds), resolution, ratio, now(), int(cost),
             refs.get("image") or None, refs.get("video") or None, refs.get("audio") or None))
    return jid


def get_job(jid):
    return queryone("SELECT * FROM jobs WHERE id=?", (jid,))


def list_jobs(key_id=None, limit=50):
    if key_id:
        return query("SELECT * FROM jobs WHERE key_id=? ORDER BY created_at DESC LIMIT ?", (key_id, int(limit)))
    return query("SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (int(limit),))


def next_queued():
    with LOCK:
        c = conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            r = c.execute("SELECT * FROM jobs WHERE status='queued' AND retry_after<=? ORDER BY created_at ASC LIMIT 1", (now(),)).fetchone()
            if not r:
                c.commit()
                return None
            c.execute("UPDATE jobs SET status='running', started_at=? WHERE id=?", (now(), r["id"]))
            c.commit()
            return dict(r)
        except Exception:
            c.rollback()
            raise


def update_job(jid, **kw):
    if not kw:
        return
    keys = list(kw.keys())
    sql = "UPDATE jobs SET %s WHERE id=?" % ",".join("%s=?" % k for k in keys)
    execute(sql, tuple(kw[k] for k in keys) + (jid,))


def requeue_job(jid, error=None, max_attempts=3, delay=0):
    j = get_job(jid)
    if not j:
        return False
    attempts = int(j.get("attempts") or 0) + 1
    if attempts >= max_attempts:
        update_job(jid, status="failed", error=str(error or "max attempts")[:2000],
                   completed_at=now(), attempts=attempts)
        return False
    update_job(jid, status="queued", attempts=attempts, error=str(error or "")[:2000],
               stage="requeued", progress=0, account_uid=None, retry_after=now()+float(delay or 0))
    return True


def add_event(jid, msg):
    execute("INSERT INTO events(job_id,ts,msg) VALUES(?,?,?)", (jid, now(), str(msg)[:1000]))


def events(jid, limit=200):
    return query("SELECT * FROM events WHERE job_id=? ORDER BY id DESC LIMIT ?", (jid, int(limit)))

# ---------------- settings ----------------
def get_setting(k, default=None):
    r = queryone("SELECT v FROM settings WHERE k=?", (str(k),))
    return r["v"] if r else default


def set_setting(k, v):
    execute("INSERT INTO settings(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
            (str(k), str(v)))
    return True
