"""FRED MCP server for AWS Lambda (function URL).

Stateless MCP "streamable HTTP" transport: every request is one HTTP POST
carrying a JSON-RPC message, answered with a plain JSON response (no SSE,
no sessions). Standard library + boto3 (bundled in the Lambda runtime) only.

Auth: the request must carry `Authorization: Bearer <token>`, where the token
and the FRED API key live in one Secrets Manager secret (JSON):
    {"bearer_token": "...", "fred_api_key": "..."}
"""

import hmac
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger()
log.setLevel(logging.INFO)

SERVER_NAME = "fred-mcp"
SERVER_VERSION = "1.0.0"
SUPPORTED_PROTOCOLS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
FRED_BASE = "https://api.stlouisfed.org/fred"
SECRET_TTL_SECONDS = 300

_secret_cache = {"value": None, "loaded_at": 0.0}


# --------------------------------------------------------------------------
# Secrets
# --------------------------------------------------------------------------
def _get_secret():
    now = time.time()
    if _secret_cache["value"] and now - _secret_cache["loaded_at"] < SECRET_TTL_SECONDS:
        return _secret_cache["value"]
    import boto3  # bundled in the Lambda runtime

    client = boto3.client("secretsmanager")
    raw = client.get_secret_value(SecretId=os.environ["SECRET_ARN"])["SecretString"]
    value = json.loads(raw)
    _secret_cache.update(value=value, loaded_at=now)
    return value


# --------------------------------------------------------------------------
# FRED API
# --------------------------------------------------------------------------
class FredError(Exception):
    pass


def _fred(path, params):
    api_key = _get_secret().get("fred_api_key", "")
    if not api_key or api_key == "REPLACE_ME":
        raise FredError("FRED API key is not configured in the secret yet.")
    query = {k: v for k, v in params.items() if v is not None and v != ""}
    query.update(api_key=api_key, file_type="json")
    url = f"{FRED_BASE}/{path}?{urllib.parse.urlencode(query)}"
    last_err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": f"{SERVER_NAME}/{SERVER_VERSION}"})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            try:
                msg = json.loads(body).get("error_message", body)
            except ValueError:
                msg = body
            last_err = FredError(f"FRED HTTP {e.code}: {msg}")
            if e.code not in (429, 500, 502, 503, 504):
                raise last_err
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = FredError(f"FRED request failed: {e}")
        time.sleep(1.5 * (attempt + 1))
    raise last_err


def _series_brief(s):
    keys = ("id", "title", "frequency_short", "units_short", "seasonal_adjustment_short",
            "observation_start", "observation_end", "last_updated", "popularity")
    return {k: s.get(k) for k in keys if k in s}


# --------------------------------------------------------------------------
# Tools
# --------------------------------------------------------------------------
def t_search_series(a):
    d = _fred("series/search", {
        "search_text": a["search_text"],
        "limit": a.get("limit", 20),
        "order_by": a.get("order_by", "search_rank"),
        "filter_variable": a.get("filter_variable"),
        "filter_value": a.get("filter_value"),
    })
    return {"count": d.get("count"), "series": [_series_brief(s) for s in d.get("seriess", [])]}


def t_get_series(a):
    d = _fred("series", {"series_id": a["series_id"]})
    return d.get("seriess", [{}])[0]


def t_get_observations(a):
    d = _fred("series/observations", {
        "series_id": a["series_id"],
        "observation_start": a.get("observation_start"),
        "observation_end": a.get("observation_end"),
        "units": a.get("units"),
        "frequency": a.get("frequency"),
        "aggregation_method": a.get("aggregation_method"),
        "sort_order": a.get("sort_order", "desc"),
        "limit": a.get("limit", 120),
    })
    obs = [{"date": o["date"], "value": None if o["value"] == "." else o["value"]}
           for o in d.get("observations", [])]
    return {
        "series_id": a["series_id"],
        "units": d.get("units"),
        "sort_order": d.get("sort_order"),
        "count_total": d.get("count"),
        "returned": len(obs),
        "observations": obs,
    }


def t_get_series_tags(a):
    d = _fred("series/tags", {"series_id": a["series_id"]})
    return [{"name": t.get("name"), "group": t.get("group_id")} for t in d.get("tags", [])]


def t_get_series_updates(a):
    d = _fred("series/updates", {
        "limit": a.get("limit", 50),
        "filter_value": a.get("filter_value", "all"),
    })
    return [_series_brief(s) for s in d.get("seriess", [])]


def t_search_by_tags(a):
    tags = a["tag_names"]
    if isinstance(tags, list):
        tags = ";".join(tags)
    d = _fred("tags/series", {
        "tag_names": tags,
        "limit": a.get("limit", 20),
        "order_by": a.get("order_by", "popularity"),
        "sort_order": "desc",
    })
    return {"count": d.get("count"), "series": [_series_brief(s) for s in d.get("seriess", [])]}


def t_get_category(a):
    cid = a.get("category_id", 0)
    cat = _fred("category", {"category_id": cid}).get("categories", [])
    kids = _fred("category/children", {"category_id": cid}).get("categories", [])
    return {"category": cat[0] if cat else None,
            "children": [{"id": c["id"], "name": c["name"]} for c in kids]}


def t_get_category_series(a):
    d = _fred("category/series", {
        "category_id": a["category_id"],
        "limit": a.get("limit", 50),
        "order_by": a.get("order_by", "popularity"),
        "sort_order": "desc",
    })
    return {"count": d.get("count"), "series": [_series_brief(s) for s in d.get("seriess", [])]}


def t_list_releases(a):
    d = _fred("releases", {"limit": a.get("limit", 100), "order_by": a.get("order_by", "release_id")})
    return [{"id": r["id"], "name": r["name"]} for r in d.get("releases", [])]


def t_get_release_series(a):
    d = _fred("release/series", {
        "release_id": a["release_id"],
        "limit": a.get("limit", 50),
        "order_by": a.get("order_by", "popularity"),
        "sort_order": "desc",
    })
    return {"count": d.get("count"), "series": [_series_brief(s) for s in d.get("seriess", [])]}


def _schema(props, required=()):
    return {"type": "object", "properties": props, "required": list(required), "additionalProperties": False}


_LIMIT = {"type": "integer", "minimum": 1, "maximum": 1000}
_SID = {"type": "string", "description": "FRED series ID, e.g. BAMLH0A0HYM2, CPIAUCSL, T10Y2Y"}

TOOLS = {
    "fred_search_series": (t_search_series, "Search FRED series by keywords.", _schema({
        "search_text": {"type": "string"},
        "limit": _LIMIT,
        "order_by": {"type": "string", "enum": ["search_rank", "popularity", "last_updated", "title"]},
        "filter_variable": {"type": "string", "enum": ["frequency", "units", "seasonal_adjustment"]},
        "filter_value": {"type": "string"},
    }, ["search_text"])),
    "fred_get_series": (t_get_series, "Get metadata for one FRED series (title, units, frequency, last updated, notes).",
                        _schema({"series_id": _SID}, ["series_id"])),
    "fred_get_observations": (t_get_observations,
        "Get data points for a FRED series. Defaults to the 120 most recent observations, newest first; "
        "set sort_order='asc' and observation_start for history.", _schema({
        "series_id": _SID,
        "observation_start": {"type": "string", "description": "YYYY-MM-DD"},
        "observation_end": {"type": "string", "description": "YYYY-MM-DD"},
        "units": {"type": "string", "enum": ["lin", "chg", "ch1", "pch", "pc1", "pca", "cch", "cca", "log"],
                  "description": "lin=levels, pc1=% change from year ago, pch=% change, chg=change"},
        "frequency": {"type": "string", "enum": ["d", "w", "bw", "m", "q", "sa", "a"]},
        "aggregation_method": {"type": "string", "enum": ["avg", "sum", "eop"]},
        "sort_order": {"type": "string", "enum": ["asc", "desc"]},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100000},
    }, ["series_id"])),
    "fred_get_series_tags": (t_get_series_tags, "Get the tags attached to a FRED series.",
                             _schema({"series_id": _SID}, ["series_id"])),
    "fred_get_series_updates": (t_get_series_updates, "List FRED series updated most recently.", _schema({
        "limit": _LIMIT,
        "filter_value": {"type": "string", "enum": ["all", "macro", "regional"]},
    })),
    "fred_search_by_tags": (t_search_by_tags, "Find FRED series matching ALL of the given tags.", _schema({
        "tag_names": {"oneOf": [{"type": "array", "items": {"type": "string"}}, {"type": "string"}],
                      "description": "e.g. ['usa','inflation','monthly'] or 'usa;inflation'"},
        "limit": _LIMIT,
        "order_by": {"type": "string", "enum": ["popularity", "last_updated", "title", "series_id"]},
    }, ["tag_names"])),
    "fred_get_category": (t_get_category, "Get a FRED category and its child categories (0 = root).",
                          _schema({"category_id": {"type": "integer", "minimum": 0}})),
    "fred_get_category_series": (t_get_category_series, "List series in a FRED category.", _schema({
        "category_id": {"type": "integer", "minimum": 0},
        "limit": _LIMIT,
        "order_by": {"type": "string", "enum": ["popularity", "last_updated", "title", "series_id"]},
    }, ["category_id"])),
    "fred_list_releases": (t_list_releases, "List FRED data releases (e.g. CPI, Employment Situation).", _schema({
        "limit": _LIMIT,
        "order_by": {"type": "string", "enum": ["release_id", "name", "press_release"]},
    })),
    "fred_get_release_series": (t_get_release_series, "List series belonging to a FRED release.", _schema({
        "release_id": {"type": "integer", "minimum": 1},
        "limit": _LIMIT,
        "order_by": {"type": "string", "enum": ["popularity", "last_updated", "title", "series_id"]},
    }, ["release_id"])),
}


# --------------------------------------------------------------------------
# JSON-RPC / MCP
# --------------------------------------------------------------------------
def _result(mid, result):
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def _error(mid, code, message):
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


def _handle_message(msg):
    """Returns a response dict, or None for notifications."""
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        if isinstance(msg, dict) and ("result" in msg or "error" in msg):
            return None  # a client response to a server request; we never send any
        return _error(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")

    method, mid, params = msg["method"], msg.get("id"), msg.get("params") or {}
    is_notification = "id" not in msg

    if method.startswith("notifications/"):
        return None
    if method == "initialize":
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOLS else SUPPORTED_PROTOCOLS[0]
        return _result(mid, {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            "instructions": "Federal Reserve Economic Data (FRED). Use fred_search_series to find series IDs, "
                            "then fred_get_observations for values. Values are strings; null means missing.",
        })
    if method == "ping":
        return _result(mid, {})
    if method == "tools/list":
        return _result(mid, {"tools": [
            {"name": name, "description": desc, "inputSchema": schema,
             "annotations": {"readOnlyHint": True, "openWorldHint": True}}
            for name, (_, desc, schema) in TOOLS.items()
        ]})
    if method == "tools/call":
        name = params.get("name")
        if name not in TOOLS:
            return _error(mid, -32602, f"Unknown tool: {name}")
        fn = TOOLS[name][0]
        try:
            data = fn(params.get("arguments") or {})
            return _result(mid, {"content": [{"type": "text", "text": json.dumps(data, separators=(",", ":"))}],
                                 "isError": False})
        except KeyError as e:
            return _result(mid, {"content": [{"type": "text", "text": f"Missing required argument: {e}"}],
                                 "isError": True})
        except FredError as e:
            return _result(mid, {"content": [{"type": "text", "text": str(e)}], "isError": True})
        except Exception:  # noqa: BLE001
            log.exception("tool %s failed", name)
            return _result(mid, {"content": [{"type": "text", "text": "Internal error calling FRED."}],
                                 "isError": True})
    if method == "fred/source_sha256":
        # Deployment check: hash of the deployed source, to compare with the local copy.
        import hashlib
        with open(__file__, encoding="utf-8") as f:
            digest = hashlib.sha256(f.read().strip().encode("utf-8")).hexdigest()
        return _result(mid, {"sha256": digest})
    if method in ("resources/list", "prompts/list"):
        key = method.split("/")[0]
        return _result(mid, {key: []})
    if is_notification:
        return None
    return _error(mid, -32601, f"Method not found: {method}")


def _http(status, body=None, extra_headers=None):
    headers = {"Cache-Control": "no-store"}
    if extra_headers:
        headers.update(extra_headers)
    resp = {"statusCode": status, "headers": headers}
    if body is not None:
        headers["Content-Type"] = "application/json"
        resp["body"] = json.dumps(body, separators=(",", ":"))
    return resp


def _health():
    """Unauthenticated readiness probe, also the target of the weekday warm-up schedule.

    Does the same cold-start work as a real MCP request -- module imports, importing boto3,
    creating the Secrets Manager client and loading the secret -- so the execution environment
    it runs in is ready for the scheduled task's first call. Returns no data beyond ok/not ok.
    """
    try:
        _get_secret()
    except Exception:  # noqa: BLE001
        log.exception("health: secret load failed")
        return _http(503, {"ok": False})
    return _http(200, {"ok": True})


def _authorized(headers):
    supplied = headers.get("authorization", "")
    if not supplied.lower().startswith("bearer "):
        return False
    expected = _get_secret().get("bearer_token", "")
    return bool(expected) and hmac.compare_digest(supplied[7:].strip().encode(), expected.encode())


def handler(event, context):
    http = event.get("requestContext", {}).get("http", {})
    method = http.get("method", "GET").upper()
    path = event.get("rawPath", "/")
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}

    # No OAuth here; let discovery probes fail fast instead of looking like auth challenges.
    if path.startswith("/.well-known/"):
        return _http(404, {"error": "not_found"})
    if path.rstrip("/") == "/health":
        return _health()

    if not _authorized(headers):
        return _http(401, {"error": "unauthorized"})

    if method == "GET":
        # No server-initiated SSE stream in stateless mode.
        return _http(405, None, {"Allow": "POST"})
    if method == "DELETE":
        return _http(200)
    if method != "POST":
        return _http(405, None, {"Allow": "POST"})

    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        import base64
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        payload = json.loads(raw)
    except ValueError:
        return _http(400, _error(None, -32700, "Parse error"))

    if isinstance(payload, list):
        responses = [r for r in (_handle_message(m) for m in payload) if r is not None]
        return _http(200, responses) if responses else _http(202)
    response = _handle_message(payload)
    return _http(200, response) if response is not None else _http(202)
