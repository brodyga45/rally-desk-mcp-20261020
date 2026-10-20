"""Exercise the real loopback MCP endpoint; all labels/results are synthetic."""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid

ROOT = Path(__file__).resolve().parent
REPORT = ROOT / "http-validation-v1.json"
report = {"started_at": datetime.now(timezone.utc).isoformat(), "requests": [],
          "synthetic_data": True, "actual_Stack": "MCP Python SDK2.3.0 via loopbackStreamableHTTP",
          "new_model_calls": 0, "new_cash_spend": 0,
          "sources_sha256": {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in (ROOT / "domain.py", ROOT / "server.py", ROOT / "web/index.html")}}


def rpc(method, params, notification=False, origin=None):
    body = {"jsonrpc": "2.0", "method": method, "params": params}
    if not notification:
        body["id"] = len(report["requests"]) + 1
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
               "MCP-Protocol-Version": "2025-11-25"}
    if origin:
        headers["Origin"] = origin
    request = Request("http://127.0.0.1:8767/mcp", data=json.dumps(body).encode(), headers=headers)
    start = time.monotonic()
    try:
        with urlopen(request, timeout=15) as response:
            status, raw = response.status, response.read().decode()
    except HTTPError as error:
        status, raw = error.code, error.read().decode()
    try:
        result = json.loads(raw) if raw else None
    except json.JSONDecodeError:
        result = {"body": raw}
    report["requests"].append({"method": method, "params": params, "status": status,
                               "response": result, "seconds": round(time.monotonic() - start, 4)})
    if origin:
        return status
    assert status in (200, 202), (status, raw)
    assert not result or "error" not in result, result
    return result.get("result") if result else None


def tool(name, args, expect_error=False):
    result = rpc("tools/call", {"name": name, "arguments": args})
    assert bool(result.get("isError")) == expect_error, result
    if expect_error:
        return result
    return result.get("structuredContent") or json.loads("".join(c["text"] for c in result["content"] if c["type"] == "text"))


def request_id():
    return str(uuid.uuid4())


try:
    init = rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                              "clientInfo": {"name": "RallyDeskValidation", "version": "0.1.0"}})
    assert init["protocolVersion"] == "2025-11-25"
    rpc("notifications/initialized", {}, notification=True)
    tools = rpc("tools/list", {})
    assert {t["name"] for t in tools["tools"]} == {"create_session", "get_session", "plan_next_round", "commit_round", "record_result"}
    state = tool("create_session", {"players": [f"HTTP{i}" for i in range(8)],
                                    "courts": 2, "target": 11, "request_id": request_id()})["state"]
    session_id = state["session_id"]
    plan = tool("plan_next_round", {"session_id": session_id})
    state = tool("commit_round", {"session_id": session_id, "expected_version": 1,
                                   "preview_hash": plan["preview_hash"], "request_id": request_id()})["state"]
    assert len(state["matches"]) == 2
    arguments = {"session_id": session_id, "expected_version": 2, "match_id": "R1C1",
                 "a": 11, "b": 7, "request_id": request_id()}
    first = tool("record_result", arguments)
    repeated = tool("record_result", arguments)
    assert first["state"] == repeated["state"] and repeated["replayed"]
    tool("record_result", {**arguments, "match_id": "R1C2", "request_id": request_id()}, expect_error=True)
    state = tool("get_session", {"session_id": session_id})
    assert state["version"] == 3 and state["matches"][1]["status"] == "pending"
    state = tool("record_result", {**arguments, "match_id": "R1C2", "expected_version": 3,
                                    "request_id": request_id()})["state"]
    tool("record_result", {**arguments, "expected_version": 4, "a": 30, "b": 4,
                           "request_id": request_id(), "correction_reason": "Invalid score probe"}, expect_error=True)
    state = tool("record_result", {**arguments, "expected_version": 4, "a": 7, "b": 11,
                                    "request_id": request_id(), "correction_reason": "Synthetic sides correction"})["state"]
    assert state["version"] == 5 and set(state["completed_games"].values()) == {1}
    assert state["audit"][-1]["old_score"] == [11, 7] and state["audit"][-1]["new_score"] == [7, 11]
    assert rpc("tools/list", {}, origin="https://untrusted.example") in (400, 403)
    report.update(passed=True, session_id=session_id, final_version=state["version"],
                  checks=["initialize2025-11-25", "initialized notification", "5tools discovered",
                          "actual planning/commit/results", "retry exactly once", "stale write rejected",
                          "prematurely completed score rejected", "correction audit", "foreign origin rejected"])
except Exception as error:
    report.update(passed=False, error_type=type(error).__name__, error=str(error))
finally:
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    REPORT.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "requests"}), flush=True)
    print("HTTP requests:", len(report["requests"]), flush=True)
raise SystemExit(0 if report["passed"] else 1)
