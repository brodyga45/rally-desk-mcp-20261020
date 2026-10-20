"""Loopback-only Rally Desk MCP server and its real browser MCP client."""
import argparse
from pathlib import Path
import tempfile

from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from starlette.responses import HTMLResponse, JSONResponse
import uvicorn

from domain import Store

ROOT = Path(__file__).resolve().parent


def application(database):
    store = Store(database)
    mcp = MCPServer("Rally Desk", version="0.1.0",
                    instructions="Plan casual doubles club rounds and record their results. Read the session, preview a round, show the proposal, then commit with its expected_version and preview_hash. Every write needs a fresh UUID request_id; retries reuse the same UUID and arguments. Stale writes are rejected. Corrections need an explicit reason. This is a local simulated assistant surface, not a real Alexa or watch integration.")
    read = ToolAnnotations(readOnlyHint=True, idempotentHint=True)
    write = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True)

    @mcp.tool(annotations=write)
    def create_session(players: list[str], courts: int, target: int, request_id: str) -> dict:
        """Create a club night for 4–12 distinct labels, 1–2 courts, target11/15/21, by2, cap30. A retry must reuse request_id and all arguments."""
        return store.create(players, courts, target, request_id)

    @mcp.tool(annotations=read)
    def get_session(session_id: str) -> dict:
        """Read the current version, rounds, completed-game counts and recent audit trail."""
        return store.snapshot(session_id)

    @mcp.tool(annotations=read)
    def plan_next_round(session_id: str) -> dict:
        """Preview a doubles round without writing. Least completed games first, then minimize repeated rests/partners/opponents. Returns explanation and preview_hash. Finish pending games first."""
        return store.plan(session_id)

    @mcp.tool(annotations=write)
    def commit_round(session_id: str, expected_version: int, preview_hash: str, request_id: str) -> dict:
        """Commit the accepted exact preview. Rejects stale versions or altered previews; an identical UUID retry commits once."""
        return store.commit(session_id, expected_version, preview_hash, request_id)

    @mcp.tool(annotations=write)
    def record_result(session_id: str, expected_version: int, match_id: str,
                      a: int, b: int, request_id: str, correction_reason: str = "") -> dict:
        """Record a completed casual score, or correct it with an explicit reason. No points after the first win. Conflicting or stale updates do not overwrite results."""
        return store.report(session_id, expected_version, match_id, a, b, request_id, correction_reason)

    @mcp.custom_route("/", methods=["GET"])
    async def index(request):
        return HTMLResponse((ROOT / "web/index.html").read_text(),
                            headers={"Content-Security-Policy": "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'"})

    @mcp.custom_route("/healthz", methods=["GET"])
    async def health(request):
        return JSONResponse({"name": "Rally Desk", "version": "0.1.0", "mode": "local demo",
                             "actual_Alexa_integration": False, "actual_watch_sync": False})

    return mcp.streamable_http_app(json_response=True, stateless_http=True,
                                   host="127.0.0.1", max_request_body_size=65536)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--database", type=Path,
                        default=Path(tempfile.gettempdir()) / "rally-desk-local/state.sqlite3")
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("Choose a port from1024to65535")
    uvicorn.run(application(args.database), host="127.0.0.1", port=args.port,
                log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
