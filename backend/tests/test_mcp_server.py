from __future__ import annotations

import asyncio
import io
import json

from buglens import indexer, mcp_server, pipeline, store


def test_list_tools_exposes_six_with_object_schemas():
    tools = mcp_server.list_tools()
    names = {t["name"] for t in tools}
    assert names == {"review", "findings", "recall", "context", "stats", "repos"}
    for t in tools:
        assert t["inputSchema"]["type"] == "object"
        assert isinstance(t["description"], str) and t["description"]

    by = {t["name"]: t for t in tools}
    assert by["review"]["inputSchema"]["required"] == ["owner", "repo", "number"]
    assert by["findings"]["inputSchema"]["required"] == ["repo", "number"]
    assert by["recall"]["inputSchema"]["required"] == ["repo", "path"]
    assert by["context"]["inputSchema"]["required"] == ["repo", "changed"]
    assert by["stats"]["inputSchema"]["required"] == ["repo"]
    assert by["repos"]["inputSchema"]["required"] == []


def test_handle_initialize_list_ping_and_unknown_method():
    init = asyncio.run(mcp_server.handle(
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
    ))
    assert init["id"] == 1
    assert init["result"]["protocolVersion"] == mcp_server.PROTOCOL_VERSION
    assert init["result"]["serverInfo"] == {
        "name": mcp_server.SERVER_NAME, "version": mcp_server.SERVER_VERSION
    }
    assert init["result"]["capabilities"] == {"tools": {}}

    listed = asyncio.run(mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))
    assert len(listed["result"]["tools"]) == 6

    pong = asyncio.run(mcp_server.handle({"jsonrpc": "2.0", "id": 3, "method": "ping"}))
    assert pong["result"] == {}

    bad = asyncio.run(mcp_server.handle({"jsonrpc": "2.0", "id": 4, "method": "nope"}))
    assert bad["error"]["code"] == -32601
    assert "method not found" in bad["error"]["message"]


def test_handle_notification_silent_and_missing_id_answered():
    assert asyncio.run(mcp_server.handle(
        {"jsonrpc": "2.0", "method": "notifications/initialized"}
    )) is None
    resp = asyncio.run(mcp_server.handle({"jsonrpc": "2.0", "method": "ping"}))
    assert resp["id"] is None
    assert resp["result"] == {}


def test_handle_tools_call_requires_name():
    resp = asyncio.run(mcp_server.handle(
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {}}
    ))
    assert resp["error"]["code"] == -32602


def test_call_tool_review_returns_payload_and_defaults_dry_run(monkeypatch):
    seen = {}

    async def fake_review(owner, repo, number, **kwargs):
        seen.update({"owner": owner, "repo": repo, "number": number, **kwargs})
        return pipeline.ReviewResult(
            status="dry-run", score=88, verdict="needs-attention", summary="s",
            findings=[{"path": "a.py", "line": 2, "severity": "major",
                       "lens": "security", "title": "eval on input"}],
            engine_notes=["note"],
        )

    monkeypatch.setattr(pipeline, "review_pr", fake_review)
    out = asyncio.run(mcp_server.call_tool("review", {"owner": "o", "repo": "r", "number": 1}))

    assert out["isError"] is False
    payload = json.loads(out["content"][0]["text"])
    assert payload["status"] == "dry-run"
    assert payload["score"] == 88
    assert payload["engine_notes"] == ["note"]
    assert payload["findings"][0] == {"path": "a.py", "line": 2, "severity": "major",
                                      "lens": "security", "title": "eval on input"}
    assert seen["dry_run"] is True
    assert seen["db"]
    assert seen["owner"] == "o" and seen["repo"] == "r" and seen["number"] == 1


def test_call_tool_findings_recall_and_missing_field(monkeypatch):
    async def fake_for_pr(repo, pr, path=None):
        return [{"path": "a.py", "severity": "major"}]

    async def fake_for_paths(repo, paths, limit=20, path=None):
        return [{"path": paths[0], "title": "prior"}]

    monkeypatch.setattr(store, "findings_for_pr", fake_for_pr)
    monkeypatch.setattr(store, "findings_for_paths", fake_for_paths)

    out = asyncio.run(mcp_server.call_tool("findings", {"repo": "o/r", "number": 3}))
    assert json.loads(out["content"][0]["text"]) == [{"path": "a.py", "severity": "major"}]

    recall = asyncio.run(mcp_server.call_tool("recall", {"repo": "o/r", "path": "a.py"}))
    assert json.loads(recall["content"][0]["text"]) == [{"path": "a.py", "title": "prior"}]

    err = asyncio.run(mcp_server.call_tool("findings", {"repo": "o/r"}))
    assert err["isError"] is True
    assert "number" in err["content"][0]["text"]


def test_call_tool_unknown_tool(monkeypatch):
    out = asyncio.run(mcp_server.call_tool("nope", {}))
    assert out["isError"] is True
    assert out["content"][0]["text"] == "unknown tool: nope"


def test_call_tool_swallows_store_exceptions(monkeypatch):
    async def boom(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(store, "known_repos", boom)
    out = asyncio.run(mcp_server.call_tool("repos", {}))
    assert out["isError"] is True
    assert "kaboom" in out["content"][0]["text"]


def test_call_tool_context_and_stats(monkeypatch):
    async def fake_graph(slug, db):
        return ({"a.py": {}}, {})

    monkeypatch.setattr(pipeline, "_load_graph", fake_graph)
    monkeypatch.setattr(indexer, "related_files", lambda index, graph, changed: ["b.py"])
    monkeypatch.setattr(indexer, "context_pack", lambda index, related: "PACK")

    out = asyncio.run(mcp_server.call_tool("context", {"repo": "o/r", "changed": ["a.py"]}))
    assert json.loads(out["content"][0]["text"]) == {"related": ["b.py"], "context": "PACK"}

    async def fake_stats(path=None):
        return {"runs": 1}

    async def fake_drops(repo, path=None):
        return {"model": 2}

    async def fake_lenses(repo, path=None):
        return {"security": {"raised": 1}}

    monkeypatch.setattr(store, "stats", fake_stats)
    monkeypatch.setattr(store, "drop_stats", fake_drops)
    monkeypatch.setattr(store, "lens_outcome_stats", fake_lenses)

    stats = asyncio.run(mcp_server.call_tool("stats", {"repo": "o/r"}))
    payload = json.loads(stats["content"][0]["text"])
    assert payload["stats"] == {"runs": 1}
    assert payload["drops"] == {"model": 2}
    assert payload["lenses"] == {"security": {"raised": 1}}


def test_serve_reads_lines_and_terminates_on_eof():
    lines = [
        "",
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize"}),
        "not json at all",
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
    ]
    stdin = io.StringIO("\n".join(lines) + "\n")
    stdout = io.StringIO()

    asyncio.run(mcp_server.serve(stdin, stdout))

    out = [line for line in stdout.getvalue().split("\n") if line]
    assert len(out) == 3
    parsed = [json.loads(line) for line in out]
    assert parsed[0]["id"] == 1 and "result" in parsed[0]
    assert parsed[1]["id"] is None and parsed[1]["error"]["code"] == -32700
    assert parsed[2]["id"] == 2 and len(parsed[2]["result"]["tools"]) == 6


def test_serve_output_is_single_line_json():
    stdin = io.StringIO(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}) + "\n")
    stdout = io.StringIO()

    asyncio.run(mcp_server.serve(stdin, stdout))

    raw = stdout.getvalue()
    assert raw.endswith("\n")
    assert raw.count("\n") == 1
    json.loads(raw.strip())
