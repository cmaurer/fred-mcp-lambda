import json, sys, io, time
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "src"))
import app
app._secret_cache.update(value={"bearer_token":"tok123","fred_api_key":"k"}, loaded_at=time.time()+1e9)

calls=[]
class R(io.BytesIO):
    def __enter__(s): return s
    def __exit__(s,*a): pass
def fake(req, timeout):
    calls.append(req.full_url)
    if "series/observations" in req.full_url:
        return R(json.dumps({"units":"lin","sort_order":"desc","count":2,"observations":[{"date":"2026-10-23","value":"3.05"},{"date":"2026-10-22","value":"."}]}).encode())
    if "series/search" in req.full_url:
        return R(json.dumps({"count":1,"seriess":[{"id":"BAMLH0A0HYM2","title":"HY OAS","notes":"long"}]}).encode())
    raise AssertionError(req.full_url)
app.urllib.request.urlopen = fake

def ev(body, auth="Bearer tok123", method="POST", path="/"):
    h={"Content-Type":"application/json"}
    if auth: h["Authorization"]=auth
    return {"requestContext":{"http":{"method":method}},"rawPath":path,"headers":h,"body":json.dumps(body) if body is not None else None}

r=app.handler(ev({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}},auth=None),None); assert r["statusCode"]==401
r=app.handler(ev({"jsonrpc":"2.0","id":1,"method":"initialize"},auth="Bearer wrong"),None); assert r["statusCode"]==401
r=app.handler(ev(None,path="/.well-known/oauth-protected-resource",method="GET"),None); assert r["statusCode"]==404
r=app.handler(ev({"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18"}}),None)
b=json.loads(r["body"]); assert b["result"]["protocolVersion"]=="2025-06-18", b
r=app.handler(ev({"jsonrpc":"2.0","method":"notifications/initialized"}),None); assert r["statusCode"]==202 and "body" not in r
r=app.handler(ev({"jsonrpc":"2.0","id":2,"method":"tools/list"}),None); tools=json.loads(r["body"])["result"]["tools"]; print(len(tools),"tools")
r=app.handler(ev({"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"fred_get_observations","arguments":{"series_id":"BAMLH0A0HYM2","limit":2}}}),None)
res=json.loads(r["body"])["result"]; print(res); assert not res["isError"]; assert json.loads(res["content"][0]["text"])["observations"][1]["value"] is None
r=app.handler(ev({"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"fred_search_series","arguments":{"search_text":"high yield spread"}}}),None)
print(json.loads(r["body"])["result"]["content"][0]["text"])
r=app.handler(ev({"jsonrpc":"2.0","id":5,"method":"tools/call","params":{"name":"fred_get_series","arguments":{}}}),None)
assert json.loads(r["body"])["result"]["isError"]
r=app.handler(ev({"jsonrpc":"2.0","id":6,"method":"bogus"}),None); assert json.loads(r["body"])["error"]["code"]==-32601
r=app.handler(ev(None,method="GET"),None); assert r["statusCode"]==405
assert all("api_key=k" in c and "file_type=json" in c for c in calls)
print("ALL OK")
