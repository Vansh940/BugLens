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


async def test_create_check_run_without_annotations(monkeypatch):
    fake, calls = recorder(status_code=201, data={"id": 1})
    monkeypatch.setattr(github, "_request", fake)

    await github.create_check_run("o", "r", "sha", "BugLens", "neutral", "Title", "Summary")

    payload = calls[0]["json"]
    assert payload["status"] == "completed"
    assert payload["conclusion"] == "neutral"
    assert payload["output"]["title"] == "Title"
    assert payload["output"]["summary"] == "Summary"
    assert "annotations" not in payload["output"]
    assert "text" not in payload["output"]


async def test_create_check_run_with_annotations_and_text(monkeypatch):
    fake, calls = recorder(status_code=201, data={"id": 1})
    monkeypatch.setattr(github, "_request", fake)

    anns = [
        {"path": "a.py", "start_line": 3, "end_line": 3,
         "annotation_level": "warning", "message": "m", "title": "t"},
    ]
    await github.create_check_run("o", "r", "sha", "BugLens", "neutral", "Title", "Summary",
                                  text="body", annotations=anns)

    payload = calls[0]["json"]
    assert payload["output"]["annotations"] == anns
    assert payload["output"]["text"] == "body"


async def test_create_check_run_403_raises(monkeypatch):
    fake, _ = recorder(status_code=403, text="Resource not accessible by integration")
    monkeypatch.setattr(github, "_request", fake)

    try:
        await github.create_check_run("o", "r", "sha", "BugLens", "neutral", "Title", "Summary")
    except github.GitHubError as exc:
        assert "403" in str(exc)
    else:
        raise AssertionError("expected GitHubError")


async def test_update_check_run_patches(monkeypatch):
    fake, calls = recorder(status_code=200, data={"id": 42})
    monkeypatch.setattr(github, "_request", fake)

    anns = [{"path": "b.py", "start_line": 9, "end_line": 9,
             "annotation_level": "notice", "message": "m"}]
    await github.update_check_run("o", "r", 42, status="completed", conclusion="failure",
                                  annotations=anns)

    call = calls[0]
    assert call["method"] == "PATCH"
    assert call["url"].endswith("/check-runs/42")
    assert call["json"]["conclusion"] == "failure"
    assert call["json"]["output"]["annotations"] == anns


async def test_find_check_run_returns_first(monkeypatch):
    run = {"id": 7, "name": "BugLens", "status": "completed"}
    fake, calls = recorder(status_code=200, data={"check_runs": [run, {"id": 8}]})
    monkeypatch.setattr(github, "_request", fake)

    found = await github.find_check_run("o", "r", "sha", "BugLens")

    assert found == run
    assert calls[0]["method"] == "GET"
    assert calls[0]["params"] == {"check_name": "BugLens", "filter": "latest", "per_page": 1}


async def test_find_check_run_returns_none_on_403(monkeypatch):
    fake, _ = recorder(status_code=403, text="forbidden")
    monkeypatch.setattr(github, "_request", fake)

    assert await github.find_check_run("o", "r", "sha", "BugLens") is None


async def test_find_check_run_returns_none_on_empty_list(monkeypatch):
    fake, _ = recorder(status_code=200, data={"check_runs": []})
    monkeypatch.setattr(github, "_request", fake)

    assert await github.find_check_run("o", "r", "sha", "BugLens") is None
