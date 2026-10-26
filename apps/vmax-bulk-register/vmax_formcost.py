# -*- coding: utf-8 -*-
"""One place that knows what the form captcha cost (R152).

Six buckets, exactly one per registration row.  ``device`` and ``reuse`` ride a
token the run already paid for; the other four buy one more for POST /register.
A value we do not recognise is *unknown* and must never be counted as free --
optimism here turns straight into a wrong budget.

Why a module instead of a tuple in each runner: R151 was a bug of exactly that
shape.  ``vmax_ready`` kept its own whitelist ``(None, "auto", "device")``; when
R150 started naming the reuse path, the same row became ``reuse``, fell out of
that whitelist, and the judge said "the form was not reused" while it had spent
nothing extra.  A membership test that silently answers wrong is worse than a
missing key: nothing raises.

    >>> import vmax_formcost as F
    >>> F.extra_tokens("reuse"), F.extra_tokens("device_retry")
    (0, 1)
    >>> F.extra_tokens("who-knows") is None
    True
"""
from __future__ import annotations

#: Free: the token for POST /register was already paid (or not needed at all).
FREE = ("device", "reuse")
#: Paid: one more token was solved for the form.
PAID = ("fresh", "auto_retry", "device_fallback", "device_retry")
#: Rows written before R150 never named the path (a missing field, the old
#: ``auto`` label, or the ``none`` that :func:`path` reports).  They are free by
#: construction -- the old flow reused the send-code token -- but they are not a
#: price: :func:`extra_tokens` says None, and the summary counts them as unknown.
LEGACY = (None, "auto", "none")


def extra_tokens(kind) -> int | None:
    """Tokens bought for the form: 0 for a named free path, 1 for a paid one.

    ``None`` means "this row does not carry a price" -- either a legacy record
    that never named its path, or a value we do not recognise.  A missing price
    must not be rendered as a zero: that is how a wrong budget gets born.
    """
    if kind in FREE:
        return 0
    if kind in PAID:
        return 1
    return None


def is_free(kind) -> bool:
    """True when the path bought nothing extra.

    Legacy rows (no field, the old ``auto`` label) count as free -- the pre-R150
    flow reused the send-code token -- while an unrecognised value does not.
    """
    return extra_tokens(kind) == 0 or kind in LEGACY


def path(record: dict | None) -> str:
    """The bucket a result row recorded (``"none"`` when it never said)."""
    rec = record or {}
    return str(rec.get("form_captcha") or "none")


def count_of(rows) -> dict:
    """Histogram of buckets over result rows, missing field included as ``none``."""
    out: dict[str, int] = {}
    for r in rows:
        k = path(r if isinstance(r, dict) else None)
        out[k] = out.get(k, 0) + 1
    return out


def summarize(counts: dict | None) -> dict:
    """The summary fields every runner shares (go / panel), from a histogram.

    ``form_device + form_reuse + form_extra_tokens + form_unknown`` equals the
    number of rows the histogram was built from -- that is the invariant a
    reader needs to tell "0 tokens" from "we forgot to record it".
    """
    c = dict(counts or {})
    # For the *summary*, a row that never named its path is not "free", it is
    # "unrecorded" -- otherwise the two look identical six months later.
    known = FREE + PAID
    return {
        "form_device": c.get("device", 0),
        "form_reuse": c.get("reuse", 0),
        "form_fallback": c.get("device_fallback", 0) + c.get("device_retry", 0),
        "form_extra_tokens": sum(c.get(k, 0) for k in PAID),
        "form_unknown": sum(c.get(k, 0) for k in c if k not in known),
        "form_rows": c,
    }