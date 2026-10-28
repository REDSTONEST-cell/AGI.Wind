"""One decisive local deployed-service check; never prints or stores credentials."""
from datetime import datetime,timezone
import json
from pathlib import Path
import time
from urllib.request import Request,build_opener,ProxyHandler
from urllib.error import HTTPError
from uuid import uuid4
import hashlib

root=Path(__file__).resolve().parents[1]
data=root/"data"
port=(data/"gateway-port.txt").read_text().strip()
base="http://127.0.0.1:"+port
token=(data/"access-token.txt").read_text().strip()
identity=json.loads((data/"local-identity.json").read_text(encoding="utf-8"))
scope=identity["scope"]
client=build_opener(ProxyHandler({}))
def request(path,body=None,authenticated=True):
    headers={"Content-Type":"application/json"}
    if authenticated: headers["Authorization"]="Bearer "+token
    req=Request(base+path,data=json.dumps(body,ensure_ascii=False).encode() if body is not None else None,headers=headers)
    try: response=client.open(req,timeout=10)
    except HTTPError as error: response=error
    return response.status,response.read()

assert request("/health/live")[0]==200
assert request("/api/v1/capabilities",authenticated=False)[0]==401
status,raw=request("/health/ready")
readiness=json.loads(raw)
assert status==503 and readiness["dependencies"]["database"]=="available" and readiness["dependencies"]["dialogue"]=="unconfigured"
value={"request_id":str(uuid4()),"conversation_id":scope["conversation_id"],"input":{"text":"本地部署验收：2026-10-25合成测试经历"},"device_id":"local-smoke"}
status,raw=request("/api/v1/chat/requests",value)
assert status==202
accepted=json.loads(raw)
assert json.loads(request("/api/v1/chat/requests",value)[1])["generation_id"]==accepted["generation_id"]
for _ in range(100):
    _,raw=request("/api/v1/generations/"+accepted["generation_id"])
    generation=json.loads(raw)
    if generation["state"] in ("failed","completed"): break
    time.sleep(.1)
assert generation["state"]=="failed" and generation["error_code"]=="MODEL_UNCONFIGURED"
_,raw=request("/api/v1/memory/search",{"companion_id":scope["companion_id"],"conversation_id":scope["conversation_id"],"query":"合成测试经历"})
hits=json.loads(raw)
assert any("合成测试经历" in hit["content"] for hit in hits)
status,raw=request("/api/v1/archive/exports",{"companion_id":scope["companion_id"]})
assert status==201
export=json.loads(raw)
status,package=request(export["download_url"])
assert status==200
report_dir=data/"local-smoke"
report_dir.mkdir(exist_ok=True)
archive_path=report_dir/"local-roundtrip-source.zip"
archive_path.write_bytes(package)
from nagi.archive.imports import ImportService
validated=ImportService.validate(archive_path)
report={"timestamp":datetime.now(timezone.utc).isoformat(),"base_url":base,"live_http":200,"unauthenticated_http":401,"readiness_http":503,"dialogue":"unconfigured_expected","request_idempotent":True,"generation_state":generation["state"],"generation_error":generation["error_code"],"keyword_hits":len(hits),"archive_verified":validated["valid"],"archive_sha256":hashlib.sha256(package).hexdigest(),"records":validated["records"],"real_vendor_or_voice_call":False}
(report_dir/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
print(json.dumps(report,ensure_ascii=False,indent=2))
