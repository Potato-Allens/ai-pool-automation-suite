# -*- coding: utf-8 -*-
"""Console helpers shared by the CLI entry points."""
from __future__ import annotations

import sys


def utf8_stdout() -> None:
    """Print Chinese safely on Windows.

    A piped stdout on cp936 raises UnicodeEncodeError on the first Chinese
    character (observed 2026-10-26: vmax_accept died printing a rehearsal log
    that contained 手机号格式不正确).  Reconfiguring the TextIOWrapper fixes it.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - older/odd streams
            pass
