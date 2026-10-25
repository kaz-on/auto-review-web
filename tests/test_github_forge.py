"""GitHub の forge 実装。github.com の既定と、GitHub 互換のサービス向けの設定。

互換のサービスの値は、実際に確かめたセルフホストのサービスに合わせてある
(clone は /git/ の下、git の認証はトークンの持ち主の名前、レビュー API なし)。
"""

from __future__ import annotations

import pytest

from webreview import config, github_api, github_auth, reviews
from webreview.errors import ReviewError
from webreview.github_forge import GitHubForge


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
