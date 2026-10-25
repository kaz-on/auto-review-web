"""parse_pr_url の URL 解釈とオーナーの制約。

オーナーの検査はレビュー対象を絞る唯一の関門なので、通す条件・弾く条件を
両方おさえる。
"""

from __future__ import annotations

import pytest

from webreview import config
from webreview.errors import ReviewError
from webreview.review import parse_pr_url


def test_正しい_url_を_owner_repo_number_に分解する(review_owner):
    pr = parse_pr_url("https://github.com/example-org/my-repo/pull/42")
    assert (pr.owner, pr.repo, pr.number) == ("example-org", "my-repo", 42)
    assert pr.url == "https://github.com/example-org/my-repo/pull/42"


def test_前後の空白を無視する(review_owner):
    pr = parse_pr_url("  https://github.com/example-org/my-repo/pull/42\n")
    assert pr.number == 42


@pytest.mark.parametrize("suffix", ["/files", "/commits/abc123", "/"])
def test_pr_url_の末尾に余分なパスがあっても解釈できる(review_owner, suffix):
    pr = parse_pr_url(f"https://github.com/example-org/my-repo/pull/42{suffix}")
    assert pr.number == 42


def test_url_が空なら入力を促す(review_owner):
    with pytest.raises(ReviewError, match="URL を入力"):
        parse_pr_url("")


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/example-org/my-repo/issues/42",
        "https://github.com/example-org/my-repo",
        "http://github.com/example-org/my-repo/pull/42",
        "https://example.com/example-org/my-repo/pull/42",
        "https://github.com/example-org/my-repo/pull/abc",
        # ホスト名の前方一致で通してはいけない。
        "https://github.com.evil.example/example-org/my-repo/pull/42",
    ],
)
def test_pr_の_url_形式でなければ弾く(review_owner, url):
    with pytest.raises(ReviewError, match="PR URL 形式ではありません"):
        parse_pr_url(url)


def test_review_owner_が未設定なら誰のリポジトリも通さない(monkeypatch):
    monkeypatch.setattr(config, "REVIEW_OWNERS", ())
    with pytest.raises(ReviewError, match="REVIEW_OWNER"):
        parse_pr_url("https://github.com/anyone/my-repo/pull/1")


@pytest.mark.parametrize("owner", ["team-a", "team-b"])
def test_複数指定したオーナーはどれも通す(monkeypatch, owner):
    monkeypatch.setattr(config, "REVIEW_OWNERS", ("team-a", "team-b"))

    pr = parse_pr_url(f"https://github.com/{owner}/my-repo/pull/1")

    assert pr.owner == owner


def test_複数指定したオーナーのどれでもなければ弾く(monkeypatch):
    monkeypatch.setattr(config, "REVIEW_OWNERS", ("team-a", "team-b"))

    with pytest.raises(ReviewError, match="team-a, team-b"):
        parse_pr_url("https://github.com/team-c/my-repo/pull/1")


def test_オーナー名は前方一致や大文字小文字の違いでは通さない(monkeypatch):
    monkeypatch.setattr(config, "REVIEW_OWNERS", ("team-a",))

    for owner in ("team-ab", "team", "Team-A"):
        with pytest.raises(ReviewError, match="いずれでもありません"):
            parse_pr_url(f"https://github.com/{owner}/my-repo/pull/1")


@pytest.mark.parametrize("repo", [".", ".."])
def test_作業ディレクトリの外を指すリポジトリ名は弾く(review_owner, repo):
    """オーナーとリポジトリ名はそのまま作業ディレクトリのパスになる。"""
    with pytest.raises(ReviewError, match="PR URL 形式ではありません"):
        parse_pr_url(f"https://github.com/example-org/{repo}/pull/1")


def test_別のオーナーのリポジトリは弾く(review_owner):
    with pytest.raises(ReviewError, match="other-org"):
        parse_pr_url("https://github.com/other-org/my-repo/pull/1")
