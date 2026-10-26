# -*- coding: utf-8 -*-
"""worker.py - account-pool workers: pick account -> HTTP submit -> poll -> download."""
import hashlib, os, re, sys, threading, time, traceback, urllib.parse, urllib.request

C = r"<PROJECT_ROOT>"
sys.path.insert(0, C)
sys.path.insert(0, os.path.join(C, "work"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import db
import refs
import _pp_http as H
from pippit import PippitAccount, PippitError, estimate_cost

BASE = os.path.dirname(os.path.abspath(__file__))
VIDEOS = os.path.join(BASE, "data", "videos")
os.makedirs(VIDEOS, exist_ok=True)
REFS_CACHE = os.path.join(BASE, "data", "refs_cache")
os.makedirs(REFS_CACHE, exist_ok=True)
LOCKS = os.path.join(BASE, "data", "line_locks")
os.makedirs(LOCKS, exist_ok=True)
os.makedirs(LOCKS, exist_ok=True)

try:
    import psutil
except Exception:
    psutil = None



def _local_ref(src):
    """Return a local path for a reference (local file or downloaded URL)."""
    src = str(src or "").strip()
    if not src:
        raise RuntimeError("empty reference")
    if os.path.exists(src):
        return src
    if src.startswith("http://") or src.startswith("https://"):
        h = hashlib.md5(src.encode("utf-8")).hexdigest()
        ext = os.path.splitext(urllib.parse.urlparse(src).path)[1] or ".bin"
        dst = os.path.join(REFS_CACHE, h + ext)
        if not os.path.exists(dst) or os.path.getsize(dst) == 0:
            req = urllib.request.Request(src, headers={"User-Agent": H.UA})
            with urllib.request.urlopen(req, timeout=90) as r, open(dst, "wb") as f:
                f.write(r.read())
        return dst
    raise RuntimeError("reference not found: %s" % src[:200])

_LOCK = threading.Lock()
_ACTIVE = [0]


def _pid_alive(pid):
    if not pid:
        return False
    try:
        if psutil:
            return psutil.pid_exists(pid)
        os.kill(pid, 0)
        return True
    except Exception:
        return False


def try_lock(uid):
    fp = os.path.join(LOCKS, str(uid) + ".lock")
    for _ in range(2):
        try:
            fh = os.open(fp, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fh, str(os.getpid()).encode())
            os.close(fh)
            return fp
        except FileExistsError:
            try:
                pid = int(open(fp, encoding="utf-8", errors="replace").read().strip() or 0)
            except Exception:
                pid = 0
            age = time.time() - os.path.getmtime(fp)
            if _pid_alive(pid) or (pid == 0 and age < 3 * 3600):
                return None
            try:
                os.remove(fp)
            except Exception:
                return None
        except Exception:
            return None
    return None


def unlock(fp):
    if fp:
        try:
            os.remove(fp)
        except Exception:
            pass


class PoolWorker:
    def __init__(self, max_workers=None):
        self.max_workers = int(max_workers or os.environ.get("WZ_MAX_WORKERS", "2"))
        self.running = False

    def start(self):
        db.init()
        self._recover()
        self.running = True
        for i in range(self.max_workers):
            t = threading.Thread(target=self._loop, name="wz-worker-%d" % i, daemon=True)
            t.start()
        threading.Thread(target=self._refresher, name="wz-credit-refresh", daemon=True).start()

    def _recover(self):
        """Unstick jobs/accounts left behind by a previous crash."""
        try:
            cutoff = time.time() - 2700
            for j in db.query("SELECT id,started_at FROM jobs WHERE status='running'"):
                if float(j.get("started_at") or 0) < cutoff:
                    db.update_job(j["id"], status="queued", stage="recovered", progress=0,
                                  retry_after=time.time() + 10)
                    db.add_event(j["id"], "recovered stale running job")
            for a in db.query("SELECT uid FROM accounts WHERE status='busy'"):
                fp = os.path.join(LOCKS, str(a["uid"]) + ".lock")
                if not os.path.exists(fp):
                    db.release_account(a["uid"], status="ready", cooldown=5)
        except Exception:
            pass

    def _loop(self):
        while self.running:
            try:
                job = db.next_queued()
            except Exception:
                time.sleep(5)
                continue
            if not job:
                time.sleep(3)
                continue
            try:
                self._process(job)
            except Exception as e:
                db.add_event(job["id"], "worker crash: %r" % (e,))
                try:
                    db.update_job(job["id"], status="failed", stage="crash",
                                  error=traceback.format_exc()[-2000:], completed_at=time.time())
                except Exception:
                    pass

    def _refresher(self):
        """Refresh credit for idle accounts every 10 minutes."""
        while self.running:
            time.sleep(600)
            try:
                rows = [a for a in db.list_accounts() if a.get("status") == "ready"]
            except Exception:
                continue
            for a in rows[:40]:
                try:
                    c = PippitAccount(a["cookies"], uid=a["uid"], ports=a["ports"] or [22030, 22031, 22032], tag=a.get("tag"))
                    credit = c.credit()["total"]
                    if os.environ.get("WZ_AUTO_CLAIM", "0") == "1" and credit < 120:
                        c.claim_all(stop_at=340)
                        credit = c.credit()["total"]
                    db.set_account_credit(a["uid"], credit)
                except Exception:
                    pass
                time.sleep(0.5)

    def _process(self, job):
        jid = job["id"]
        cost = int(job.get("cost") or estimate_cost(job.get("seconds") or 30))
        db.update_job(jid, stage="picking_account", progress=2)
        acct = db.pick_account(cost)
        if not acct:
            db.update_job(jid, status="queued", stage="waiting_for_account", progress=0,
                          retry_after=time.time() + 60)
            db.add_event(jid, "no account with >= %d credit; retry in 60s" % cost)
            return
        uid = acct["uid"]
        fp = try_lock(uid)
        if not fp:
            db.release_account(uid, credit=acct.get("credit"), status="ready", cooldown=30)
            db.update_job(jid, status="queued", stage="account_busy", retry_after=time.time() + 20)
            db.add_event(jid, "account %s busy in another line; retry in 20s" % uid)
            return

        client = None
        credit = acct.get("credit") or 0
        try:
            client = PippitAccount(acct["cookies"], uid=uid, ports=acct["ports"] or [22030, 22031, 22032],
                                   tag=acct.get("tag"), workspace_id=acct.get("workspace_id"),
                                   space_id=acct.get("space_id"))
            client.bootstrap()
            credit = client.credit()["total"]
            db.set_account_credit(uid, credit)
            if credit < cost:
                db.release_account(uid, credit=credit, status="ready", cooldown=300,
                                   error="low credit %d < %d" % (credit, cost))
                db.update_job(jid, status="queued", stage="waiting_for_account", progress=0,
                              retry_after=time.time() + 120, account_uid=None)
                db.add_event(jid, "account %s credit %d < %d" % (uid, credit, cost))
                return

            db.update_job(jid, account_uid=uid, stage="submitting", progress=5)
            db.add_event(jid, "using account %s credit=%d" % (uid, credit))

            def on_progress(*args):
                try:
                    stage = str(args[0]) if args else "?"
                    aid = args[1] if len(args) > 1 else None
                    if stage == "submitted":
                        prog, st = 10, "submitted"
                        db.add_event(jid, "thread=%s run=%s" % (args[1] if len(args) > 1 else "", args[2] if len(args) > 2 else ""))
                    elif stage in ("2", "3", "1"):
                        prog, st = 40, "generating"
                    else:
                        prog, st = 40, stage[:40]
                    if aid:
                        prog, st = 80, "asset_ready"
                    db.update_job(jid, progress=prog, stage=st)
                except Exception:
                    pass

            extra_parts = []
            for kind in ("image", "video", "audio"):
                src = job.get("ref_" + kind)
                if not src:
                    continue
                db.update_job(jid, stage="uploading_ref_" + kind)
                lp = _local_ref(src)
                info = refs.upload_asset(client.pp, lp, client.workspace_id,
                                         asset_type=refs.ASSET_TYPE[kind], log=lambda *a: None)
                url, md = ("", {})
                if kind != "video":
                    try:
                        url, md = refs.asset_meta(client.pp, info["pippit_asset_id"])
                    except Exception:
                        url, md = ("", {})
                extra_parts.append(refs.make_part(info, asset_url=url, metadata=md))
                db.add_event(jid, "ref %s entry=%s %sx%s %.2fs %d bytes" %
                             (kind, info.get("asset_id"), info.get("width"), info.get("height"),
                              float(info.get("duration") or 0), int(info.get("size") or 0)))

            dest = os.path.join(VIDEOS, jid + ".mp4")
            prompt = job["prompt"]
            secs = int(job.get("seconds") or 30)
            if secs >= 10 and not re.search(r"(\d+\s*(?:s|sec|secs|second|seconds|秒))", prompt, re.I):
                prompt = ("%d-second video, %d shots of 5 seconds each. " % (secs, max(1, secs // 5))) + prompt
                db.add_event(jid, "prompt duration hint added (%ds)" % secs)
            res = client.run_video(prompt, dest,
                                   seconds=int(job.get("seconds") or 30),
                                   resolution=job.get("resolution") or "1080p",
                                   ratio=job.get("ratio") or "9:16",
                                   timeout=2400, on_progress=on_progress,
                                   extra_parts=(extra_parts or None))
            db.update_job(jid, progress=95, stage="probing")
            meta = H.probe(dest)
            db.update_job(jid, status="completed", progress=100, stage="completed",
                          account_uid=uid, thread_id=res.get("thread_id"), run_id=res.get("run_id"),
                          asset_id=res.get("asset_id"), video_path=dest, remote_url=res.get("url"),
                          completed_at=time.time(), width=meta.get("w"), height=meta.get("h"),
                          duration=meta.get("dur"), bytes=meta.get("size"))
            db.release_account(uid, credit=res.get("credit_after", credit), status="ready",
                               cooldown=180, done=True)
            db.add_event(jid, "completed asset=%s %dx%d %.3fs %d bytes" %
                         (res.get("asset_id"), meta.get("w"), meta.get("h"), meta.get("dur"), meta.get("size")))
        except Exception as e:
            err = repr(e)[:1500]
            try:
                if client:
                    credit = client.credit()["total"]
            except Exception:
                pass
            db.release_account(uid, credit=credit, status="ready", cooldown=300, failed=True, error=err)
            db.add_event(jid, "failed on %s: %s" % (uid, err))
            if db.requeue_job(jid, error=err, max_attempts=3, delay=45):
                db.add_event(jid, "requeued for another account")
            else:
                db.update_job(jid, status="failed", stage="failed", error=err, completed_at=time.time())
        finally:
            unlock(fp)


WORKER = None


def start_workers(max_workers=None):
    global WORKER
    if WORKER is None:
        WORKER = PoolWorker(max_workers)
        WORKER.start()
    return WORKER