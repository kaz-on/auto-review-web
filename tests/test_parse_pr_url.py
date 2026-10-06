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
    monkeypatch.setattr(config, "REVIEW_OWNER", "")
    with pytest.raises(ReviewError, match="REVIEW_OWNER"):
        parse_pr_url("https://github.com/anyone/my-repo/pull/1")


def test_別のオーナーのリポジトリは弾く(review_owner):
    with pytest.raises(ReviewError, match="other-org"):
        parse_pr_url("https://github.com/other-org/my-repo/pull/1")
