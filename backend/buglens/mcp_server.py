"""Local Model Context Protocol server over stdio (Wave J).

Exposes BugLens' read-only review data to an MCP client (Claude Desktop, Cursor)
using the JSON-RPC 2.0 handshake implemented by hand with the standard library
only. No ``mcp`` dependency, no third-party package, no network: every tool reads
from the existing engine store and the ``review`` tool is dry-run by default so it
never posts to GitHub.

One asyncio event loop serves the whole session so the aiosqlite connection pool
is never shared across loops.
"""
from __future__ import annotations

import asyncio
import json
import sys

from . import indexer, pipeline, store

PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "buglens"
SERVER_VERSION = "0.1.0"


def list_tools() -> list:
    """The six read-only MCP tools with JSON-Schema input contracts."""
    return [
        {
            "name": "review",
            "description": (
                "Run a BugLens review of a pull request (dry-run by default, never posts) "
                "and return the status, score, verdict, summary, engine notes and findings."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "owner": {"type": "string", "description": "Repository owner."},
                    "repo": {"type": "string", "description": "Repository name."},
                    "number": {"type": "integer", "description": "Pull request number."},
                    "dry_run": {"type": "boolean", "default": True,
                                "description": "When true, nothing is posted to GitHub."},
                },
                "required": ["owner", "repo", "number"],
            },
        },
        {
            "name": "findings",
            "description": "Posted findings for a pull request, keyed by the owner/name slug.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository slug, owner/name."},
                    "number": {"type": "integer", "description": "Pull request number."},
                },
                "required": ["repo", "number"],
            },
        },
        {
            "name": "recall",
            "description": "Prior posted findings BugLens raised on a single file path.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository slug, owner/name."},
                    "path": {"type": "string", "description": "File path to look up."},
                },
                "required": ["repo", "path"],
            },
        },
        {
            "name": "context",
            "description": (
                "Related files and a compact context pack for a set of changed paths, "
                "derived from the persistent code graph."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository slug, owner/name."},
                    "changed": {"type": "array", "items": {"type": "string"},
                                "description": "Changed file paths."},
                },
                "required": ["repo", "changed"],
            },
        },
        {
            "name": "stats",
            "description": "Store counters, verifier drop reasons and per-lens feedback for a repo.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "repo": {"type": "string", "description": "Repository slug, owner/name."},
                },
                "required": ["repo"],
            },
        },
        {
            "name": "repos",
            "description": "Repositories this store has reviewed, with run and resolution counters.",
            "inputSchema": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    ]


_REQUIRED = {
    "review": ("owner", "repo", "number"),
    "findings": ("repo", "number"),
    "recall": ("repo", "path"),
    "context": ("repo", "changed"),
    "stats": ("repo",),
    "repos": (),
}


def _text_result(text: str, *, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _json_result(payload) -> dict:
    return _text_result(json.dumps(payload, default=str))


def _missing(arguments: dict, required: tuple) -> str | None:
    for key in required:
        if key not in arguments or arguments.get(key) is None:
            return key
    return None


async def call_tool(name: str, arguments: dict) -> dict:
    """Invoke one tool by name, returning an MCP result envelope. Never raises."""
    args = arguments if isinstance(arguments, dict) else {}
    try:
        if name not in _REQUIRED:
            return _text_result(f"unknown tool: {name}", is_error=True)
        missing = _missing(args, _REQUIRED[name])
        if missing is not None:
            return _text_result(f"missing required argument: {missing}", is_error=True)

        if name == "review":
            result = await pipeline.review_pr(
                args["owner"], args["repo"], args["number"],
                dry_run=bool(args.get("dry_run", True)), db=store.db_path(),
            )
            payload = {
                "status": result.status,
                "score": result.score,
                "verdict": result.verdict,
                "summary": result.summary,
                "engine_notes": list(result.engine_notes or []),
                "findings": [
                    {
                        "path": f.get("path"),
                        "line": f.get("line"),
                        "severity": f.get("severity"),
                        "lens": f.get("lens"),
                        "title": f.get("title"),
                    }
                    for f in (result.findings or [])
                ],
            }
            return _json_result(payload)

        if name == "findings":
            return _json_result(
                await store.findings_for_pr(args["repo"], args["number"], path=store.db_path())
            )

        if name == "recall":
            return _json_result(
                await store.findings_for_paths(args["repo"], [args["path"]], path=store.db_path())
            )

        if name == "context":
            changed = args["changed"]
            if not isinstance(changed, list):
                changed = [changed]
            index, graph = await pipeline._load_graph(args["repo"], store.db_path())
            related = indexer.related_files(index, graph, changed)
            pack = indexer.context_pack(index, related)
            return _json_result({"related": related, "context": pack})

        if name == "stats":
            repo = args["repo"]
            return _json_result({
                "stats": await store.stats(store.db_path()),
                "drops": await store.drop_stats(repo, store.db_path()),
                "lenses": await store.lens_outcome_stats(repo, store.db_path()),
            })

        return _json_result(await store.known_repos(store.db_path()))
    except Exception as exc:
        return _text_result(str(exc), is_error=True)


def _rpc_result(msg_id, result) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def _rpc_error(msg_id, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


async def handle(message: dict) -> dict | None:
    """Turn one parsed JSON-RPC request into a response, or None for a notification."""
    msg_id = None
    try:
        if not isinstance(message, dict):
            return _rpc_error(None, -32600, "invalid request")
        msg_id = message.get("id")
        method = message.get("method")
        params = message.get("params")

        if isinstance(method, str) and method.startswith("notifications/"):
            return None
        if method == "initialize":
            return _rpc_result(msg_id, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            })
        if method == "ping":
            return _rpc_result(msg_id, {})
        if method == "tools/list":
            return _rpc_result(msg_id, {"tools": list_tools()})
        if method == "tools/call":
            if not isinstance(params, dict):
                return _rpc_error(msg_id, -32602, "invalid params")
            name = params.get("name")
            if not isinstance(name, str) or not name:
                return _rpc_error(msg_id, -32602, "invalid params: missing tool name")
            return _rpc_result(msg_id, await call_tool(name, params.get("arguments")))
        return _rpc_error(msg_id, -32601, f"method not found: {method}")
    except Exception as exc:
        return _rpc_error(msg_id, -32603, str(exc))


async def serve(stdin=None, stdout=None) -> None:
    """Read newline-delimited JSON-RPC from ``stdin``, write responses to ``stdout``."""
    stdin = stdin if stdin is not None else sys.stdin
    stdout = stdout if stdout is not None else sys.stdout
    while True:
        line = await asyncio.to_thread(stdin.readline)
        if line == "":
            break
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except Exception:
            response = _rpc_error(None, -32700, "parse error")
        else:
            response = await handle(message)
        if response is not None:
            stdout.write(json.dumps(response) + "\n")
            stdout.flush()


def main() -> None:
    asyncio.run(serve())
