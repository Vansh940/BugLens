from buglens import github


class FakeResponse:
    def __init__(self, status_code=200, data=None, text=""):
        self.status_code = status_code
        self._data = data if data is not None else {}
        self.text = text

    def json(self):
        return self._data


def recorder(status_code=200, data=None, text=""):
    calls = []

    async def fake_request(method, url, *, params=None, json=None, accept=None):
        calls.append({"method": method, "url": url, "params": params, "json": json})
        return FakeResponse(status_code, data, text)

    return fake_request, calls


def recorder_pages(pages):
    calls = []

    async def fake_request(method, url, *, params=None, json=None, accept=None):
        calls.append({"method": method, "url": url, "params": params, "json": json})
        index = (params or {}).get("page", 1) - 1
        return FakeResponse(200, pages[index] if index < len(pages) else [])

    return fake_request, calls


async def test_list_review_comments_filters_by_review(monkeypatch):
    page = [
        {"id": 1, "node_id": "n1", "path": "a.py", "line": 3, "body": "x",
         "pull_request_review_id": 55},
        {"id": 2, "node_id": "n2", "path": "b.py", "line": 9, "body": "y",
         "pull_request_review_id": 99},
        {"id": 3, "node_id": "n3", "path": "c.py", "line": 4, "body": "z",
         "pull_request_review_id": 55},
    ]
    fake, calls = recorder_pages([page, []])
    monkeypatch.setattr(github, "_request", fake)

    found = await github.list_review_comments("o", "r", 7, 55)

    assert [c["id"] for c in found] == [1, 3]
    assert calls[0]["method"] == "GET"
    assert calls[0]["params"] == {"per_page": 100, "page": 1}


async def test_list_review_comments_raises_on_500(monkeypatch):
    fake, _ = recorder(status_code=500, text="boom")
    monkeypatch.setattr(github, "_request", fake)

    try:
        await github.list_review_comments("o", "r", 7, 55)
    except github.GitHubError as exc:
        assert "500" in str(exc)
    else:
        raise AssertionError("expected GitHubError")


async def test_reply_review_comment_posts_to_replies_url(monkeypatch):
    fake, calls = recorder(status_code=201, data={"id": 10, "body": "hi"})
    monkeypatch.setattr(github, "_request", fake)

    out = await github.reply_review_comment("o", "r", 7, 123, "hi")

    assert out == {"id": 10, "body": "hi"}
    call = calls[0]
    assert call["method"] == "POST"
    assert call["url"].endswith("/pulls/7/comments/123/replies")
    assert call["json"] == {"body": "hi"}


async def test_reply_review_comment_raises_on_404(monkeypatch):
    fake, _ = recorder(status_code=404, text="not found")
    monkeypatch.setattr(github, "_request", fake)

    try:
        await github.reply_review_comment("o", "r", 7, 123, "hi")
    except github.GitHubError as exc:
        assert "404" in str(exc)
    else:
        raise AssertionError("expected GitHubError")


async def test_update_review_comment_patches_and_raises(monkeypatch):
    fake, calls = recorder(status_code=200, data={"id": 5, "body": "new"})
    monkeypatch.setattr(github, "_request", fake)

    out = await github.update_review_comment("o", "r", 5, "new")

    assert out == {"id": 5, "body": "new"}
    assert calls[0]["method"] == "PATCH"
    assert calls[0]["url"].endswith("/pulls/comments/5")
    assert calls[0]["json"] == {"body": "new"}

    fake2, _ = recorder(status_code=422, text="bad")
    monkeypatch.setattr(github, "_request", fake2)
    try:
        await github.update_review_comment("o", "r", 5, "new")
    except github.GitHubError as exc:
        assert "422" in str(exc)
    else:
        raise AssertionError("expected GitHubError")


async def test_delete_review_comment_204_true_404_false_403_raises(monkeypatch):
    fake, calls = recorder(status_code=204)
    monkeypatch.setattr(github, "_request", fake)
    assert await github.delete_review_comment("o", "r", 5) is True
    assert calls[0]["method"] == "DELETE"
    assert calls[0]["url"].endswith("/pulls/comments/5")

    fake2, _ = recorder(status_code=404, text="gone")
    monkeypatch.setattr(github, "_request", fake2)
    assert await github.delete_review_comment("o", "r", 5) is False

    fake3, _ = recorder(status_code=403, text="forbidden")
    monkeypatch.setattr(github, "_request", fake3)
    try:
        await github.delete_review_comment("o", "r", 5)
    except github.GitHubError as exc:
        assert "403" in str(exc)
    else:
        raise AssertionError("expected GitHubError")


async def test_minimize_comment_true_on_good_payload(monkeypatch):
    seen = {}

    async def fake_graphql(query, variables):
        seen["query"] = query
        seen["variables"] = variables
        return {"minimizeComment": {"minimizedComment": {"isMinimized": True}}}

    monkeypatch.setattr(github, "graphql", fake_graphql)

    assert await github.minimize_comment("NODE", "OUTDATED") is True
    assert seen["variables"] == {"id": "NODE", "classifier": "OUTDATED"}
    assert "minimizeComment" in seen["query"]


async def test_minimize_comment_false_when_graphql_raises(monkeypatch):
    async def fake_graphql(query, variables):
        raise github.GitHubError("graphql 500: boom")

    monkeypatch.setattr(github, "graphql", fake_graphql)

    assert await github.minimize_comment("NODE") is False


async def test_minimize_comment_false_on_errors_payload(monkeypatch):
    async def fake_graphql(query, variables):
        return {"errors": [{"message": "nope"}]}

    monkeypatch.setattr(github, "graphql", fake_graphql)

    assert await github.minimize_comment("NODE") is False
