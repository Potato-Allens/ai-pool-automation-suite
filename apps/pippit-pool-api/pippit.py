# -*- coding: utf-8 -*-
"""pippit.py - Pippit (Dreamina Seedance) account client, pure HTTP.

Reverse-engineered endpoints (2026-10):
  GET  /home                                -> workspace_id / space_id (SSR HTML)
  POST /commerce/v1/benefits/user_credit    -> vip/gift/purchase credit
  GET  /api/biz/v1/reward_task/list         -> reward tasks
  POST /api/biz/v1/reward_task/finish       -> {task_id}
  POST /api/biz/v1/reward_task/claim        -> {task_id}
  POST /api/biz/v1/agent/submit_run?babi_param=...  -> run/thread
  POST /api/biz/v1/agent/get_thread         -> run_list + tool calls + PippitAssetID
  POST /api/biz/v1/asset/query              -> no_watermark_video_url.download_url
"""
import io, json, os, re, sys, time, uuid, urllib.parse

C = r"<PROJECT_ROOT>"
sys.path.insert(0, os.path.join(C, "work"))
import _pp_http as H

RATIO_CODE = {"9:16": 3, "16:9": 1, "1:1": 2, "3:4": 4, "4:3": 5, "21:9": 6}
SIZE_RATIO = {"1080x1920": "9:16", "1920x1080": "16:9", "1080x1080": "1:1",
              "720x1280": "9:16", "1280x720": "16:9"}
CREDIT_PER_SEC = 4


def estimate_cost(seconds):
    return int(seconds) * CREDIT_PER_SEC


def _walk_strings(o, out):
    if isinstance(o, dict):
        for v in o.values():
            _walk_strings(v, out)
    elif isinstance(o, list):
        for v in o:
            _walk_strings(v, out)
    elif isinstance(o, str):
        out.append(o)


class PippitError(RuntimeError):
    pass


class PippitAccount:
    def __init__(self, cookies, uid=None, ports=None, tag=None, workspace_id=None, space_id=None):
        self.cookies = dict(cookies or {})
        self.uid = str(uid) if uid else None
        self.tag = tag or ""
        self.pp = H.PP(self.cookies, ports or [22030, 22031, 22032])
        if self.uid:
            self.pp.uid = self.uid
        self.workspace_id = str(workspace_id) if workspace_id else None
        self.space_id = str(space_id) if space_id else None
        self.space_idc = "useast5"

    # ---------- account ----------
    def bootstrap(self, timeout=40):
        """Scrape workspace_id / space_id from the SSR home page."""
        if self.workspace_id and self.space_id:
            return self.workspace_id, self.space_id
        last = None
        for port in self.pp.ports:
            try:
                r = self.pp.s.get(H.PAPI + "/home?lang=en",
                                  headers={"User-Agent": H.UA},
                                  proxies=H._proxies(port), timeout=timeout,
                                  verify=False, allow_redirects=True)
                if r.status_code != 200:
                    last = "HTTP%d" % r.status_code
                    continue
                html = r.text
                def pick(k):
                    v = re.findall(r'\\?"%s\\?"\s*:\s*\\?"?(\d+)' % k, html)
                    return v[0] if v else None
                ws, sp = pick("workspace_id"), pick("space_id")
                if ws and sp:
                    self.workspace_id, self.space_id = ws, sp
                    self.pp.last_port = port
                    return ws, sp
                last = "not_in_html(%d)" % len(html)
            except Exception as e:
                last = repr(e)[:120]
        raise PippitError("bootstrap failed: %s" % last)

    def credit(self):
        return self.pp.credit()

    def claim_all(self, max_tasks=200, sleep=1.2, stop_at=340):
        """Finish+claim every live reward task. Returns (before, after)."""
        before = self.credit()["total"]
        if before >= stop_at:
            return before, before
        try:
            j = self.pp.req("/api/biz/v1/reward_task/list?task_type=1&version=v2",
                            None, method="GET", tries=1, timeout=25).json()
        except Exception:
            return before, self.credit()["total"]
        tasks = (j.get("data") or {}).get("reward_tasks") or []
        done = 0
        for x in tasks:
            if done >= max_tasks:
                break
            tid = x.get("task_id")
            if tid is None or int(tid) == 20002:
                continue
            if x.get("task_status") not in (0, 1):
                continue
            for path in ("/api/biz/v1/reward_task/finish", "/api/biz/v1/reward_task/claim"):
                try:
                    self.pp.req(path, {"task_id": tid}, tries=1, timeout=20)
                except Exception:
                    pass
            done += 1
            time.sleep(sleep)
            if self.credit()["total"] >= stop_at:
                break
        return before, self.credit()["total"]

    # ---------- submit ----------
    def submit(self, prompt, seconds=30, resolution="1080p", ratio="9:16",
               title=None, language="en", extra_client_extra=None, extra_parts=None):
        self.bootstrap()
        umid = str(uuid.uuid4())
        rid = str(uuid.uuid4())
        tid = str(uuid.uuid4())
        # Platform quirk: requesting video_resolution=1080p caps output at ~19s.
        # Sending 720p (the web UI default) still renders 1080x1920 but unlocks 30s.
        wire_res = os.environ.get("PP_WIRE_RES") or "720p"
        settings = {
            "resolution": "2K",
            "video_resolution": wire_res,
            "video_duration": int(seconds),
            "ratio": RATIO_CODE.get(ratio, 3),
        }
        babi = {
            "user_message_id": umid, "section_id": rid,
            "scene_lv1": "ai_agent", "scene_lv2": "front_tool",
            "tool_id": "integrated_agent", "sub_tool_id": "integrated_agent",
            "edit_type": "integrated_agent", "enter_from": "home",
        }
        client_extra = {
            "edittype": "integrated_agent", "edit_type": "integrated_agent",
            "entrance_from": "home_page_integrated_agent_combo_input_box",
            "generate_type": "generate", "generate_position": "home",
            "generate_mode": "agent", "model_type": "auto",
            "set_tool_type": "integrated_agent", "canvas_type": "none_canvas",
            "sub_tool_id": "integrated_agent",
        }
        if extra_client_extra:
            client_extra.update(extra_client_extra)
        run_extra = {"babi_param": dict(babi, generate_id=rid), "client_extra": client_extra}
        content = [{"type": "data", "data": json.dumps(settings, separators=(",", ":")),
                    "sub_type": "biz/general_agent_settings"}]
        if extra_parts:
            content.extend(extra_parts)
        content.append({"type": "text", "data": prompt, "sub_type": "plain/text"})
        body = {
            "message": {
                "role": "user",
                "content": content,
                "thread_id": tid, "run_id": rid, "message_id": umid,
                "created_at": int(time.time() * 1000),
            },
            "user_info": {
                "language": language, "consumer_uid": self.uid,
                "workspace_id": self.workspace_id, "space_id": self.space_id,
                "space_idc": self.space_idc,
            },
            "agent_name": "pippit_nest_agent", "entrance_from": "home",
            "run_extra": json.dumps(run_extra, separators=(",", ":")),
        }
        path = "/api/biz/v1/agent/submit_run?babi_param=" + urllib.parse.quote(
            json.dumps(babi, separators=(",", ":")))
        r = self.pp.req(path, body, timeout=90)
        try:
            j = r.json()
        except Exception:
            raise PippitError("submit bad json: %s" % r.text[:300])
        if str(j.get("ret")) != "0":
            raise PippitError("submit ret=%s errmsg=%s" % (j.get("ret"), j.get("errmsg")))
        run = (j.get("data") or {}).get("run") or {}
        if not run.get("thread_id"):
            raise PippitError("submit no thread: %s" % json.dumps(j)[:300])
        return run

    # ---------- poll ----------
    def get_thread(self, thread_id):
        r = self.pp.req("/api/biz/v1/agent/get_thread",
                        {"scopes": ["run_list.entry_list"], "thread_id": thread_id}, timeout=45)
        j = r.json()
        if str(j.get("ret")) != "0":
            raise PippitError("get_thread ret=%s" % j.get("ret"))
        return j

    @staticmethod
    def extract_asset_id(thread_json):
        """Find PippitAssetID from direct fields or nested tool_call_resp JSON.

        User messages are stripped first: the server echoes the input reference
        asset (PippitAssetID) inside the user message, which must never be
        mistaken for the generated result.
        """
        found = []

        def strip_user(o):
            if isinstance(o, dict):
                if str(o.get("role") or "") == "user":
                    return {}
                return {k: strip_user(v) for k, v in o.items()}
            if isinstance(o, list):
                return [strip_user(v) for v in o]
            return o

        thread_json = strip_user(thread_json)

        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if k == "pippit_asset_id" and v:
                        found.append(str(v))
                    elif k == "PippitAssetID" and v:
                        found.append(str(v))
                    else:
                        walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
            elif isinstance(o, str):
                s = o.strip()
                if "PippitAssetID" in s and s[:1] in "{[":
                    try:
                        walk(json.loads(s))
                    except Exception:
                        m = re.search(r'PippitAssetID\\?"\s*:\s*\\?"?(\d+)', s)
                        if m:
                            found.append(m.group(1))
        walk(thread_json)
        return found[-1] if found else None

    @staticmethod
    def run_state(thread_json):
        runs = (((thread_json.get("data") or {}).get("thread") or {}).get("run_list") or [])
        if not runs:
            return None, ""
        run = runs[-1]
        text = []
        for e in run.get("entry_list") or []:
            m = e.get("message") or {}
            for c in m.get("content") or []:
                if c.get("type") == "text" and c.get("data"):
                    text.append(str(c["data"]))
                if c.get("sub_type") == "tool_call_resp" and c.get("data"):
                    text.append(str(c["data"]))
        return run.get("state"), "\n".join(text)[-2000:]

    def wait(self, thread_id, timeout=2400, poll=8, on_progress=None):
        """Poll until a PippitAssetID appears. Returns (asset_id, thread_json)."""
        t0 = time.time()
        idle_final = 0
        while time.time() - t0 < timeout:
            try:
                j = self.get_thread(thread_id)
            except Exception as e:
                if on_progress:
                    on_progress("poll_error", str(e)[:200])
                time.sleep(poll)
                continue
            aid = self.extract_asset_id(j)
            state, text = self.run_state(j)
            if on_progress:
                on_progress(state, aid, text)
            if aid:
                return aid, j
            low = (text or "").lower()
            if ('''success": false''' in text or "could not be completed" in low
                    or "error_code" in low or "temporary service" in low
                    or "could not be prepared" in low):
                raise PippitError("run failed: %s" % text[-400:])
            if state == 4:
                raise PippitError("run failed state=4: %s" % text[-400:])
            if state == 3:
                idle_final += 1
                if idle_final >= 3:
                    raise PippitError("run finished without asset: %s" % text[-400:])
            else:
                idle_final = 0
            time.sleep(poll)
        raise PippitError("timeout waiting for asset")

    # ---------- asset ----------
    def asset(self, asset_id):
        assets, raw = self.pp.asset(asset_id)
        return assets, raw

    def clean_url(self, asset_id):
        assets, _ = self.asset(asset_id)
        if not assets:
            raise PippitError("asset %s not found" % asset_id)
        a = assets[0]
        v = a.get("Video") or {}
        nw = v.get("no_watermark_video_url") or {}
        url = nw.get("download_url") or v.get("origin_url") or ""
        if not url:
            raise PippitError("asset %s has no video url" % asset_id)
        return url, a

    def download(self, url, dest):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        return self.pp.download_fast(url, dest)

    def run_video(self, prompt, dest, seconds=30, resolution="1080p", ratio="9:16",
                  timeout=2400, on_progress=None, extra_parts=None):
        """Full one-shot pipeline. Returns metadata dict."""
        cost = estimate_cost(seconds)
        c0 = self.credit()["total"]
        if c0 < cost:
            raise PippitError("no credit: %d < %d" % (c0, cost))
        run = self.submit(prompt, seconds, resolution, ratio, extra_parts=extra_parts)
        tid, rid = run["thread_id"], run["run_id"]
        if on_progress:
            on_progress("submitted", tid, rid)
        aid, thread = self.wait(tid, timeout=timeout, poll=8, on_progress=on_progress)
        url, meta = self.clean_url(aid)
        n = self.download(url, dest)
        c1 = self.credit()["total"]
        return {"thread_id": tid, "run_id": rid, "asset_id": aid, "url": url,
                "file": dest, "bytes": n, "credit_before": c0, "credit_after": c1,
                "asset_name": (meta or {}).get("Name"), "asset": meta}