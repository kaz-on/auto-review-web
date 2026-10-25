"""GitHub の forge 実装。github.com の既定と、GitHub 互換のサービス向けの設定。

互換のサービスの値は、実際に確かめたセルフホストのサービスに合わせてある
(clone は /git/ の下、git の認証はトークンの持ち主の名前、レビュー API なし)。
"""

from __future__ import annotations

from functools import partial
from pathlib import Path

import pytest

from webreview import config, github_api, github_auth, reviews
from webreview.errors import ReviewError
from webreview.github_forge import GitHubForge
from webreview.links import linkify


@pytest.fixture
def compat(monkeypatch):
    """GitHub 互換のセルフホストのサービスを向く設定にする。"""
    monkeypatch.setattr(config, "GITHUB_WEB_URL", "http://git.example.local:8888")
    monkeypatch.setattr(config, "GITHUB_CLONE_URL", "{web}/git/{owner}/{repo}.git")
    monkeypatch.setattr(config, "GITHUB_GIT_USERNAME", "review-bot")
    monkeypatch.setattr(config, "GITHUB_POST_MODE", "issue_comment")
    return GitHubForge()


# --- github.com (既定) -------------------------------------------------------
def test_既定では_github_com_の形を使う():
    forge = GitHubForge()

    assert forge.parse_pr_url("https://github.com/o/r/pull/3") == ("o", "r", 3)
    assert forge.pr_url("o", "r", 3) == "https://github.com/o/r/pull/3"
    assert forge.pr_url_pattern() == "https://github.com/<owner>/<repo>/pull/<番号>"
    assert forge.clone_url("o", "r") == "https://github.com/o/r.git"


@pytest.mark.parametrize("url", [
    "https://github.com/o/r.git",
    "https://github.com/o/r",
])
def test_origin_の_url_を分解する(url):
    assert GitHubForge().parse_remote_url(url) == ("o", "r")


def test_ドットを含むリポジトリ名も_origin_から分解できる():
    assert GitHubForge().parse_remote_url("https://github.com/o/my.repo.git") == (
        "o", "my.repo",
    )


def test_別のホストや形の_origin_は分解しない():
    forge = GitHubForge()

    assert forge.parse_remote_url("https://gitlab.example.com/o/r.git") is None
    assert forge.parse_remote_url("https://github.com/o/r/extra.git") is None


def test_git_の認証は_github_com_に限りユーザー名は固定(monkeypatch):
    monkeypatch.setattr(github_auth, "get_provider", lambda owner: _Provider(owner))

    credential = GitHubForge().git_credential("o")

    assert credential.url == "https://github.com"
    assert credential.username == "x-access-token"
    # トークンはオーナーごとの provider から取る。
    assert credential.password == "token-for-o"


class _Provider:
    def __init__(self, owner: str):
        self.owner = owner

    def token(self) -> str:
        return f"token-for-{self.owner}"


# --- GitHub 互換のサービス ---------------------------------------------------
def test_設定したホストの_pr_url_を受け付ける(compat):
    assert compat.parse_pr_url("http://git.example.local:8888/o/r/pull/1") == ("o", "r", 1)
    assert compat.pr_url("o", "r", 1) == "http://git.example.local:8888/o/r/pull/1"
    assert compat.pr_url_pattern() == (
        "http://git.example.local:8888/<owner>/<repo>/pull/<番号>"
    )


@pytest.mark.parametrize("url", [
    "https://github.com/o/r/pull/1",
    # ポート番号やホスト名の前方一致で通してはいけない。
    "http://git.example.local:88881/o/r/pull/1",
    "http://git.example.local:8888.evil.example/o/r/pull/1",
    "https://git.example.local:8888/o/r/pull/1",
])
def test_設定したホスト以外の_pr_url_は受け付けない(compat, url):
    assert compat.parse_pr_url(url) is None


def test_clone_url_は設定の形で組み立てて分解できる(compat):
    url = compat.clone_url("o", "r")

    assert url == "http://git.example.local:8888/git/o/r.git"
    assert compat.parse_remote_url(url) == ("o", "r")
    # github.com の形の origin は別のリポジトリとみなす。
    assert compat.parse_remote_url("https://github.com/o/r.git") is None


def test_git_の認証は設定したホストとユーザー名で行う(compat, monkeypatch):
    monkeypatch.setattr(github_auth, "get_provider", lambda owner: _Provider(owner))

    credential = compat.git_credential("o")

    # helper はスキームとホストだけで効かせる (clone の /git/ の下に限らない)。
    assert credential.url == "http://git.example.local:8888"
    assert credential.username == "review-bot"


def test_レビュー_api_が無ければ本文全体を_1_件のコメントにする(compat, monkeypatch, temp_db):
    posted: list[tuple] = []
    monkeypatch.setattr(github_auth, "get_provider", lambda owner: _Provider(owner))
    monkeypatch.setattr(
        github_api, "create_issue_comment",
        lambda provider, owner, repo, number, body: posted.append(
            (provider.owner, owner, repo, number, body)
        ),
    )
    monkeypatch.setattr(
        github_api, "create_review",
        lambda *a, **k: pytest.fail("レビュー API は呼ばない"),
    )
    review = reviews.create(
        owner="o", repo="r", pr_number=1, model="m",
        body="要約。\n\n- [P1] 指摘 — /work/o/r/a.py:3\n  説明。\n",
    )
    lines: list[str] = []

    compat.post_review("o", "r", 1, review, (), lines.append)

    # 指摘を行コメントに抜き出さず、ヘッダ付きの本文をそのまま送る。
    assert posted == [("o", "o", "r", 1, reviews.render_body(review))]
    assert any("コメントとして投稿" in line for line in lines)


# --- 設定の不備 ---------------------------------------------------------------
@pytest.mark.parametrize("name, value, message", [
    ("GITHUB_POST_MODE", "comment", "GITHUB_POST_MODE"),
    ("GITHUB_CLONE_URL", "{web}/git/repo.git", "GITHUB_CLONE_URL"),
    ("GITHUB_WEB_URL", "git.example.local", "GITHUB_WEB_URL"),
])
def test_設定に不備があれば分かる形で失敗する(monkeypatch, name, value, message):
    monkeypatch.setattr(config, name, value)

    with pytest.raises(ReviewError, match=message):
        GitHubForge()


# --- blob へのリンク ----------------------------------------------------------
def test_blob_の_url_はコミットに固定して行範囲を指す():
    forge = GitHubForge()

    assert forge.blob_url("o", "r", "abc", "src/a.py", 40, 45) == (
        "https://github.com/o/r/blob/abc/src/a.py#L40-L45"
    )
    assert forge.blob_url("o", "r", "abc", "a.py", 7, 7) == (
        "https://github.com/o/r/blob/abc/a.py#L7"
    )
    assert forge.blob_url("o", "r", "abc", "a.py") == "https://github.com/o/r/blob/abc/a.py"


def test_blob_の_url_のパスはエンコードする():
    assert GitHubForge().blob_url("o", "r", "abc", "docs/設計 メモ.md") == (
        "https://github.com/o/r/blob/abc/docs/%E8%A8%AD%E8%A8%88%20%E3%83%A1%E3%83%A2.md"
    )


def test_互換のサービスでは本文のパスをコミットの_blob_へのリンクにする(
    compat, monkeypatch, temp_db, tmp_path
):
    posted: list[str] = []
    monkeypatch.setattr(github_auth, "get_provider", lambda owner: _Provider(owner))
    monkeypatch.setattr(
        github_api, "create_issue_comment",
        lambda provider, owner, repo, number, body: posted.append(body),
    )
    root = tmp_path / "work" / "o" / "r"
    review = reviews.create(
        owner="o", repo="r", pr_number=1, model="m", head_commit="abc",
        body=f"- [P1] 指摘 — {root}/a.py:3-4\n  [CR-004](AGENTS.md#L14-L15) に反します。\n",
    )

    compat.post_review("o", "r", 1, review, (root,), lambda _l: None)

    web = "http://git.example.local:8888/o/r/blob/abc"
    assert posted[0].endswith(
        f"- [P1] 指摘 — [a.py:3-4]({web}/a.py#L3-L4)\n"
        f"  [CR-004]({web}/AGENTS.md#L14-L15) に反します。\n"
    )


def test_行コメントの説明と本文に残った部分もリンクにする(monkeypatch, temp_db, tmp_path):
    calls: list[dict] = []
    monkeypatch.setattr(github_auth, "get_provider", lambda owner: _Provider(owner))
    monkeypatch.setattr(
        github_api, "create_review",
        lambda provider, owner, repo, number, body, comments=None, commit_id=None:
            calls.append({"body": body, "comments": comments}),
    )
    root = tmp_path / "work" / "o" / "r"
    review = reviews.create(
        owner="o", repo="r", pr_number=1, model="m", head_commit="abc",
        body=(
            f"要約。{root}/b.py にも同じ問題がある。\n\n"
            f"- [P1] 指摘 — {root}/a.py:3-3\n  [CR-004](AGENTS.md#L14) に反します。\n"
        ),
    )

    GitHubForge().post_review("o", "r", 1, review, (root,), lambda _l: None)

    web = "https://github.com/o/r/blob/abc"
    assert calls[0]["body"].endswith(f"要約。[b.py]({web}/b.py) にも同じ問題がある。")
    # 行コメントの位置は相対パスのまま、説明の中のリンクだけを置き換える。
    [comment] = calls[0]["comments"]
    assert comment["path"] == "a.py"
    assert comment["body"].endswith(f"[CR-004]({web}/AGENTS.md#L14) に反します。")


def test_レビューしたコミットが分からない古いレビューは相対パスに直すだけ(
    compat, monkeypatch, temp_db, tmp_path
):
    posted: list[str] = []
    monkeypatch.setattr(github_auth, "get_provider", lambda owner: _Provider(owner))
    monkeypatch.setattr(
        github_api, "create_issue_comment",
        lambda provider, owner, repo, number, body: posted.append(body),
    )
    root = tmp_path / "work" / "o" / "r"
    review = reviews.create(
        owner="o", repo="r", pr_number=1, model="m", body=f"- [P1] 指摘 — {root}/a.py:3\n",
    )

    compat.post_review("o", "r", 1, review, (root,), lambda _l: None)

    assert posted[0].endswith("- [P1] 指摘 — a.py:3\n")


def test_日本語や空白を含むリンク先は一度だけエンコードする():
    blob = partial(GitHubForge().blob_url, "o", "r", "abc")

    assert linkify("[設計](docs/%E8%A8%AD%E8%A8%88%20memo.md#L2)", (Path("/w"),), blob) == (
        "[設計](https://github.com/o/r/blob/abc/docs/%E8%A8%AD%E8%A8%88%20memo.md#L2)"
    )
