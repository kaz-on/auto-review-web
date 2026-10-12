"""GitHub REST API のラッパ。

urllib を直接使っているので、urlopen を差し替えて要求と応答の扱いを確かめる。
401 のときだけトークンを取り直して 1 回再試行する、という制御が要。
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

from webreview import config, github_api
from webreview.github_api import ApiError

from conftest import FakeProvider


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _http_error(code: int, payload) -> urllib.error.HTTPError:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return urllib.error.HTTPError(
        "https://api.github.com/x", code, "err", {}, io.BytesIO(body)
    )


@pytest.fixture
def capture_requests(monkeypatch):
    """urlopen を差し替え、投げた Request と返す応答を制御する。"""
    sent: list[urllib.request.Request] = []
    responses: list = [FakeResponse(b"{}")]

    def fake_urlopen(req, timeout=None):
        sent.append(req)
        result = responses.pop(0) if responses else FakeResponse(b"{}")
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return sent, responses


# --- 低レベル ---------------------------------------------------------------
def test_必要なヘッダを付けて呼び出す(capture_requests):
    sent, responses = capture_requests
    responses[:] = [FakeResponse(b'{"ok": true}')]

    data = github_api.api_request("GET", "/repos/o/r", authorization="Bearer t")

    assert data == {"ok": True}
    req = sent[0]
    assert req.full_url == f"{config.GITHUB_API_URL}/repos/o/r"
    assert req.get_method() == "GET"
    assert req.get_header("Authorization") == "Bearer t"
    # 既定バージョンの変更で挙動が変わらないよう明示する。
    assert req.get_header("X-github-api-version") == "2022-11-28"
    assert req.get_header("Accept") == "application/vnd.github+json"
    assert req.get_header("User-agent") == "auto-review"


def test_ボディがあれば_json_として送る(capture_requests):
    sent, _ = capture_requests

    github_api.api_request(
        "POST", "/repos/o/r/x", authorization="Bearer t", body={"event": "COMMENT"}
    )

    req = sent[0]
    assert req.get_method() == "POST"
    assert json.loads(req.data) == {"event": "COMMENT"}
    assert req.get_header("Content-type") == "application/json"


def test_ボディが無ければ_content_type_を付けない(capture_requests):
    sent, _ = capture_requests

    github_api.api_request("GET", "/x", authorization="Bearer t")

    assert sent[0].data is None
    assert sent[0].get_header("Content-type") is None


def test_絶対_url_はそのまま使う(capture_requests):
    sent, _ = capture_requests

    github_api.api_request(
        "GET", "https://ghe.example.com/api/v3/x", authorization="Bearer t"
    )

    assert sent[0].full_url == "https://ghe.example.com/api/v3/x"


def test_空の応答は_none(capture_requests):
    _, responses = capture_requests
    responses[:] = [FakeResponse(b"")]

    assert github_api.api_request("POST", "/x", authorization="Bearer t") is None


def test_json_でない応答は解釈できないと伝える(capture_requests):
    _, responses = capture_requests
    responses[:] = [FakeResponse(b"<html>maintenance</html>")]

    with pytest.raises(ApiError, match="解釈できません"):
        github_api.api_request("GET", "/x", authorization="Bearer t")


def test_http_エラーは本文のメッセージを添えて伝える(capture_requests):
    _, responses = capture_requests
    responses[:] = [_http_error(422, {
        "message": "Validation Failed",
        "errors": [{"message": "body is too long"}],
    })]

    with pytest.raises(ApiError) as e:
        github_api.api_request("POST", "/x", authorization="Bearer t")

    assert e.value.status == 422
    assert "HTTP 422" in str(e.value)
    assert "Validation Failed" in str(e.value)
    assert "- body is too long" in str(e.value)


def test_http_エラーの本文が_json_でなくても伝える(capture_requests):
    _, responses = capture_requests
    responses[:] = [_http_error(500, b"<html>oops</html>")]

    with pytest.raises(ApiError, match="HTTP 500") as e:
        github_api.api_request("GET", "/x", authorization="Bearer t")
    # 詳細が取れないときに末尾のコロンだけが残らないようにする。
    assert not str(e.value).endswith(": ")


def test_接続できないときは理由を添えて伝える(capture_requests):
    _, responses = capture_requests
    responses[:] = [urllib.error.URLError("name resolution failed")]

    with pytest.raises(ApiError, match="接続できません") as e:
        github_api.api_request("GET", "/x", authorization="Bearer t")
    assert e.value.status is None


# --- トークン付きの呼び出し --------------------------------------------------
def test_401_ならトークンを取り直して一度だけ再試行する(capture_requests):
    sent, responses = capture_requests
    responses[:] = [_http_error(401, {"message": "Bad credentials"}),
                    FakeResponse(b'{"ok": true}')]
    provider = FakeProvider("expired", "fresh")

    data = github_api._request_with_token(provider, "GET", "/x")

    assert data == {"ok": True}
    assert provider.invalidated == 1
    assert [r.get_header("Authorization") for r in sent] == [
        "Bearer expired", "Bearer fresh",
    ]


def test_再試行も_401_なら諦める(capture_requests):
    _, responses = capture_requests
    responses[:] = [_http_error(401, {}), _http_error(401, {})]
    provider = FakeProvider("a", "b")

    with pytest.raises(ApiError):
        github_api._request_with_token(provider, "GET", "/x")
    assert provider.invalidated == 1


@pytest.mark.parametrize("status", [403, 404, 500])
def test_401_以外は再試行しない(capture_requests, status):
    sent, responses = capture_requests
    responses[:] = [_http_error(status, {"message": "no"})]
    provider = FakeProvider("t")

    with pytest.raises(ApiError):
        github_api._request_with_token(provider, "GET", "/x")

    assert len(sent) == 1
    assert provider.invalidated == 0


# --- 高レベル ---------------------------------------------------------------
def test_pr_の_base_head_タイトル_本文を取得する(capture_requests):
    sent, responses = capture_requests
    responses[:] = [FakeResponse(json.dumps({
        "base": {"ref": "main", "sha": "b" * 40},
        "head": {"sha": "h" * 40},
        "title": "タイトル", "body": "本文",
    }).encode())]

    pull = github_api.get_pull(FakeProvider("t"), "example-org", "my-repo", 42)

    assert (pull.base_ref, pull.title, pull.body) == ("main", "タイトル", "本文")
    # レビュー対象のコミットはブランチ名から引き直さず、この SHA で確定させる。
    assert (pull.base_sha, pull.head_sha) == ("b" * 40, "h" * 40)
    assert sent[0].full_url.endswith("/repos/example-org/my-repo/pulls/42")


def test_pr_本文が_null_でも空文字にする(capture_requests):
    _, responses = capture_requests
    responses[:] = [FakeResponse(json.dumps({
        "base": {"ref": "main", "sha": "b" * 40}, "head": {"sha": "h" * 40},
        "title": None, "body": None,
    }).encode())]

    pull = github_api.get_pull(FakeProvider("t"), "o", "r", 1)

    assert (pull.title, pull.body) == ("", "")


def test_pr_の作成者と変更の規模を取得する(capture_requests):
    _, responses = capture_requests
    responses[:] = [FakeResponse(json.dumps({
        "base": {"ref": "main", "sha": "b" * 40}, "head": {"sha": "h" * 40},
        "title": "t", "body": "", "user": {"login": "alice"},
        "additions": 120, "deletions": 30, "changed_files": 4,
    }).encode())]

    pull = github_api.get_pull(FakeProvider("t"), "o", "r", 1)

    assert pull.author == "alice"
    assert (pull.additions, pull.deletions, pull.changed_files) == (120, 30, 4)


def test_作成者や変更の規模が取れなくてもレビューは止めない(capture_requests):
    _, responses = capture_requests
    responses[:] = [FakeResponse(json.dumps({
        "base": {"ref": "main", "sha": "b" * 40}, "head": {"sha": "h" * 40},
        "title": "t", "body": "", "user": None, "additions": "many",
    }).encode())]

    pull = github_api.get_pull(FakeProvider("t"), "o", "r", 1)

    assert pull.author == ""
    assert (pull.additions, pull.deletions, pull.changed_files) == (None, None, None)


def test_base_ブランチが取れなければ実行しない(capture_requests):
    _, responses = capture_requests
    responses[:] = [FakeResponse(b'{"base": {}}')]

    with pytest.raises(ApiError, match="base ブランチ"):
        github_api.get_pull(FakeProvider("t"), "o", "r", 1)


@pytest.mark.parametrize("payload, missing", [
    ('{"base": {"ref": "main"}, "head": {"sha": "hhh"}}', "base"),
    ('{"base": {"ref": "main", "sha": "bbb"}, "head": {}}', "head"),
])
def test_コミットの_sha_が取れなければ実行しない(capture_requests, payload, missing):
    # 取り違えたコミットをレビューするより、止めて知らせる方を選ぶ。
    _, responses = capture_requests
    responses[:] = [FakeResponse(payload.encode())]

    with pytest.raises(ApiError, match=f"{missing} コミット"):
        github_api.get_pull(FakeProvider("t"), "o", "r", 1)


def test_応答が_pr_の形でなければ実行しない(capture_requests):
    _, responses = capture_requests
    responses[:] = [FakeResponse(b"[]")]

    with pytest.raises(ApiError, match="PR の情報"):
        github_api.get_pull(FakeProvider("t"), "o", "r", 1)


def test_レビューをコメントとして投稿する(capture_requests):
    sent, _ = capture_requests

    github_api.create_review(
        FakeProvider("t"), "example-org", "my-repo", 42, "レビュー本文"
    )

    req = sent[0]
    assert req.full_url.endswith("/repos/example-org/my-repo/pulls/42/reviews")
    # 任意の項目は指定が無ければ送らない (GitHub 側の既定に任せる)。
    assert json.loads(req.data) == {"event": "COMMENT", "body": "レビュー本文"}


def test_行コメントとレビュー対象のコミットを添えて投稿する(capture_requests):
    sent, _ = capture_requests
    comments = [{"path": "a.py", "line": 16, "side": "RIGHT", "body": "指摘"}]

    github_api.create_review(
        FakeProvider("t"), "example-org", "my-repo", 42, "レビュー本文",
        comments=comments, commit_id="deadbeef",
    )

    assert json.loads(sent[0].data) == {
        "event": "COMMENT",
        "body": "レビュー本文",
        "commit_id": "deadbeef",
        "comments": comments,
    }
