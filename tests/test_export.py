"""レビューと指摘の CSV 書き出し。

指摘の数え方は投稿と同じ findings.parse に任せているので、ここでは優先度の振り分け・
最新 rev の判定・CSV の体裁 (BOM、空欄、改行を含む値) を確かめる。
"""

from __future__ import annotations

import csv
import io

import pytest

from webreview import export, reviews

pytestmark = pytest.mark.usefixtures("temp_db", "temp_paths", "review_owner")

BODY = """\
要約。

- [P1] 合計を誤る — {work}/my-repo/a.py:3-4
  説明 1 行目。
  説明 2 行目。
- [P2] 閉じ忘れ — {work}/my-repo/b.py:7
- [P1] 二件目 — {work}/my-repo/a.py:9
- 位置の無い箇条書き
"""


def _create(work, pr_number: int = 1, body: str | None = None, **kw):
    kw.setdefault("created_at", 1_700_000_000.0 + pr_number)
    return reviews.create(
        repo="my-repo", pr_number=pr_number,
        body=BODY.format(work=work) if body is None else body, **kw,
    )


def _as_dicts(header, rows):
    return [dict(zip(header, row)) for row in rows]


def test_レビュー単位では優先度ごとに指摘を数える(temp_paths):
    work, _ = temp_paths
    _create(work, pr_author="alice", pr_additions=10, pr_deletions=2,
            pr_changed_files=1, include_quality=True)

    [row] = _as_dicts(export.REVIEW_COLUMNS, export.review_rows())

    assert row["pr_author"] == "alice"
    assert row["pr_url"] == "https://github.com/example-org/my-repo/pull/1"
    assert row["created_at"] == "2023-11-14T22:13:21Z"
    assert row["include_quality"] == 1
    assert (row["findings"], row["findings_p1"], row["findings_p2"]) == (3, 2, 1)
    assert (row["findings_p0"], row["findings_p3"], row["findings_other"]) == (0, 0, 0)
    assert row["unparsed_items"] == 1


def test_優先度の無い指摘はその他に数える(temp_paths):
    work, _ = temp_paths
    _create(work, body=f"- 優先度なし — {work}/my-repo/a.py:1\n- [p0] 小文字 — a.py:2\n")

    [row] = _as_dicts(export.REVIEW_COLUMNS, export.review_rows())

    assert (row["findings"], row["findings_p0"], row["findings_other"]) == (2, 1, 1)


def test_指摘の無いレビューも_0_件として出す(temp_paths):
    work, _ = temp_paths
    _create(work, body="問題は見つかりませんでした。")

    [row] = _as_dicts(export.REVIEW_COLUMNS, export.review_rows())

    assert (row["findings"], row["unparsed_items"]) == (0, 0)
    assert list(export.finding_rows()) == []


def test_同じ_pr_の最新の_rev_だけに印を付ける(temp_paths):
    work, _ = temp_paths
    _create(work, pr_number=1)
    _create(work, pr_number=1)
    _create(work, pr_number=2)

    rows = _as_dicts(export.REVIEW_COLUMNS, export.review_rows())

    assert [(r["pr_number"], r["rev"], r["is_latest_rev"]) for r in rows] == [
        (1, 0, 0), (1, 1, 1), (2, 0, 1),
    ]


def test_古い順に並べ集計タブと同じ条件で絞り込める(temp_paths):
    work, _ = temp_paths
    _create(work, pr_number=2)
    _create(work, pr_number=1)
    reviews.create(repo="other", pr_number=3, body="injection の指摘")

    assert [r[2] for r in export.review_rows()] == [1, 2, 3]
    assert [r[2] for r in export.review_rows(repo="my-repo")] == [1, 2]
    assert [r[2] for r in export.review_rows(q="injection")] == [3]


def test_指摘単位ではパスをリポジトリからの相対にする(temp_paths):
    work, _ = temp_paths
    _create(work, pr_author="alice")
    reviews.put_title("my-repo", 1, "タイトル")

    rows = _as_dicts(export.FINDING_COLUMNS, export.finding_rows())

    assert [(r["priority"], r["path"], r["start_line"], r["end_line"]) for r in rows] == [
        ("P1", "a.py", 3, 4), ("P2", "b.py", 7, 7), ("P1", "a.py", 9, 9),
    ]
    assert rows[0]["pr_author"] == "alice"
    assert rows[0]["title"] == "合計を誤る"
    assert rows[0]["description"] == "説明 1 行目。\n説明 2 行目。"


def test_リポジトリの外を指すパスは出力されたまま残す(temp_paths):
    work, _ = temp_paths
    _create(work, body="- [P1] 外 — /elsewhere/x.py:1\n")

    [row] = _as_dicts(export.FINDING_COLUMNS, export.finding_rows())

    assert row["path"] == "/elsewhere/x.py"


def test_csv_は_bom_付きで空欄と改行を含む値を扱える():
    text = "".join(export.to_csv(["a", "b"], [[None, "1行目\n2行目"], [1, 'x"y']]))

    assert text.startswith("﻿a,b\r\n")
    rows = list(csv.reader(io.StringIO(text.removeprefix("﻿"))))
    assert rows == [["a", "b"], ["", "1行目\n2行目"], ["1", 'x"y']]
