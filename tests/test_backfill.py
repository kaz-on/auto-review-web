"""既存のレビューに PR の作成者と変更の規模を埋める管理用スクリプト。

GitHub API は差し替える。レビューのあとに push された PR だけ compare API で
規模を求め直すこと、既に入っている値を上書きしないことを確かめる。
"""

from __future__ import annotations

import pytest

from webreview import backfill, config, github_api, github_auth, reviews
from webreview.errors import ReviewError

pytestmark = pytest.mark.usefixtures("temp_db", "review_owner")


def _pull(head: str = "h1", author: str = "alice") -> github_api.PullRequest:
    return github_api.PullRequest(
        base_ref="main", base_sha="b1", head_sha=head, title="t", body="",
        author=author, additions=10, deletions=2, changed_files=3,
    )


@pytest.fixture
def github(monkeypatch):
    """get_pull / compare_stats の呼ばれ方を記録し、応答を差し替えられるようにする。"""
    state = {"pulls": {}, "compare": github_api.DiffStats(5, 1, 2), "asked": [], "compared": []}

    def fake_get_pull(provider, owner, repo, number):
        state["asked"].append((repo, number))
        pull = state["pulls"].get(number, _pull())
        if isinstance(pull, Exception):
            raise pull
        return pull

    def fake_compare(provider, owner, repo, base, head):
        state["compared"].append((base, head))
        return state["compare"]

    monkeypatch.setattr(github_auth, "get_provider", lambda: object())
    monkeypatch.setattr(github_api, "get_pull", fake_get_pull)
    monkeypatch.setattr(github_api, "compare_stats", fake_compare)
    return state


def _create(pr_number: int = 1, **kw):
    kw.setdefault("base_commit", "b0")
    kw.setdefault("head_commit", "h1")
    return reviews.create(repo="my-repo", pr_number=pr_number, body="本文", **kw)


def test_head_が同じなら_pr_の値で埋める(github):
    review = _create()

    result = backfill.backfill(out=lambda _: None)

    got = reviews.get(review.id)
    assert got.pr_author == "alice"
    assert (got.pr_additions, got.pr_deletions, got.pr_changed_files) == (10, 2, 3)
    assert github["compared"] == []
    assert (result.updated, result.size_unknown, result.failed) == (1, 0, 0)


def test_レビューのあとに_push_されていたら_compare_で求め直す(github):
    github["pulls"][1] = _pull(head="h2")
    review = _create()

    backfill.backfill(out=lambda _: None)

    got = reviews.get(review.id)
    assert (got.pr_additions, got.pr_deletions, got.pr_changed_files) == (5, 1, 2)
    # レビューした差分 (base_commit...head_commit) で求める。
    assert github["compared"] == [("b0", "h1")]


def test_head_が記録されていなければ規模は空のまま作成者だけ埋める(github):
    review = _create(head_commit=None)

    result = backfill.backfill(out=lambda _: None)

    got = reviews.get(review.id)
    assert got.pr_author == "alice"
    assert got.pr_additions is None
    assert result.size_unknown == 1


def test_同じ_pr_の_rev_違いでは_pr_を一度だけ取得する(github):
    _create()
    _create()

    backfill.backfill(out=lambda _: None)

    assert github["asked"] == [("my-repo", 1)]


def test_既に値のあるレビューは対象にせず上書きもしない(github):
    _create(pr_author="bob", pr_additions=1, pr_deletions=1, pr_changed_files=1)
    partial = _create(pr_number=2, pr_author="carol")

    backfill.backfill(out=lambda _: None)

    assert github["asked"] == [("my-repo", 2)]
    got = reviews.get(partial.id)
    assert got.pr_author == "carol"
    assert got.pr_additions == 10


def test_dry_run_では書き換えない(github):
    review = _create()

    result = backfill.backfill(dry_run=True, out=lambda _: None)

    assert result.updated == 1
    assert reviews.get(review.id).pr_author is None


def test_取得に失敗した_pr_は飛ばして残りを埋める(github):
    github["pulls"][1] = ReviewError("not found")
    _create(pr_number=1)
    ok = _create(pr_number=2)

    result = backfill.backfill(out=lambda _: None)

    assert (result.updated, result.failed) == (1, 1)
    assert reviews.get(ok.id).pr_author == "alice"


def test_失敗があれば終了コードで伝える(github):
    github["pulls"][1] = ReviewError("not found")
    _create()

    assert backfill.main([]) == 1


def test_review_owner_が無ければ何もしない(github, monkeypatch):
    monkeypatch.setattr(config, "REVIEW_OWNER", "")
    _create()

    assert backfill.main([]) == 2
    assert github["asked"] == []
