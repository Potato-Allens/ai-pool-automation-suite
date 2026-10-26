# -*- coding: utf-8 -*-
"""refs.py - pure-HTTP reference material upload for Pippit.

Chain (reverse-engineered 2026-10-18):
  1 POST /lv/v1/asset/prepare_upload_cloud   (init)
  2 POST /lv/v1/asset/prepare_upload_cloud   (+md5/size/file_type) -> STS + upload_domain + service_id + upload_id
  3 GET  https://{upload_domain}/?Action=ApplyUploadInner...  (AWS SigV4 with STS) -> StoreUri + Auth + SessionKey
  4 POST https://{upload_host}/upload/v1/{StoreUri}  (binary, Authorization: {Auth})
  5 POST https://{upload_domain}/?Action=CommitUploadInner  {SessionKey}
  6 POST /lv/v1/asset/create_cloud_asset   -> cloud_file_entry.entry_id
  7 POST /api/biz/v3/asset/create_v2 {asset_source_type:3, asset_source_id:entry_id, asset_type} -> PippitAssetID
"""
import hashlib, hmac, io, json, mimetypes, os, random, string, sys, time, urllib.parse, zlib
import requests

C = r"<PROJECT_ROOT>"
sys.path.insert(0, os.path.join(C, "work"))
import _pp_http as H

ASSET_TYPE = {"image": 1, "video": 2, "audio": 3}
FILE_TYPE = {1: "image", 2: "video", 3: "audio"}


def _sha256_hex(b):
    return hashlib.sha256(b).hexdigest()


def _hmac(key, msg):
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _sign(sts, method, host, path, params, service="vod", region="i18n", body=b""):
    """AWS SigV4 variant used by ttuploader (headers joined by spaces)."""
    now = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    date = now[:8]
    headers = {"X-Amz-Date": now}
    if body:
        headers["X-Amz-Content-Sha256"] = _sha256_hex(body)
    tok = sts.get("session_token") or sts.get("SessionToken")
    if tok:
        headers["x-amz-security-token"] = tok
    canon_headers = "\n".join("%s:%s" % (k.lower(), str(v).strip()) for k, v in sorted(headers.items(), key=lambda x: x[0].lower()))
    signed = ";".join(k.lower() for k in sorted(headers.keys(), key=str.lower))
    q = "&".join("%s=%s" % (urllib.parse.quote(str(k), safe="-_.~"),
                            urllib.parse.quote(str(v), safe="-_.~"))
                 for k, v in sorted(params.items()) if v is not None)
    canonical = "\n".join([method.upper(), path, q, canon_headers + "\n", signed, _sha256_hex(body)])
    scope = "%s/%s/%s/aws4_request" % (date, region, service)
    sts_str = "\n".join(["AWS4-HMAC-SHA256", now, scope, _sha256_hex(canonical.encode("utf-8"))])
    k = _hmac(("AWS4" + sts["secret_access_key"]).encode("utf-8"), date)
    k = _hmac(k, region); k = _hmac(k, service); k = _hmac(k, "aws4_request")
    sig = hmac.new(k, sts_str.encode("utf-8"), hashlib.sha256).hexdigest()
    headers["Authorization"] = ("AWS4-HMAC-SHA256 Credential=%s/%s, SignedHeaders=%s, Signature=%s"
                                % (sts["access_key_id"], scope, signed, sig))
    return headers, q


def _md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def upload_asset(pp, path, workspace_id, asset_type=None, timeout=120, log=print):
    """Upload a local file and return dict(pippit_asset_id, asset_id, uri, metadata, type)."""
    path = os.path.abspath(path)
    size = os.path.getsize(path)
    md5 = _md5_file(path)
    name = os.path.basename(path)
    mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
    if asset_type is None:
        if mime.startswith("image/"):
            asset_type = 1
        elif mime.startswith("video/"):
            asset_type = 2
        elif mime.startswith("audio/"):
            asset_type = 3
        else:
            raise ValueError("unsupported mime: %s" % mime)
    ftype = FILE_TYPE[asset_type]

    # 1+2. prepare
    pp.req("/lv/v1/asset/prepare_upload_cloud",
           {"space_id": "0", "workspace_id": workspace_id, "is_web_user": True}, timeout=40)
    j = pp.req("/lv/v1/asset/prepare_upload_cloud",
               {"workspace_id": workspace_id, "space_id": "0", "md5": md5, "size": size,
                "file_type": ftype, "is_web_user": True}, timeout=40).json()
    d = j.get("data") or {}
    upload_id = d.get("upload_id")
    # the web uploader calls flushToken() after init and signs with the LAST token
    j3 = pp.req("/lv/v1/asset/prepare_upload_cloud",
                {"space_id": "0", "workspace_id": workspace_id, "is_web_user": True}, timeout=40).json()
    d3 = j3.get("data") or {}
    sts = d3.get("security_token") or d.get("security_token") or {}
    sts["access_key_id"] = sts.get("access_key_id") or sts.get("AccessKeyId")
    sts["secret_access_key"] = sts.get("secret_access_key") or sts.get("SecretAccessKey")
    sts["session_token"] = sts.get("session_token") or sts.get("SessionToken")
    upload_domain = d3.get("upload_domain") or d.get("upload_domain")
    service_id = d3.get("service_id") or d.get("service_id")
    everphoto_uid = str(d3.get("everphoto_user_id") or d3.get("everphoto_uid") or d.get("everphoto_user_id") or d.get("everphoto_uid"))
    if not (upload_domain and sts.get("session_token")):
        raise RuntimeError("prepare_upload_cloud bad: %s" % json.dumps(j)[:400])
    log("prepare ok domain=%s service=%s upload_id=%s" % (upload_domain, service_id, upload_id))

    # 3. ApplyUploadInner
    service = "vod"
    params = {"Action": "ApplyUploadInner", "Version": "2020-11-19", "SpaceName": service_id,
              "FileType": "object", "IsInner": 1, "FileSize": size,
              "s": "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(10)),
              "device_platform": "web"}
    hdr, q = _sign(sts, "GET", upload_domain, "/", params, service=service)
    url = "https://%s/?%s" % (upload_domain, q)
    r = pp.s.get(url, headers=hdr, timeout=timeout, verify=False, proxies=H._proxies(None))
    if r.status_code != 200:
        raise RuntimeError("ApplyUploadInner HTTP%d %s" % (r.status_code, r.text[:300]))
    res = r.json().get("Result") or {}
    addr = res.get("UploadAddress")
    if addr:
        node = addr
        store = (addr.get("StoreInfos") or [{}])[0]
        hosts = addr.get("UploadHosts") or []
        upload_host = hosts[0] if hosts else upload_domain
        session_key = addr.get("SessionKey")
    else:
        node = ((res.get("InnerUploadAddress") or {}).get("UploadNodes") or [{}])[0]
        store = (node.get("StoreInfos") or [{}])[0]
        upload_host = node.get("UploadHost") or upload_domain
        session_key = node.get("SessionKey")
    store_uri, auth = store.get("StoreUri"), store.get("Auth")
    if not (store_uri and auth):
        raise RuntimeError("ApplyUploadInner no store: %s" % json.dumps(res)[:400])
    log("apply ok host=%s uri=%s" % (upload_host, store_uri))

    # 4. upload bytes
    up_url = "https://%s/upload/v1/%s" % (upload_host, store_uri)
    with open(path, "rb") as f:
        data = f.read()
    uh = {"Authorization": auth, "Content-Type": "application/octet-stream",
          "Content-CRC32": "%08x" % (zlib.crc32(data) & 0xFFFFFFFF),
          "X-Storage-U": urllib.parse.quote(everphoto_uid, safe="")}
    last = None
    r = None
    for attempt in range(3):
        for port in [None] + list(pp.ports or []):
            try:
                r = pp.s.post(up_url, data=data, headers=uh, timeout=min(timeout, 30),
                              verify=False, proxies=H._proxies(port))
                if r.status_code == 200:
                    break
                last = "HTTP%d %s" % (r.status_code, r.text[:200])
            except Exception as e:
                last = repr(e)[:200]
                r = None
        if r is not None and r.status_code == 200:
            break
        if attempt < 2:
            log("upload retry %d/2 after: %s" % (attempt + 1, str(last)[:160]))
            time.sleep(1.5 * (attempt + 1))
    if r is None or r.status_code != 200:
        raise RuntimeError("upload failed: %s" % last)
    log("upload ok", r.text[:160])

    # 5. commit
    cparams = {"Action": "CommitUploadInner", "Version": "2020-11-19", "SpaceName": service_id}
    cbody = json.dumps({"SessionKey": session_key, "Functions": []}, separators=(",", ":")).encode("utf-8")
    hdr2, q2 = _sign(sts, "POST", upload_domain, "/", cparams, service=service, body=cbody)
    hdr2["Content-Type"] = "text/plain;charset=UTF-8"
    lastc = None
    r = None
    for attempt in range(3):
        for port in [None] + list(pp.ports or []):
            try:
                r = pp.s.post("https://%s/?%s" % (upload_domain, q2), data=cbody,
                              headers=hdr2, timeout=min(timeout, 30), verify=False, proxies=H._proxies(port))
                if r.status_code == 200 and '"Error"' not in r.text:
                    break
                lastc = "HTTP%d %s" % (r.status_code, r.text[:200])
            except Exception as e:
                lastc = repr(e)[:200]; r = None
        if r is not None and r.status_code == 200 and '"Error"' not in r.text:
            break
        if attempt < 2:
            log("commit retry %d/2 after: %s" % (attempt + 1, str(lastc)[:160]))
            time.sleep(1.5 * (attempt + 1))
    if r is None:
        raise RuntimeError("commit failed: %s" % lastc)
    if r.status_code != 200:
        raise RuntimeError("commit HTTP%d %s" % (r.status_code, r.text[:300]))
    log("commit ok", r.text[:200])

    # 6. create_cloud_asset
    meta = json.dumps({"uploadSource": {"owner": str(getattr(pp, "uid", "") or ""), "platform": "browser",
                                        "systemVersion": H.UA, "appVersion": "1.0.6"}})
    body = {"everphoto_id": everphoto_uid,
            "asset": {"uri": store_uri, "size": size, "workspace_id": workspace_id,
                      "filename": name, "upload_id": upload_id, "if_image_async_resize": True,
                      "permission": 0, "space_id": "0", "file_type": ftype, "folder_id": "-1",
                      "meta": meta, "md5": md5},
            "is_web_user": True}
    r = pp.req("/lv/v1/asset/create_cloud_asset", body, timeout=60)
    jj = r.json()
    data = jj.get("data") or {}
    entry = data.get("cloud_file_entry") or {}
    cloud_asset = data.get("cloud_asset") or {}
    entry_id = cloud_asset.get("asset_id") or entry.get("entry_id")
    log("cloud resp", r.text[:900])
    if not entry_id:
        raise RuntimeError("create_cloud_asset bad: %s" % r.text[:400])
    log("cloud asset ok entry=%s" % entry_id)

    # 7. create_v2
    r = pp.req("/api/biz/v3/asset/create_v2",
               {"asset_source_type": 3, "asset_source_id": str(entry_id),
                "asset_type": asset_type, "name": name}, timeout=60).json()
    aid = (r.get("data") or {}).get("PippitAssetID")
    if not aid:
        raise RuntimeError("create_v2 bad: %s" % json.dumps(r)[:400])
    log("create_v2 ok pippit_asset_id=%s" % aid)
    pb = probe_media(path)
    return {"pippit_asset_id": str(aid), "asset_id": str(entry_id), "uri": store_uri,
            "name": name, "type": ftype, "mime": mime, "size": size, "md5": md5,
            "asset_type": asset_type, "width": pb.get("width") or 0,
            "height": pb.get("height") or 0, "duration": pb.get("duration") or 0.0,
            "format": (os.path.splitext(name)[1] or "").lstrip(".").lower()}


def probe_media(path):
    """Local probe (ffprobe) -> {width,height,duration,format}. Safe fallbacks."""
    out = {"width": 0, "height": 0, "duration": 0.0,
           "format": (os.path.splitext(path)[1] or "").lstrip(".").lower()}
    try:
        import subprocess
        cmd = ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path]
        flags = 0x08000000 if os.name == "nt" else 0
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30,
                           creationflags=flags)
        if r.returncode == 0:
            j = json.loads(r.stdout or "{}")
            st = (j.get("streams") or [{}])[0] or {}
            fm = j.get("format") or {}
            out["width"] = int(st.get("width") or 0)
            out["height"] = int(st.get("height") or 0)
            out["duration"] = float(fm.get("duration") or st.get("duration") or 0.0)
            if fm.get("format_name"):
                out["format_name"] = fm["format_name"]
    except Exception:
        pass
    return out


def make_part(info, asset_url=None, metadata=None):
    """Build the submit_run content part using the live-captured shape (2026-10-18).

    image: {"image":{"name","url","metadata":{md5,name,width,height,size},"asset_id"}}
    video: {"video":{"name","url","metadata":{md5,name,width,height,duration_ms},"asset_id"}}
    audio: {"audio":{"name","url","metadata":{md5,format,duration,size},"asset_id",
                     "security_check_scene":[]}}

    NOTE: asset_id must be the EntryID returned by create_cloud_asset
    (data.cloud_asset.asset_id), NOT the PippitAssetID.
    """
    t = info.get("type")
    md = dict(metadata or {})
    aid = str(info.get("asset_id") or "")
    name = info.get("name") or ""
    url = asset_url or ""
    size = int(info.get("size") or 0)
    width = int(md.get("width") or info.get("width") or 0)
    height = int(md.get("height") or info.get("height") or 0)
    dur = float(md.get("duration") or info.get("duration") or 0.0)
    if t == "image":
        image = {"name": name, "url": url,
                 "metadata": {"md5": info.get("md5") or "", "name": name,
                              "width": width, "height": height, "size": size},
                 "asset_id": aid}
        return {"type": "data", "sub_type": "biz/x_data_upload_image",
                "data": json.dumps({"image": image}, separators=(",", ":"))}
    if t == "video":
        video = {"name": name, "url": url,
                 "metadata": {"md5": info.get("md5") or "", "name": name,
                              "width": width, "height": height,
                              "duration_ms": int(round(dur * 1000))},
                 "asset_id": aid}
        return {"type": "data", "sub_type": "biz/x_data_upload_video",
                "data": json.dumps({"video": video}, separators=(",", ":"))}
    if t == "audio":
        fmt = (md.get("format") or info.get("format") or "mp3").lstrip(".")
        audio = {"name": name, "url": url,
                 "metadata": {"md5": info.get("md5") or "", "format": fmt,
                              "duration": round(dur, 3), "size": size},
                 "asset_id": aid, "security_check_scene": []}
        return {"type": "data", "sub_type": "biz/x_data_upload_audio",
                "data": json.dumps({"audio": audio}, separators=(",", ":"))}
    raise ValueError("unsupported type: %r" % (t,))


def asset_meta(pp, pippit_asset_id):
    """Return (url, metadata dict) from /api/biz/v1/asset/detail. Never raises."""
    try:
        j = pp.req("/api/biz/v1/asset/detail", {"PippitAssetID": str(pippit_asset_id)},
                   tries=2, timeout=40).json()
    except Exception:
        return "", {}
    arr = j.get("data") or []
    a = arr[0] if isinstance(arr, list) and arr else (arr if isinstance(arr, dict) else None)
    if not isinstance(a, dict):
        return "", {}
    for key in ("Image", "Video", "Audio"):
        obj = a.get(key) or {}
        if obj.get("url") or obj.get("metadata"):
            m = dict(obj.get("metadata") or {})
            m["uri"] = (a.get("AssetOption") or {}).get("imagex_uri") or obj.get("uri") or ""
            return obj.get("url") or "", m
    return "", {}
