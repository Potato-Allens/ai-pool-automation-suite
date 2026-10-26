# -*- coding: utf-8 -*-
"""Account pool: load pp_acc_*.json, atomic locks shared with the production lines."""
import glob, io, json, os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
C = r"<PROJECT_ROOT>"
sys.path.insert(0, os.path.join(C, "work"))
import _pp_http as H  # noqa: E402

ACCT = os.path.join(C, "outputs", "evidence_r323")
LOCKS = os.path.join(C, "work", "line_locks")
EXH = os.path.join(C, "work", "pp_exhausted.json")
os.makedirs(LOCKS, exist_ok=True)

def load_accounts():
    seen, out = {}, []
    for fp in sorted(glob.glob(os.path.join(ACCT, "pp_acc_*.json"))):
        try:
            e = json.load(io.open(fp, encoding="utf-8-sig"))
        except Exception:
            continue
        uid = str(e.get("uid") or "")
        if not uid or not e.get("cookies") or uid in seen:
            continue
        seen[uid] = 1
        out.append({"uid": uid, "tag": e.get("tag") or os.path.basename(fp)[7:-5],
                    "path": fp, "mtime": os.path.getmtime(fp)})
    try:
        ex = set(json.load(io.open(EXH, encoding="utf-8")))
    except Exception:
        ex = set()
    return [a for a in out if a["uid"] not in ex]

def lock_path(uid):
    return os.path.join(LOCKS, uid + ".lock")

def _lock_stale(fp):
    """A lock is stale when the owning PID is gone (reboot/crash) or it is ancient."""
    try:
        age = time.time() - os.path.getmtime(fp)
    except OSError:
        return True
    try:
        pid = int((io.open(fp, encoding="ascii", errors="ignore").read() or "0").strip() or 0)
    except Exception:
        pid = 0
    if pid > 0:
        try:
            import psutil
            if not psutil.pid_exists(pid):
                return True
            try:
                pr = psutil.Process(pid)
                if not pr.is_running() or pr.status() == psutil.STATUS_ZOMBIE:
                    return True
            except Exception:
                return True
            return age > 6 * 3600
        except Exception:
            pass
    return age > 3 * 3600


def try_lock(uid):
    fp = lock_path(uid)
    for _ in range(2):
        try:
            fd = os.open(fp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode()); os.close(fd)
            return fp
        except FileExistsError:
            if _lock_stale(fp):
                try: os.remove(fp)
                except OSError: return None
                continue
            return None
        except OSError:
            return None
    return None

def unlock(fp):
    try: os.remove(fp)
    except OSError: pass

def mark_exhausted(uid):
    try:
        cur = set(json.load(io.open(EXH, encoding="utf-8")))
    except Exception:
        cur = set()
    if uid not in cur:
        cur.add(uid)
        io.open(EXH, "w", encoding="utf-8").write(json.dumps(sorted(cur), indent=1))

CAP = os.path.join(C, "work", "capacity_pool.json")


def load_capacity():
    """Paths of accounts the capacity scan found credit-worthy (>=120)."""
    try:
        return set(json.load(io.open(CAP, encoding="utf-8")))
    except Exception:
        return set()


def pick(exclude=None):
    """Return (acct, lockfile) for the most recently used unlocked account.

    Accounts listed in work/capacity_pool.json (known to have credits) are tried
    first so an API job does not land on a drained account and burn a retry.
    """
    exclude = exclude or set()
    cap = load_capacity()
    accts = sorted(load_accounts(),
                   key=lambda a: (a["path"] in cap, a["mtime"]), reverse=True)
    for a in accts:
        if a["uid"] in exclude:
            continue
        lf = try_lock(a["uid"])
        if lf:
            return a, lf
    return None, None

def stats():
    accts = load_accounts()
    locked = 0
    for a in accts:
        fp = lock_path(a["uid"])
        if os.path.exists(fp) and not _lock_stale(fp):
            locked += 1
    try:
        ex = len(json.load(io.open(EXH, encoding="utf-8")))
    except Exception:
        ex = 0
    return {"accounts_total": len(accts), "accounts_locked": locked, "accounts_exhausted": ex}

def credit(acct, port):
    try:
        d = json.load(io.open(acct["path"], encoding="utf-8-sig"))
        pp = H.PP(d.get("cookies") or {}, ports=[port])
        return int(pp.credit()["total"])
    except Exception:
        return None
