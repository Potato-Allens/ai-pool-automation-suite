# -*- coding: utf-8 -*-
"""Day-of runner: SIM cards in, verified accounts out, one command.

    python vmax_day.py --input cards.csv \
        --sms "http://127.0.0.1:8787/code?phone={phone}&secret=<TEST_SECRET>&since={since}" \
        --out runs/1026/results.jsonl --farm-workers 3 --yes

Order
-----
preflight -> token farm up -> batch (vmax_go) -> password re-check (vmax_login)
-> report (vmax_report) -> manifest.json

Why another wrapper: `vmax_go.py` already runs preflight + batch, but on the
day the SIM cards arrive three things still bite:

* the token farm is a separate process nobody remembers to start (queue
  starvation turns into `no_token` failures, which look like bad cards);
* nobody proves the accounts afterwards -- a 303 + JWT can still be a login
  that never works, so we re-check every ok row with `vmax_login`;
* a crash leaves no single artifact that says what was run, with which input,
  and how far it got.

Every step is a subprocess with its own log under `<run dir>/logs/`, and the
manifest is rewritten after each step, so a killed run still tells you where it
stopped.

Exit codes: 0 ok, 1 some accounts failed, 2 preflight failed, 3 aborted on 429,
4 nothing to do, 5 stopped on --max-fail, 6 stopped on --max-minutes,
7 pool had already-registered numbers, 8 farm would not start,
9 the SMS channel is not ready (nothing was spent), 130 interrupted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import threading
import time

from vmax_sms import build_sms, channel_check  # noqa: F401  (build_sms: tests)

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable or "python"

LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def _iso(ts: float | None = None) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() if ts is None else ts))


def _sha256(path: str) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def mask_secrets(text: str) -> str:
    """`secret=abc` -> `secret=***`; also `X-Sms-Secret: abc`."""
    text = re.sub(r"(?i)(secret=)[^&\s]+", r"\1***", text)
    text = re.sub(r"(?i)(x-sms-secret\s*:\s*)\S+", r"\1***", text)
    return text


def count_rows(path: str) -> int:
    """Rows in a CSV (minus header) or JSONL (non-blank lines)."""
    try:
        if path.lower().endswith((".jsonl", ".ndjson")):
            with open(path, encoding="utf-8-sig") as f:
                return sum(1 for line in f if line.strip())
        import csv
        with open(path, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
        return max(0, len([r for r in rows[1:] if any(c.strip() for c in r)]))
    except OSError:
        return 0


def _load_json(path: str):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001 - a missing/broken summary is not fatal
        return None


def _save_json(obj: dict, path: str) -> None:
    """Write atomically; keys starting with ``_`` are in-process only."""
    tmp = path + ".tmp"
    payload = {k: v for k, v in obj.items() if not k.startswith("_")}
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# subprocess steps
# --------------------------------------------------------------------------- #
def _execute(cmd: list[str], log_path: str, echo: bool = True) -> tuple[int, str]:
    """Run *cmd*, tee stdout+stderr to *log_path*, return (exit code, text)."""
    os.makedirs(os.path.dirname(os.path.abspath(log_path)) or ".", exist_ok=True)
    lines: list[str] = []
    with open(log_path, "w", encoding="utf-8", errors="replace") as f:
        f.write("# " + " ".join(mask_secrets(" ".join(cmd))) + "\n")
        f.flush()
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, encoding="utf-8", errors="replace",
                             bufsize=1, env=env)
        assert p.stdout is not None
        for line in p.stdout:
            f.write(line)
            f.flush()
            lines.append(line)
            if echo:
                sys.stdout.write(line)
                sys.stdout.flush()
        rc = p.wait()
    return rc, "".join(lines)


def run_step(name: str, cmd: list[str], manifest: dict, run_dir: str,
             execute=None) -> tuple[int, str]:
    """Execute one pipeline step, record it in the manifest, return (rc, text)."""
    execute = execute or _execute
    log_path = os.path.join(run_dir, "logs", name.replace(" ", "_") + ".log")
    started = time.time()
    print(f"[day] step {name}: {mask_secrets(' '.join(cmd))}", flush=True)
    rc, text = execute(cmd, log_path)
    rec = {"name": name, "cmd": [mask_secrets(c) for c in cmd], "exit": rc,
           "started": _iso(started), "seconds": round(time.time() - started, 1),
           "log": log_path}
    with LOCK:
        manifest.setdefault("steps", []).append(rec)
        _save_json(manifest, manifest["_path"])
    print(f"[day] step {name} -> {rc} ({rec['seconds']}s)", flush=True)
    return rc, text


# --------------------------------------------------------------------------- #
# token farm (background process + supervision)
# --------------------------------------------------------------------------- #
class Farm:
    def __init__(self, cmd: list[str], token_dir: str, log_path: str,
                 popen=None, restart_limit: int = 3):
        self.cmd = cmd
        self.token_dir = token_dir
        self.log_path = log_path
        self._popen = popen or self._default_popen
        self.restart_limit = restart_limit
        self.proc = None
        self._fh = None
        self.starts = 0
        self.restarts = 0
        self.dead = False
        self.want_alive = False

    @staticmethod
    def _default_popen(cmd, fh):
        flags = 0
        if os.name == "nt":
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        return subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace",
                                env=env, creationflags=flags)

    def start(self) -> bool:
        os.makedirs(os.path.dirname(os.path.abspath(self.log_path)) or ".", exist_ok=True)
        self._fh = open(self.log_path, "a", encoding="utf-8", errors="replace")
        self._fh.write("# " + mask_secrets(" ".join(self.cmd)) + "\n")
        self._fh.flush()
        try:
            self.proc = self._popen(self.cmd, self._fh)
        except Exception as e:  # noqa: BLE001
            print(f"[day] farm failed to start: {type(e).__name__}: {e}", file=sys.stderr)
            return False
        self.starts += 1
        self.want_alive = True
        return True

    def alive(self) -> bool:
        return bool(self.proc) and self.proc.poll() is None

    def depth(self) -> int:
        try:
            return len([f for f in os.listdir(self.token_dir)
                        if f.startswith("tok_") and f.endswith(".json")])
        except OSError:
            return 0

    def wait_ready(self, want: int = 1, timeout: float = 300.0) -> bool:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.depth() >= want:
                return True
            if not self.alive():
                if self.restarts >= self.restart_limit:
                    return False
                self.restarts += 1
                print(f"[day] farm exited early; restart {self.restarts}/{self.restart_limit}", flush=True)
                self.start()
            time.sleep(3)
        return self.depth() >= want

    def check(self) -> None:
        """Supervisor tick: restart a dead farm (bounded)."""
        if not self.want_alive or self.alive():
            return
        if self.restarts >= self.restart_limit:
            self.dead = True
            return
        self.restarts += 1
        print(f"[day] farm died; restart {self.restarts}/{self.restart_limit}", flush=True)
        self.start()

    def stop(self) -> None:
        self.want_alive = False
        proc = self.proc
        if proc is not None and proc.poll() is None:
            try:
                if os.name == "nt":
                    subprocess.call(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                else:
                    proc.terminate()
            except Exception:  # noqa: BLE001
                pass
            try:
                proc.wait(timeout=10)
            except Exception:  # noqa: BLE001
                pass
        if self._fh:
            try:
                self._fh.close()
            except Exception:  # noqa: BLE001
                pass
            self._fh = None


class Supervisor(threading.Thread):
    def __init__(self, farm: Farm, stop_event: threading.Event, interval: float = 5.0):
        super().__init__(daemon=True)
        self.farm = farm
        self.stop_event = stop_event
        self.interval = interval

    def run(self) -> None:
        while not self.stop_event.wait(self.interval):
            try:
                self.farm.check()
            except Exception:  # noqa: BLE001 - never let supervision kill the run
                pass


# --------------------------------------------------------------------------- #
# manifest
# --------------------------------------------------------------------------- #
def new_manifest(args, run_dir: str, argv: list[str] | None = None) -> dict:
    return {
        "tool": "vmax_day", "version": 1,
        "started": _iso(), "_t0": time.time(),
        "cwd": os.getcwd(), "run_dir": os.path.abspath(run_dir),
        "argv": [mask_secrets(a) for a in (argv if argv is not None else sys.argv[1:])],
        "input": {"path": os.path.abspath(args.input or ""),
                  "sha256": _sha256(args.input) if args.input else "",
                  "rows": count_rows(args.input) if args.input else 0},
        "config": {
            "out": os.path.abspath(args.out),
            "sms": mask_secrets(args.sms),
            "sms_dir": os.path.abspath(args.sms_dir),
            "sms_resends": args.sms_resends,
            "sms_recheck": getattr(args, "sms_recheck", 5),
            "sms_recheck_seconds": getattr(args, "sms_recheck_seconds", 300.0),
            "no_sms_check": bool(getattr(args, "no_sms_check", False)),
            "captcha": args.captcha,
            "token_dir": os.path.abspath(args.token_dir),
            "farm_workers": args.farm_workers,
            "farm_max_queue": args.farm_max_queue,
            "verify": args.verify,
            "sms_check": getattr(args, "sms_given", False)
                          and not getattr(args, "no_sms_check", False),
            "sms_given": getattr(args, "sms_given", False),
            "on_taken": getattr(args, "on_taken", "skip"),
            "retry_limited": getattr(args, "retry_limited", False),
            "contract": os.path.abspath(args.contract)
                        if getattr(args, "contract", None) else None,
            "platform": args.platform,
            "base": args.base or "https://auth.vobilemax.cn",
        },
        "steps": [],
        "farm": {},
        "_path": os.path.join(run_dir, "manifest.json"),
    }


def finish(manifest: dict, verdict: str, code: int) -> int:
    manifest["verdict"] = verdict
    manifest["exit_code"] = code
    manifest["ended"] = _iso()
    manifest["duration_s"] = round(time.time() - manifest.pop("_t0", time.time()), 1)
    try:
        _save_json(manifest, manifest["_path"])
    except Exception as e:  # noqa: BLE001
        print(f"[day] could not write manifest: {e}", file=sys.stderr)
    print(f"[day] verdict={verdict} exit={code} manifest={manifest['_path']}", flush=True)
    return code


# --------------------------------------------------------------------------- #
# commands
# --------------------------------------------------------------------------- #
def verdict_for(rc_batch: int, go_summary: dict | None) -> str:
    """Manifest verdict for the batch exit code.

    Exit 7 usually means "the pool was full of numbers someone else registered".
    When the run also burned its reset channel on a daily cap, say so: the
    manifest then tells you the taken rows were not even retried today, which is
    a different next action (resume tomorrow) from "buy fewer recycled cards".
    """
    code = rc_batch if isinstance(rc_batch, int) else -1
    v = {0: "ok", 1: "some_failed", 3: "aborted_429", 4: "nothing_to_do",
         5: "stopped_max_fail", 6: "stopped_max_minutes", 7: "pool_had_taken",
         9: "sms_channel_down"}.get(
             code, f"batch_exit_{code}")
    summ = go_summary or {}
    # Two different ways to end with a capped reset channel: the breaker fired
    # during this run, or the numbers were already known-capped today (0 token).
    if code == 7 and (summ.get("reset_limited") or summ.get("skipped_capped")):
        return "pool_had_taken_reset_capped"
    return v


def _as_int(v) -> int:
    """A corrupted summary must not crash the day runner."""
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def should_verify(go_summary: dict | None) -> bool:
    """Does this run need the 0-SMS re-verification pass?

    Rows recovered from the write-ahead log are exactly the ones we know least
    about: the account was created, but the run that created it never finished
    (the token exchange may not even have happened).  Gating only on ``ok`` would
    skip them and leave the batch "green" with unverified accounts, so a recovery
    counts as rows produced.
    """
    if not go_summary:
        return False
    return (_as_int(go_summary.get("ok")) > 0
            or _as_int(go_summary.get("journal_recovered")) > 0)


def _tool(name: str) -> list[str]:
    return [PY, "-u", os.path.join(HERE, name)]


def build_commands(args) -> dict[str, list[str]]:
    common_auth = []
    if args.base:
        common_auth += ["--base", args.base]
    if args.redirect:
        common_auth += ["--redirect", args.redirect]
    if args.proxy:
        common_auth += ["--proxy", args.proxy]
    if args.platform != "dreammaker":
        common_auth += ["--platform", args.platform]

    # --no-sms-check must mean *no probe anywhere*: day skips its own gate and the
    # child would otherwise re-gate (and exit 9) a second after the flag said "skip".
    skip_sms = ["--no-sms-check"] if getattr(args, "no_sms_check", False) else []
    preflight = _tool("vmax_go.py") + ["--preflight-only", "--captcha", args.captcha,
                                       "--queue-dir", args.token_dir,
                                       "--on-taken", args.on_taken] + common_auth + skip_sms
    batch = (_tool("vmax_go.py") +
             ["--input", args.input, "--out", args.out,
              "--captcha", args.captcha, "--queue-dir", args.token_dir,
              "--queue-wait", str(args.queue_wait), "--sms", args.sms,
              "--sms-dir", args.sms_dir,
              "--sms-resends", str(args.sms_resends),
              "--sms-timeout", str(args.sms_timeout),
              # The mid-run watchdog lives in vmax_go (R118); hand it down so a
              # 300-number day can be tuned from here instead of only by calling go.
              "--sms-recheck", str(getattr(args, "sms_recheck", 5)),
              "--sms-recheck-seconds", str(getattr(args, "sms_recheck_seconds", 300.0)),
              "--max-fail", str(args.max_fail), "--max-minutes", str(args.max_minutes),
              "--on-taken", args.on_taken, "--yes"] + common_auth + skip_sms)
    if args.on_taken == "reset" and getattr(args, "retry_limited", False):
        # Only meaningful with the reset channel, and only when asked: retrying a
        # number that already answered 429 today costs one more captcha token.
        batch += ["--retry-limited"]
    if args.captcha == "queue":
        batch += ["--captcha-attempts", str(args.captcha_attempts)]
    else:
        batch += ["--captcha-param", args.captcha_param]
    batch += ["--form-captcha", getattr(args, "form_captcha", "auto")]
    if args.force or args.go_force:
        # vmax_go runs its own preflight.  --force overrides every gate (here and
        # in the child); --go-force only relaxes the child, which is what a mock
        # base or an offline rehearsal needs.  --skip-preflight deliberately does
        # NOT imply --force: skipping *this* wrapper's step must not silently
        # disable the child's checks.
        batch += ["--force"]
    contract: list[str] = []
    if getattr(args, "contract", None):
        # Free gate (0 token / 0 SMS): the site renaming a field costs a whole
        # batch, so check the fingerprint before the farm spends anything.
        contract = (_tool("vmax_contract.py") + ["check", "--baseline", args.contract]
                    + [x for x in (["--base", args.base] if args.base else [])
                       + (["--redirect", args.redirect] if args.redirect else [])
                       + (["--proxy", args.proxy] if args.proxy else [])])
    login_out = os.path.join(os.path.dirname(os.path.abspath(args.out)) or ".", "logins.jsonl")
    verify = (_tool("vmax_login.py") +
              ["--input", args.out, "--out", login_out, "--captcha", args.captcha,
               "--queue-dir", args.token_dir, "--token-reuse-seconds", "60"] + common_auth)
    if args.captcha == "param":
        verify += ["--captcha-param", args.captcha_param]
    report = _tool("vmax_report.py") + [args.out]
    report_login = _tool("vmax_report.py") + [login_out]
    farm = (_tool("vmax_tokens.py") +
            ["--workers", str(args.farm_workers), "--max-queue", str(args.farm_max_queue),
             "--out-dir", args.token_dir, "--attempts", str(args.captcha_attempts)] + common_auth)
    return {"preflight": preflight, "contract": contract, "farm": farm, "batch": batch,
            "verify": verify, "report": report, "report_login": report_login,
            "login_out": login_out}


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="day-of runner: cards -> verified accounts")
    ap.add_argument("--input", help="CSV/JSONL of cards (phone[,username,password,proxy])")
    ap.add_argument("--out", default="results.jsonl")
    ap.add_argument("--sms", default=None,
                    help="sms provider template (see vmax_sms.py); when given, the channel "
                         "is probed before the farm spends a token")
    ap.add_argument("--sms-resends", type=int, default=1,
                    help="resend the code N times when the first one does not arrive (default 1)")
    ap.add_argument("--sms-timeout", type=int, default=300)
    ap.add_argument("--sms-recheck", type=int, default=5,
                    help="re-probe the SMS channel every N accounts mid-run (0 = off; "
                         "passed to the child)")
    ap.add_argument("--sms-recheck-seconds", type=float, default=300.0,
                    help="... or every S seconds mid-run (0 = off; passed to the child)")
    ap.add_argument("--sms-dir", default="sms_inbox",
                    help="directory watched by the `file:` sms provider")
    ap.add_argument("--no-sms-check", action="store_true",
                    help="skip the pre-token SMS channel probe here *and* in the child "
                         "(the default check costs 0 token and 0 SMS and runs before the "
                         "farm starts)")
    ap.add_argument("--captcha", default="queue", choices=["queue", "param"])
    ap.add_argument("--form-captcha", default="auto",
                    choices=["auto", "reuse", "fresh", "device"],
                    help="how the /register submit gets its captcha: auto/reuse/fresh, or "
                         "device = the site's own signup-check path (no form token; R140)")
    ap.add_argument("--captcha-param", default="",
                    help="pre-solved token for --captcha param (e.g. a paid solver's answer)")
    ap.add_argument("--token-dir", default="tokens")
    ap.add_argument("--queue-wait", type=float, default=600)
    ap.add_argument("--captcha-attempts", type=int, default=6)
    ap.add_argument("--farm-workers", type=int, default=2)
    ap.add_argument("--farm-max-queue", type=int, default=4)
    ap.add_argument("--farm-ready-timeout", type=float, default=300)
    ap.add_argument("--max-fail", type=int, default=0)
    ap.add_argument("--max-minutes", type=float, default=0)
    ap.add_argument("--skip-farm", action="store_true",
                    help="do not start the farm (tokens are already in --token-dir)")
    ap.add_argument("--skip-preflight", action="store_true",
                    help="skip only the wrapper preflight step (the child vmax_go still checks)")
    ap.add_argument("--contract", default=None, metavar="BASELINE",
                    help="run vmax_contract.py check against this baseline before spending "
                         "anything (1 token / 1 SMS are refused on a critical drift)")
    ap.add_argument("--go-force", action="store_true",
                    help="pass --force to the child vmax_go only (mock/offline bases)")
    ap.add_argument("--on-taken", default="skip", choices=["skip", "reset"],
                    help="already-registered numbers: skip (default) or recover them with "
                         "the password-reset flow (1 token + 1 SMS each)")
    ap.add_argument("--retry-limited", action="store_true",
                    help="with --on-taken reset, also retry numbers that already "
                         "answered 429 today (one token each)")
    ap.add_argument("--no-verify", dest="verify", action="store_false",
                    help="skip the password re-check after the batch")
    ap.add_argument("--force", action="store_true", help="run even if preflight fails")
    ap.add_argument("--yes", action="store_true", help="accepted for parity; the wrapper never prompts")
    ap.add_argument("--base", default=None)
    ap.add_argument("--redirect", default=None)
    ap.add_argument("--proxy", default=None)
    ap.add_argument("--platform", default="dreammaker", choices=["dreammaker", "max"])
    ap.add_argument("--dry-run", action="store_true", help="print the plan, touch nothing")
    ap.add_argument("--preflight-only", action="store_true")
    ap.add_argument("--execute", default=None, help=argparse.SUPPRESS)  # test hook
    args = ap.parse_args(argv)
    args.verify = getattr(args, "verify", True)
    try:
        from vmax_console import utf8_stdout
        utf8_stdout()
    except Exception:  # pragma: no cover
        pass
    sms_given = args.sms is not None
    args.sms_given = sms_given
    if args.sms is None:
        args.sms = "http://127.0.0.1:8787/code?phone={phone}"

    run_dir = os.path.dirname(os.path.abspath(args.out)) or "."
    os.makedirs(os.path.join(run_dir, "logs"), exist_ok=True)
    manifest = new_manifest(args, run_dir, argv if argv is not None else sys.argv[1:])
    _save_json(manifest, manifest["_path"])
    cmds = build_commands(args)

    if args.dry_run:
        manifest["plan"] = {k: [mask_secrets(x) for x in v]
                            for k, v in cmds.items() if isinstance(v, list) and v}
        print("[day] dry run -- commands that would be executed:", flush=True)
        for name in ("preflight", "contract", "farm", "batch", "verify", "report"):
            if not cmds.get(name):
                continue
            print(f"  {name:9s}: {mask_secrets(' '.join(cmds[name]))}", flush=True)
        return finish(manifest, "dry_run", 0)

    if not args.input:
        print("[day] --input is required (or use --dry-run)", file=sys.stderr)
        return finish(manifest, "no_input", 2)
    if not os.path.isfile(args.input):
        print(f"[day] input not found: {args.input}", file=sys.stderr)
        return finish(manifest, "no_input", 2)
    if manifest["input"]["rows"] == 0:
        print(f"[day] input has no rows: {args.input}", file=sys.stderr)
        return finish(manifest, "empty_input", 2)

    execute = args.execute or _execute

    if not args.skip_preflight:
        rc, _ = run_step("preflight", cmds["preflight"], manifest, run_dir, execute)
        if rc == 2 and not args.force:
            return finish(manifest, "preflight_failed", 2)
        if rc != 0:
            print(f"[day] preflight exit {rc}; continuing (--force)" if not args.force
                  else f"[day] preflight exit {rc}; forced", flush=True)
    if getattr(args, "contract", None):
        rc, _ = run_step("contract", cmds["contract"], manifest, run_dir, execute)
        if rc != 0 and not args.force:
            verdict = "contract_drift" if rc == 1 else "contract_unavailable"
            print(f"[day] refusing to spend tokens: contract check exit {rc} "
                  f"(see {os.path.join(run_dir, 'logs', 'contract.log')})", file=sys.stderr, flush=True)
            return finish(manifest, verdict, 2)
        if rc != 0:
            print(f"[day] contract check exit {rc}; forced", flush=True)

    # Check the receiver *before* the farm produces single-use tokens: a dead
    # inbox otherwise costs a captcha token and a real SMS per number.
    if getattr(args, "sms_given", False) and not args.no_sms_check:
        sms = channel_check(args.sms, args.sms_dir)
        sms_ok, sms_detail = sms["ok"], sms["detail"]
        manifest["sms_check"] = sms
        with LOCK:
            _save_json(manifest, manifest["_path"])
        print("[day] sms channel: %s -- %s" % ("OK" if sms_ok else "DOWN", sms_detail),
              flush=True)
        if not sms_ok and not args.force:
            print("[day] refusing to spend tokens on a dead SMS channel "
                  "(fix it, or pass --no-sms-check / --force)", file=sys.stderr, flush=True)
            return finish(manifest, "sms_channel_down", 9)

    if args.preflight_only:
        return finish(manifest, "preflight_only", 0)

    stop_event = threading.Event()
    farm = None
    sup = None
    if not args.skip_farm and args.captcha != "param":
        farm = Farm(cmds["farm"], args.token_dir,
                    os.path.join(run_dir, "logs", "farm.log"))
        if not farm.start():
            return finish(manifest, "farm_failed", 8)
        ready = farm.wait_ready(want=1, timeout=args.farm_ready_timeout)
        manifest["farm"] = {"workers": args.farm_workers, "max_queue": args.farm_max_queue,
                            "ready": ready, "restarts": farm.restarts}
        with LOCK:
            _save_json(manifest, manifest["_path"])
        if not ready:
            print("[day] farm produced no token in time; the batch will keep waiting "
                  "(check logs/farm.log)", file=sys.stderr, flush=True)
        sup = Supervisor(farm, stop_event)
        sup.start()

    try:
        rc_batch, _ = run_step("batch", cmds["batch"], manifest, run_dir, execute)
        if rc_batch == 9 and not os.path.exists(args.out):
            # The receiver was down before the first row: nothing was written, so
            # there is nothing to verify or report.
            return finish(manifest, "sms_channel_down", 9)
        if rc_batch == 9:
            # Mid-run stop (R118 watchdog): the rows already written are real
            # accounts.  Verify and report them, then still exit 9 so nobody
            # mistakes this for a complete run.
            print("[day] batch stopped on a dead SMS channel; the finished rows are kept "
                  "and will be verified/reported", flush=True)
        go_summary = _load_json(args.out + ".summary.json")
        manifest["batch_summary"] = go_summary
        manifest["journal_recovered"] = int((go_summary or {}).get("journal_recovered") or 0)
        if manifest["journal_recovered"]:
            print(f"[day] journal: {manifest['journal_recovered']} row(s) recovered from the "
                  f"write-ahead log (0 token, 0 SMS); the 0-SMS re-verification still applies",
                  flush=True)

        rc_verify = None
        if args.verify and should_verify(go_summary):
            rc_verify, _ = run_step("verify", cmds["verify"], manifest, run_dir, execute)
            manifest["login_summary"] = _load_json(cmds["login_out"] + ".summary.json")

        rc_report, _ = run_step("report", cmds["report"], manifest, run_dir, execute)
        manifest["report"] = {"results_exit": rc_report}
        if args.verify and manifest.get("login_summary") is not None:
            rc_rl, text_rl = run_step("report_login", cmds["report_login"], manifest, run_dir, execute)
            manifest["report"]["logins_exit"] = rc_rl
            manifest["report"]["logins_text"] = text_rl[-4000:]
    finally:
        stop_event.set()
        if sup is not None:
            sup.join(timeout=10)
        if farm is not None:
            farm.stop()
        if farm is not None:
            manifest["farm"] = dict(manifest.get("farm") or {},
                                    starts=farm.starts, restarts=farm.restarts,
                                    dead=farm.dead, ended=_iso())

    verdict = verdict_for(rc_batch, go_summary)
    if rc_batch == 0 and rc_verify not in (None, 0):
        return finish(manifest, "verify_failed", 1)
    return finish(manifest, verdict, rc_batch)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("[day] interrupted", file=sys.stderr)
        raise SystemExit(130)
