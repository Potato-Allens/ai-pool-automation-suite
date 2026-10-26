import io, json, time, urllib.request
BASE="http://127.0.0.1:8787"
key=io.open("_smoke_key.txt",encoding="ascii").read().strip()
adm=io.open("admin_token.txt",encoding="utf-8").read().strip()
def req(path, method="GET", body=None, admin=False):
    h={"Content-Type":"application/json"}
    h["X-Admin-Token" if admin else "Authorization"] = adm if admin else "Bearer "+key
    data=json.dumps(body).encode() if body is not None else None
    r=urllib.request.Request(BASE+path,data=data,headers=h,method=method)
    with urllib.request.urlopen(r,timeout=30) as x: return json.loads(x.read())
print("usage", req("/v1/usage"))
print("pool", req("/v1/pool/status", admin=True))
prompt=io.open(r"<PROJECT_ROOT>\work\prompt_prod_coffee6_30.txt",encoding="utf-8").read().strip()
j=req("/v1/videos/generations","POST",{"prompt":prompt,"model":"seedance-2.5","resolution":"1080p","duration":30,"ratio":"9:16"})
print("submitted", j["id"], j["status"], "progress", j["progress"])
io.open("_smoke_job.txt","w",encoding="utf-8").write(j["id"])
for i in range(9):
    time.sleep(20)
    s=req("/v1/videos/generations/"+j["id"])
    print("%02d %s progress=%s err=%s" % (i, s["status"], s["progress"], (s.get("error") or "")[:80]))
    if s["status"] in ("succeeded","failed","canceled"): break
