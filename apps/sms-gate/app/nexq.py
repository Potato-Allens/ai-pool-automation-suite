# -*- coding: utf-8 -*-
"""Cross-worker FIFO queue for number allocation (NexSMS + d1jiema).

Upstream throttles per-project orders, so a burst of users must not translate into
a burst of purchases. Requests that find the pool empty get a ticket and a position
("3 people ahead of you"); one dispatcher per worker claims a few tickets at a time
and buys one batch that serves all of them.
"""
from __future__ import annotations

import secrets
import threading
import time

from . import d1jiema, db, providers

_START_LOCK = threading.Lock()
_STARTED = False

# A serving ticket that never finishes (worker killed mid-purchase) is reclaimed.
SERVING_LEASE = 75
# Finished tickets stay readable for a while so a late poll still works.
KEEP_DONE_SEC = 1800


def _int(key: str, default: int, lo: int, hi: int) -> int:
    try:
        return max(lo, min(hi, int(db.get_setting(key, str(default)) or default)))
    except Exception:
        return default


def concurrency() -> int:
    """How many tickets may be worked on at the same time."""
    return _int("nexsms_queue_concurrency", 5, 1, 20)


def max_len() -> int:
    return _int("nexsms_queue_max", 200, 10, 5000)


def timeout() -> int:
    return _int("nexsms_queue_timeout_sec", 600, 60, 3600)


def _count(sql: str, args=()) -> int:
    r = db.q1(sql, args)
    return int(r["n"]) if r else 0


def stats() -> dict:
    return {
        "waiting": _count("SELECT COUNT(*) AS n FROM nex_queue WHERE status='waiting'"),
        "serving": _count("SELECT COUNT(*) AS n FROM nex_queue WHERE status='serving'"),
        "ready": _count("SELECT COUNT(*) AS n FROM nex_queue WHERE status='ready'"),
        "concurrency": concurrency(),
        "cooldown": providers.cooldown_left(),
    }


def position_of(tid: int) -> int:
    """People still ahead: earlier tickets that are waiting or being served."""
    return _count("SELECT COUNT(*) AS n FROM nex_queue WHERE id<? "
                  "AND status IN ('waiting','serving')", (tid,))


def live_ticket(card_id: int):
    r = db.q1("SELECT * FROM nex_queue WHERE card_id=? AND status IN ('waiting','serving') "
              "ORDER BY id LIMIT 1", (card_id,))
    return dict(r) if r else None


def _payload(t: dict) -> dict:
    ahead = position_of(int(t["id"]))
    total = _count("SELECT COUNT(*) AS n FROM nex_queue WHERE status IN ('waiting','serving')")
    return {
        "id": int(t["id"]), "token": t["token"], "country": t["country"],
        "choice": t.get("choice") or t["country"],
        "provider": t.get("provider") or "",
        "ahead": ahead, "position": ahead + 1, "total": max(total, ahead + 1),
        "eta": min(900, ahead * 4), "status": t["status"],
        "cooldown": providers.cooldown_left(),
    }


def snapshot(t: dict) -> dict:
    """Public view of one ticket (used when restoring a session)."""
    return _payload(t)


def enqueue(card_id: int, choice: str, ip: str = "", provider: str = "",
            card_type: str = "") -> dict:
    """Join the queue (or return the existing ticket for this card)."""
    t = live_ticket(card_id)
    if t:
        return _payload(t)
    choice = str(choice or "us")
    provider = provider or providers.provider_for_choice(choice)
    card_type = card_type or str((providers.CHOICES.get(choice) or {}).get("card_type") or "")
    cd = providers.cooldown_left()
    if cd > 180:
        # A long upstream ban: queueing people for 50 minutes is worse than saying no.
        raise providers.ProviderBusy("号源通道限流中，约 %d 分钟后恢复，请稍后再试" % max(1, cd // 60),
                                     retry_after=cd)
    if _count("SELECT COUNT(*) AS n FROM nex_queue WHERE status IN ('waiting','serving')") >= max_len():
        raise providers.ProviderBusy("排队人数已满，请稍后再试", retry_after=15)
    tok = secrets.token_urlsafe(18)
    now = int(time.time())
    with db.tx() as c:
        # Re-check inside the write transaction: BEGIN IMMEDIATE serialises writers, so
        # a burst of twenty clicks can never create twenty tickets for one card.
        row = c.execute("SELECT * FROM nex_queue WHERE card_id=? AND status IN ('waiting','serving') "
                        "ORDER BY id LIMIT 1", (card_id,)).fetchone()
        if row:
            return _payload(dict(row))
        cur = c.execute(
            "INSERT INTO nex_queue(token,card_id,country,choice,provider,card_type,status,created_at,ip) "
            "VALUES(?,?,?,?,?,?, 'waiting', ?, ?)",
            (tok, card_id, choice, choice, provider, card_type, now, ip))
        row = c.execute("SELECT * FROM nex_queue WHERE id=?", (cur.lastrowid,)).fetchone()
        out = dict(row)
    return _payload(out)


def status(tid: int, token: str, card_id: int):
    """Ticket snapshot for its owner, or None when unknown/foreign."""
    if tid <= 0 or not token:
        return None
    r = db.q1("SELECT * FROM nex_queue WHERE id=? AND card_id=?", (tid, card_id))
    if not r:
        return None
    t = dict(r)
    if not secrets.compare_digest(str(t["token"]), str(token)):
        return None
    now = int(time.time())
    if t["status"] == "waiting" and now - int(t["created_at"]) > timeout():
        db.conn().execute("UPDATE nex_queue SET status='expired', done_at=?, err='排队超时' WHERE id=?",
                          (now, tid))
        t["status"] = "expired"
        t["err"] = "排队超时，请重新取号"
    out = _payload(t)
    out["state"] = t["status"]
    out["error"] = t.get("err") or ""
    if t["status"] == "ready":
        out["number_id"] = int(t["number_id"])
    return out


def cancel_live(card_id: int) -> None:
    """Drop pending tickets for a card that just received a number by another path."""
    db.conn().execute("UPDATE nex_queue SET status='done', done_at=?, err='已取到号码' "
                      "WHERE card_id=? AND status IN ('waiting','serving')",
                      (int(time.time()), card_id))


def finish(tid: int) -> None:
    db.conn().execute("UPDATE nex_queue SET status='done', done_at=? WHERE id=?",
                      (int(time.time()), tid))


# ------------------------------------------------------------------ dispatcher
def release_alloc_markers() -> None:
    """A card whose ticket died must be able to try again (same locked choice)."""
    try:
        db.conn().execute(
            "UPDATE card_choices SET number_id=0 WHERE number_id=-1 AND card_id NOT IN "
            "(SELECT card_id FROM nex_queue WHERE status IN ('waiting','serving','ready'))")
    except Exception:
        pass


def _sweep() -> None:
    now = int(time.time())
    db.conn().execute("UPDATE nex_queue SET status='waiting', started_at=0, err='' "
                      "WHERE status='serving' AND started_at<?", (now - SERVING_LEASE,))
    db.conn().execute("UPDATE nex_queue SET status='expired', done_at=?, err='排队超时' "
                      "WHERE status='waiting' AND created_at<?", (now, now - timeout()))
    db.conn().execute("UPDATE nex_queue SET status='done', err='已交付' "
                      "WHERE status='ready' AND done_at<?", (now - 900,))
    db.conn().execute("DELETE FROM nex_queue WHERE status IN ('done','error','expired') AND done_at<?",
                      (now - KEEP_DONE_SEC,))
    release_alloc_markers()


def _claim(limit: int) -> list:
    """Atomically take up to `limit` waiting tickets, never exceeding concurrency."""
    now = int(time.time())
    out = []
    with db.tx() as c:
        serving = c.execute("SELECT COUNT(*) AS n FROM nex_queue WHERE status='serving'").fetchone()["n"]
        free = concurrency() - int(serving)
        if limit < free:
            free = limit
        if free <= 0:
            return []
        rows = c.execute("SELECT * FROM nex_queue WHERE status='waiting' ORDER BY id LIMIT ?",
                         (free,)).fetchall()
        for r in rows:
            cur = c.execute("UPDATE nex_queue SET status='serving', started_at=?, tries=tries+1 "
                            "WHERE id=? AND status='waiting'", (now, r["id"]))
            if cur.rowcount == 1:
                d = dict(r)
                d["tries"] = int(d.get("tries") or 0) + 1
                out.append(d)
    return out


def _ready(tid: int, number_id: int) -> None:
    db.conn().execute("UPDATE nex_queue SET status='ready', number_id=?, done_at=? WHERE id=?",
                      (number_id, int(time.time()), tid))


def _requeue(t: dict, err: str, soft: bool = False) -> None:
    if soft:
        db.conn().execute("UPDATE nex_queue SET status='waiting', started_at=0, err=? WHERE id=?",
                          (err[:160], t["id"]))
        return
    if int(t.get("tries") or 0) >= 3:
        db.conn().execute("UPDATE nex_queue SET status='error', err=?, done_at=? WHERE id=?",
                          (err[:160], int(time.time()), t["id"]))
    else:
        db.conn().execute("UPDATE nex_queue SET status='waiting', started_at=0, err=? WHERE id=?",
                          (err[:160], t["id"]))


def _serve(batch: list) -> None:
    nex_items = []
    for t in batch:
        choice = str(t.get("choice") or t.get("country") or "")
        info = providers.CHOICES.get(choice) or {}
        prov = str(t.get("provider") or "") or providers.provider_for_choice(choice)
        if prov == "d1jiema":
            _serve_d1(t, choice, str(t.get("card_type") or info.get("card_type") or "全部"))
        else:
            nex_items.append(t)
    if nex_items:
        _serve_nex(nex_items)


def _serve_d1(t: dict, choice: str, card_type: str) -> None:
    """Buy one d1jiema number for one ticket. Upstream calls are paced globally."""
    tid = int(t["id"])
    card_id = int(t["card_id"])
    now = int(time.time())
    card = db.q1("SELECT * FROM cards WHERE id=?", (card_id,))
    if not card:
        db.conn().execute("UPDATE nex_queue SET status='error',err='卡密不存在',done_at=? WHERE id=?",
                          (now, tid))
        return
    if card["status"] != "active" or int(card["used_codes"]) >= int(card["max_codes"]):
        db.conn().execute("UPDATE nex_queue SET status='error',err='卡密已失效',done_at=? WHERE id=?",
                          (now, tid))
        return
    if db.q1("SELECT id FROM numbers WHERE card_id=?", (card_id,)):
        db.conn().execute("UPDATE nex_queue SET status='error',err='该卡密已取过号码',done_at=? WHERE id=?",
                          (now, tid))
        db.conn().execute("UPDATE card_choices SET number_id=0 WHERE card_id=? AND number_id=-1",
                          (card_id,))
        return
    tok = d1jiema.card_lease(card_id)
    if not tok:
        _requeue(t, "正在为该卡密取号", soft=True)
        return
    try:
        phone = d1jiema.get_phone("", card_type)
        with db.tx() as c:
            if c.execute("SELECT id FROM numbers WHERE card_id=?", (card_id,)).fetchone():
                c.execute("UPDATE nex_queue SET status='error',err='该卡密已取过号码',done_at=? WHERE id=?",
                          (now, tid))
                c.execute("UPDATE card_choices SET number_id=0 WHERE card_id=? AND number_id=-1",
                          (card_id,))
                return
            cur = c.execute(
                "INSERT INTO numbers(phone,country,provider,ext_id,choice,status,card_id,"
                "allocated_at,created_at,price,note) VALUES(?,?,?,?,?,'allocated',?,?,?,?,?)",
                (phone, "cn", "d1jiema", phone, choice, card_id, now, now, 0, card_type))
            nid = int(cur.lastrowid)
            c.execute("UPDATE card_choices SET number_id=?, alloc_at=? WHERE card_id=?",
                      (nid, now, card_id))
        _ready(tid, nid)
    except d1jiema.D1Busy as e:
        _requeue(t, str(e), soft=True)
    except d1jiema.D1Error as e:
        _requeue(t, str(e), soft=False)
    except Exception as e:
        _requeue(t, "%s: %s" % (type(e).__name__, str(e)[:80]), soft=True)
    finally:
        d1jiema.card_unlease(card_id, tok)


def _serve_nex(items: list) -> None:
    groups = {}
    for t in items:
        country = str(t.get("choice") or t.get("country") or "")
        groups.setdefault(country, []).append(t)
    for country, group in groups.items():
        err = ""
        need = len(group) - providers._nex_available_count(country)
        while need > 0:
            if providers.cooldown_left() > 0:
                err = "上游限流冷却中"
                break
            try:
                qty = min(10, max(need, providers._nex_batch_qty()))
                app, rows = providers._nex_purchase(country, qty)
                providers._nex_store_items(rows, country, 0, app)
            except providers.ProviderBusy as e:
                err = str(e)
                break
            except providers.ProviderError as e:
                err = str(e)
                break
            except Exception as e:
                err = "%s: %s" % (type(e).__name__, str(e)[:100])
                break
            need = len(group) - providers._nex_available_count(country)
        for t in group:
            try:
                row = providers._take_available_nex(country, int(t["card_id"]))
            except Exception:
                row = None
            if row:
                try:
                    db.conn().execute("UPDATE numbers SET choice=? WHERE id=?", (country, row["id"]))
                except Exception:
                    pass
                _ready(int(t["id"]), int(row["id"]))
            else:
                _requeue(t, err or "号源暂时不足")


def worker_loop() -> None:
    while True:
        try:
            _sweep()
            cd = providers.cooldown_left()
            if cd > 0:
                time.sleep(min(3, max(1, cd)))
                continue
            batch = _claim(concurrency())
            if not batch:
                time.sleep(0.7)
                continue
            _serve(batch)
            time.sleep(0.05)
        except Exception:
            time.sleep(1.5)


def retire_queue() -> None:
    """The queue is retired. Close leftovers so no browser can wait forever."""
    try:
        db.conn().execute(
            "UPDATE nex_queue SET status='done', done_at=?, err='已切换为直接取号' "
            "WHERE status IN ('waiting','serving','ready')", (int(time.time()),))
    except Exception:
        pass


def start() -> None:
    global _STARTED
    with _START_LOCK:
        if _STARTED:
            return
        _STARTED = True
    threading.Thread(target=worker_loop, daemon=True, name="nexq-dispatcher").start()
