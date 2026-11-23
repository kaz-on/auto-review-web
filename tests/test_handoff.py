"""指摘をエージェントに渡す依頼文。

指摘の読み取りは findings のテストで確かめているので、ここでは依頼文に載る文脈
(PR・コミット・場所・説明) と、パスを直せないときの扱いを確かめる。
"""

from __future__ import annotations

import pytest

from webreview import handoff, reviews

pytestmark = pytest.mark.usefixtures("temp_db", "temp_paths", "review_owner")


def _create(body: str, **kw):
    return reviews.create(owner="example-org", repo="my-repo", pr_number=7, body=body, **kw)


def test_指摘をまとめて場所と説明を載せる(temp_paths):
    work, _ = temp_paths
    root = work / "example-org" / "my-repo"
    review = _create(
        f"要約。\n\n- [P1] 合計を誤る — {root}/src/a.py:3-4\n  説明 1 行目。\n  説明 2 行目。\n"
        f"- 閉じ忘れ — {root}/b.py:7\n- 位置の無い箇条書き\n",
        head_commit="abc123",
    )

    assert handoff.handoff(review) == (
        "プルリクエストの自動レビューで、次の指摘がありました。\n\n"
        "- PR: https://github.com/example-org/my-repo/pull/7\n"
        "- レビューしたコミット: abc123\n\n"
        "手元のコードがレビューしたコミットと異なる場合は、現在のコードで確かめてください。\n\n"
        "## 1. [P1] 合計を誤る\n\n"
        "場所: src/a.py:3-4\n\n"
        "説明 1 行目。\n説明 2 行目。\n\n"
        "## 2. 閉じ忘れ\n\n"
        "場所: b.py:7\n"
    )


def test_コミットが分からなければ触れない(temp_paths):
    work, _ = temp_paths
    text = handoff.handoff(_create(f"- 閉じ忘れ — {work}/example-org/my-repo/b.py:7\n"))

    assert text is not None
    assert "コミット" not in text
    assert text.endswith("場所: b.py:7\n")


def test_直せないパスは出力のまま渡す():
    text = handoff.handoff(_create("- 閉じ忘れ — /elsewhere/b.py:7\n"))

    assert text is not None
    assert "場所: /elsewhere/b.py:7" in text


def test_指摘が無ければ_none():
    assert handoff.handoff(_create("問題は見つかりませんでした。\n- 位置の無い箇条書き")) is None
