"""GitLab の forge 実装と API ラッパ。

値の形は GitLab CE のテスト用インスタンスで確かめたものに合わせてある
(MR の diff_refs、作った直後は null、changes_count は文字列、サブグループの
パスはエンコードが要る)。
"""

from __future__ import annotations

import io
import json
import urllib.error
import urllib.request

import pytest

from webreview import config, forge, gitlab_api, gitlab_forge, reviews
from webreview.errors import ReviewError
from webreview.gitlab_forge import GitLabForge
from webreview.review import parse_pr_url

WEB = "http://gitlab.example.local:8929"


@pytest.fixture(autouse=True)
def gitlab(monkeypatch):
    monkeypatch.setattr(config, "REVIEW_FORGE", "gitlab")
    monkeypatch.setattr(config, "GITLAB_URL", WEB)
    monkeypatch.setattr(config, "GITLAB_TOKEN", "glpat-test")
    monkeypatch.setattr(gitlab_forge, "_sleep", lambda _s: None)


def _mr(**kw):
    data = {
        "iid": 1,
        "title": "MR のタイトル",
        "description": "MR の本文",
        "target_branch": "main",
        "diff_refs": {"base_sha": "b" * 40, "head_sha": "h" * 40, "start_sha": "s" * 40},
        "changes_count": "3",
        "author": {"username": "alice"},
    }
    data.update(kw)
    return data


# --- forge の選択 -------------------------------------------------------------
def test_review_forge_で_gitlab_を選ぶ():
    assert isinstance(forge.get(), GitLabForge)


def test_知らない_forge_は分かる形で失敗する(monkeypatch):
    monkeypatch.setattr(config, "REVIEW_FORGE", "bitbucket")

    with pytest.raises(ReviewError, match="REVIEW_FORGE"):
        forge.get()


def test_gitlab_url_が無ければ分かる形で失敗する(monkeypatch):
    monkeypatch.setattr(config, "GITLAB_URL", "")

    with pytest.raises(ReviewError, match="GITLAB_URL"):
        GitLabForge()


def test_トークンが無ければ認証の確認で知らせる(monkeypatch):
    monkeypatch.setattr(config, "GITLAB_TOKEN", "")

    with pytest.raises(ReviewError, match="GITLAB_TOKEN"):
        GitLabForge().auth_summary()


# --- URL ----------------------------------------------------------------------
@pytest.mark.parametrize("url, expected", [
    (f"{WEB}/group/project/-/merge_requests/1", ("group", "project", 1)),
    (f"{WEB}/group/sub/project/-/merge_requests/12", ("group/sub", "project", 12)),
    (f"{WEB}/group/project/-/merge_requests/1/diffs", ("group", "project", 1)),
    (f"{WEB}/group/project/-/merge_requests/1#note_5", ("group", "project", 1)),
    (f"{WEB}/group/project/-/merge_requests/1?tab=diffs", ("group", "project", 1)),
])
def test_mr_の_url_を分解する(url, expected):
    assert GitLabForge().parse_pr_url(url) == expected


@pytest.mark.parametrize("url", [
    f"{WEB}/project/-/merge_requests/1",          # グループが無い
    f"{WEB}/group/project/merge_requests/1",      # /-/ の無い古い形
    f"{WEB}/group/project/-/issues/1",
    "http://gitlab.example.local:89290/group/project/-/merge_requests/1",
    "https://github.com/group/project/pull/1",
])
def test_mr_の_url_でなければ分解しない(url):
    assert GitLabForge().parse_pr_url(url) is None


def test_url_を組み立てて_origin_から分解できる():
    host = GitLabForge()

    assert host.pr_url("group/sub", "project", 3) == (
        f"{WEB}/group/sub/project/-/merge_requests/3"
    )
    assert host.pr_url_pattern() == f"{WEB}/<group>/<project>/-/merge_requests/<番号>"
    clone = host.clone_url("group/sub", "project")
    assert clone == f"{WEB}/group/sub/project.git"
    assert host.parse_remote_url(clone) == ("group/sub", "project")
    assert host.parse_remote_url("https://github.com/group/project.git") is None


def test_git_の認証はユーザー名を問わないので_oauth2_にする():
    credential = GitLabForge().git_credential("group")

    assert (credential.url, credential.username, credential.password) == (
        WEB, "oauth2", "glpat-test",
    )


# --- オーナーの制約 -----------------------------------------------------------
def test_許可したグループの下のサブグループも通す(monkeypatch):
    """グループアクセストークンの効く範囲と揃える。"""
    monkeypatch.setattr(config, "REVIEW_OWNERS", ("group",))

    pr = parse_pr_url(f"{WEB}/group/sub/project/-/merge_requests/1")

    assert (pr.owner, pr.repo) == ("group/sub", "project")
    assert pr.url == f"{WEB}/group/sub/project/-/merge_requests/1"


@pytest.mark.parametrize("owner", ["group-other", "other/group", "Group"])
def test_名前が前方一致するだけの別のグループは通さない(monkeypatch, owner):
    monkeypatch.setattr(config, "REVIEW_OWNERS", ("group",))

    with pytest.raises(ReviewError, match="いずれでもありません"):
        parse_pr_url(f"{WEB}/{owner}/project/-/merge_requests/1")


def test_サブグループだけを許可すれば親の別のプロジェクトは通さない(monkeypatch):
    monkeypatch.setattr(config, "REVIEW_OWNERS", ("group/sub",))

    with pytest.raises(ReviewError, match="いずれでもありません"):
        parse_pr_url(f"{WEB}/group/project/-/merge_requests/1")


def test_作業ディレクトリの外を指すパスは弾く(monkeypatch):
    monkeypatch.setattr(config, "REVIEW_OWNERS", ("group",))

    with pytest.raises(ReviewError, match="GitLab の PR URL 形式ではありません"):
        parse_pr_url(f"{WEB}/group/../project/-/merge_requests/1")


# --- MR の情報 ----------------------------------------------------------------
def test_mr_の情報を_diff_refs_から読む(monkeypatch):
    monkeypatch.setattr(gitlab_api, "get_merge_request", lambda *a: _mr())

    pull = GitLabForge().get_pull("group", "project", 1)

    # 比較の起点は merge-base (GitLab が MR の差分として表示する範囲)。
    assert (pull.base_ref, pull.base_sha, pull.head_sha) == ("main", "b" * 40, "h" * 40)
    assert (pull.title, pull.body, pull.author) == ("MR のタイトル", "MR の本文", "alice")
    # 行数は返らない。変更ファイル数は文字列で返る。
    assert (pull.additions, pull.deletions, pull.changed_files) == (None, None, 3)


def test_変更ファイル数が上限を超えた表記なら記録しない(monkeypatch):
    monkeypatch.setattr(gitlab_api, "get_merge_request", lambda *a: _mr(changes_count="1000+"))

    assert GitLabForge().get_pull("group", "project", 1).changed_files is None


def test_作った直後で_diff_refs_が無ければ待って取り直す(monkeypatch):
    responses = [_mr(diff_refs=None), _mr(diff_refs=None), _mr()]
    monkeypatch.setattr(gitlab_api, "get_merge_request", lambda *a: responses.pop(0))

    assert GitLabForge().get_pull("group", "project", 1).head_sha == "h" * 40
    assert responses == []


def test_diff_refs_がいつまでも無ければ分かる形で失敗する(monkeypatch):
    calls: list = []
    monkeypatch.setattr(
        gitlab_api, "get_merge_request", lambda *a: calls.append(a) or _mr(diff_refs=None)
    )

    with pytest.raises(ReviewError, match="少し待ってから"):
        GitLabForge().get_pull("group", "project", 1)
    assert len(calls) == 1 + len(gitlab_forge._DIFF_REFS_RETRY_DELAYS)


def test_target_ブランチが無ければ実行しない(monkeypatch):
    monkeypatch.setattr(gitlab_api, "get_merge_request", lambda *a: _mr(target_branch=None))

    with pytest.raises(ReviewError, match="target ブランチ"):
        GitLabForge().get_pull("group", "project", 1)


# --- 投稿 ---------------------------------------------------------------------
def test_本文全体を改行を明示して_1_件のコメントにする(monkeypatch, temp_db):
    posted: list = []
    monkeypatch.setattr(gitlab_api, "create_note", lambda *a: posted.append(a))
    review = reviews.create(
        owner="group/sub", repo="project", pr_number=1, model="m",
        body="要約。\n\n- [P1] 指摘 — a.py:3\n  説明。\n",
    )
    lines: list[str] = []

    GitLabForge().post_review("group/sub", "project", 1, review, (), lines.append)

    assert posted == [(
        "glpat-test", "group/sub", "project", 1,
        "codex review (m, rev-0):\n\n要約。\n\n- [P1] 指摘 — a.py:3  \n  説明。\n",
    )]
    assert any("MR !1" in line for line in lines)


def test_blob_の_url_は_gitlab_の行範囲の書き方にする():
    host = GitLabForge()

    assert host.blob_url("group/sub", "project", "abc", "src/a.py", 40, 45) == (
        f"{WEB}/group/sub/project/-/blob/abc/src/a.py#L40-45"
    )
    assert host.blob_url("group", "project", "abc", "a.py", 7, 7) == (
        f"{WEB}/group/project/-/blob/abc/a.py#L7"
    )


def test_本文のパスと相対リンクをコミットの_blob_へのリンクにして投稿する(
    monkeypatch, temp_db, tmp_path
):
    posted: list = []
    monkeypatch.setattr(gitlab_api, "create_note", lambda *a: posted.append(a[-1]))
    root = tmp_path / "work" / "group" / "project"
    review = reviews.create(
        owner="group", repo="project", pr_number=1, model="m", head_commit="abc",
        body=f"- [P1] 指摘 — {root}/a.py:3-4\n  [CR-004](AGENTS.md#L14-L15) に反します。",
    )

    GitLabForge().post_review("group", "project", 1, review, (root,), lambda _l: None)

    web = f"{WEB}/group/project/-/blob/abc"
    # リンクにしたうえで、段落の中の改行を明示する。
    assert posted[0].endswith(
        f"- [P1] 指摘 — [a.py:3-4]({web}/a.py#L3-4)  \n"
        f"  [CR-004]({web}/AGENTS.md#L14-15) に反します。"
    )


# --- API ラッパ ---------------------------------------------------------------
class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


@pytest.fixture
def capture_requests(monkeypatch):
    sent: list[urllib.request.Request] = []
    responses: list = []

    def fake_urlopen(req, timeout=None):
        sent.append(req)
        result = responses.pop(0) if responses else _Response(b"{}")
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return sent, responses


def test_サブグループのプロジェクトはパスをエンコードして引く(capture_requests):
    """エンコードしないと別のエンドポイントとして解釈され 404 になる。"""
    sent, responses = capture_requests
    responses.append(_Response(json.dumps(_mr()).encode()))

    gitlab_api.get_merge_request("glpat-test", "group/sub", "project", 7)

    req = sent[0]
    assert req.full_url == (
        f"{WEB}/api/v4/projects/group%2Fsub%2Fproject/merge_requests/7"
    )
    assert req.get_header("Private-token") == "glpat-test"


def test_コメントを投稿する(capture_requests):
    sent, _ = capture_requests

    gitlab_api.create_note("glpat-test", "group", "project", 7, "本文")

    req = sent[0]
    assert req.get_method() == "POST"
    assert req.full_url.endswith("/projects/group%2Fproject/merge_requests/7/notes")
    assert json.loads(req.data) == {"body": "本文"}


def test_http_エラーは本文のメッセージを添えて伝える(capture_requests):
    sent, responses = capture_requests
    responses.append(urllib.error.HTTPError(
        "u", 404, "err", {}, io.BytesIO(b'{"message": "404 Not found"}')
    ))

    with pytest.raises(gitlab_api.ApiError, match="404 Not found") as excinfo:
        gitlab_api.get_merge_request("glpat-test", "group", "project", 999)
    assert excinfo.value.status == 404
