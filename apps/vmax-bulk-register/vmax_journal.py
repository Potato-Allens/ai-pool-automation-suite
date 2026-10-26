# -*- coding: utf-8 -*-
"""Write-ahead log around POST /register: an account we created is never lost.

Why this exists
---------------
``register_one()`` gets ``303`` back and the account is now real: the server has
consumed the SMS code, created the user and issued a one-time OAuth code.  The
row only reaches ``results.jsonl`` when the caller returns -- so a Ctrl-C, a
killed lane, an OOM or a disk hiccup in that window loses two things at once:

* the generated ``username`` / ``password`` (they only lived in memory), and
* the fact that the number is no longer fresh.

On resume the number looks new again.  ``send-code`` answers "this phone is
already registered", the row becomes ``phone_taken``, and the only way back in
is the password-reset flow, which costs *another* SMS.  One crash, one wasted
card.

The journal fixes exactly that window.  It is an append-only JSONL next to the
output file, written twice per card:

    {"phase": "registered", ...}   as soon as the 303 lands  (credentials safe)
    {"phase": "ok", "uid": ...}    once the token exchange returned a uid

Recovery is free.  Everything needed to rebuild the row -- phone, username,
password, uid -- is already in the log, so ``recover()`` hands the runner rows
it can append without a single captcha token or SMS.

What is deliberately NOT stored: the access token (it expires in an hour and
``results.jsonl`` already keeps whatever the row needs) and anything from the
SMS itself.  Drafts are bounded by the number of cards in the run, one short
line each.
"""
from __future__ import annotations

import json
import os
import threading
import time

import vmax_jsonl as jsonl

SUFFIX = ".journal"

# One process, several lanes: appends must not interleave inside a line.
_LOCK = threading.Lock()


def default_path(out: str) -> str:
    """``results.jsonl`` -> ``results.jsonl.journal`` (same directory, same name)."""
    return str(out) + SUFFIX


def append(path, phone, phase, username=None, password=None, uid=None,
           note=None, lock=None) -> bool:
    """Append one record.  Never raises: a journal hiccup must not kill a run.

    Returns True when the line reached the file.  The write is a single
    ``os.write`` on an ``O_APPEND`` descriptor, so a lane that dies mid-call
    leaves either nothing or one whole line -- never a half-written record that
    a later ``load()`` would have to guess about.
    """
    if not path or not phone or not phase:
        return False
    rec = {"ts": int(time.time()), "phone": str(phone), "phase": str(phase)}
    if username:
        rec["username"] = str(username)
    if password:
        rec["password"] = str(password)
    if uid:
        rec["uid"] = str(uid)
    if note:
        rec["note"] = str(note)
    # vmax_jsonl owns the part that has to be right: one locked, single write per
    # record.  O_APPEND alone is not enough across processes (measured: 3 of 400
    # lines lost), and a merged journal line is an account we cannot recover.
    return jsonl.append_line(path, rec, lock=lock if lock is not None else _LOCK)


def load(path) -> list[dict]:
    """Every readable record, oldest first.  Missing file / torn line => skipped."""
    rows: list[dict] = []
    if not path:
        return rows
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except Exception:  # a torn tail after kill -9 is expected, not fatal
                    continue
                if isinstance(rec, dict) and rec.get("phone"):
                    rows.append(rec)
    except OSError:
        return []
    return rows


def latest(path) -> dict:
    """One record per phone: ``ok`` beats ``registered``, later beats earlier."""
    best: dict[str, dict] = {}
    for rec in load(path):
        phone = str(rec["phone"])
        cur = best.get(phone)
        rank = 1 if rec.get("phase") == "ok" else 0
        if cur is None:
            best[phone] = rec
            continue
        cur_rank = 1 if cur.get("phase") == "ok" else 0
        if rank >= cur_rank:
            best[phone] = rec
    return best


def _phones_in(out_path) -> set[str]:
    """Every phone that already has a row, whatever its status."""
    seen: set[str] = set()
    if not out_path:
        return seen
    try:
        with open(out_path, "r", encoding="utf-8", errors="replace") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    rec = json.loads(raw)
                except Exception:
                    continue
                if isinstance(rec, dict) and rec.get("phone"):
                    seen.add(str(rec["phone"]))
    except OSError:
        return seen
    return seen


def synthesize(entry: dict, source: str = "") -> dict:
    """Turn a journal record into the row ``results.jsonl`` would have held."""
    row = {
        "phone": entry.get("phone"),
        "username": entry.get("username"),
        "password": entry.get("password"),
        "status": "ok",
        "uid": entry.get("uid"),
        "ts": int(time.time()),
        "via": "journal",
        "journal_recovered": True,
        "note": ("rebuilt from the write-ahead log: /register returned 303 but the "
                 "row never reached the output file (0 token, 0 SMS)"),
    }
    if entry.get("note"):
        row["journal_note"] = entry["note"]
    if source:
        row["journal"] = os.path.basename(str(source))
    return row


def recover(journal_path, out_path) -> tuple[list[dict], list[dict]]:
    """Split the journal into (rows we can append now, entries we cannot).

    A record is recoverable when it has a ``uid``: that means the token
    exchange already happened, so the row can be rebuilt with no network call.
    A ``registered`` record without a uid is a crash in the ~200 ms between the
    303 and the token exchange -- rare, and reported rather than guessed at.

    Phones that already appear in ``out_path`` (any status, not just ok) are left
    alone: the row is there, the journal has nothing to add.
    """
    entries = latest(journal_path)
    if not entries:
        return [], []
    known = _phones_in(out_path)
    rows: list[dict] = []
    unresolved: list[dict] = []
    for phone in sorted(entries):
        entry = entries[phone]
        if phone in known:
            continue
        if entry.get("uid"):
            rows.append(synthesize(entry, journal_path))
        else:
            unresolved.append(entry)
    return rows, unresolved
