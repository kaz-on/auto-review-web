"""レビューと指摘を CSV に書き出す。

集計はアプリの外 (スクリプトや表計算) で行う前提で、ここでは数え上げまでに留める。
指摘は投稿と同じ読み取り方 (findings.parse) で数えるので、書式を読めなかった
箇条書きは指摘に含めず、unparsed_items として別に出す。

Excel でそのまま開けるよう、UTF-8 の BOM を付ける。pandas なら
encoding="utf-8-sig" で読める。
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone
from typing import Any

from . import config, db, findings, reviews

REVIEW_COLUMNS = [
    "review_id", "repo", "pr_number", "pr_url", "pr_title", "pr_author",
    "rev", "is_latest_rev", "created_at", "model", "reasoning_effort",
    "include_quality", "pr_additions", "pr_deletions", "pr_changed_files",
    "findings", "findings_p0", "findings_p1", "findings_p2", "findings_p3",
    "findings_other", "unparsed_items",
]

FINDING_COLUMNS = [
    "review_id", "repo", "pr_number", "pr_author", "rev", "is_latest_rev",
    "created_at", "include_quality", "priority", "title", "path",
    "start_line", "end_line", "description",
]

# 個別の列に分ける優先度。codex は P0 (最も重い) 〜 P3 を付ける。それ以外の表記や
# 優先度の無い指摘は findings_other に入れる。
_PRIORITIES = ("P0", "P1", "P2", "P3")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rows(repo: str | None, q: str | None) -> Iterator[Any]:
    """絞り込み条件に合うレビューを古い順に返す (集計タブと同じ条件)。

    件数に上限は設けないので、本文を含む行を一度に読み込まずカーソルのまま回す。
    """
    where, params = reviews._filter(repo, q, alias="r.")
    return db.connect().execute(
        "SELECT r.*, t.title AS title, "
        # 同じ PR をやり直したレビューのうち、最新のものか。PR 単位の集計で使う。
        "(r.rev = (SELECT MAX(x.rev) FROM reviews x "
        "WHERE x.repo = r.repo AND x.pr_number = r.pr_number)) AS is_latest_rev "
        "FROM reviews r "
        "LEFT JOIN pr_titles t ON t.repo = r.repo AND t.pr_number = r.pr_number"
        + where
        + " ORDER BY r.created_at, r.id",
        params,
    )


def review_rows(repo: str | None = None, q: str | None = None) -> Iterator[list[Any]]:
    """レビュー 1 件を 1 行にする。"""
    for row in _rows(repo, q):
        parsed = findings.parse(row["body"])
        counts = dict.fromkeys(_PRIORITIES, 0)
        other = 0
        for item in parsed.items:
            key = (item.priority or "").upper()
            if key in counts:
                counts[key] += 1
            else:
                other += 1
        yield [
            row["id"], row["repo"], row["pr_number"],
            f"https://github.com/{config.REVIEW_OWNER}/{row['repo']}/pull/{row['pr_number']}",
            row["title"], row["pr_author"], row["rev"], row["is_latest_rev"],
            _iso(row["created_at"]), row["model"], row["reasoning_effort"],
            row["include_quality"], row["pr_additions"], row["pr_deletions"],
            row["pr_changed_files"], len(parsed.items),
            *(counts[p] for p in _PRIORITIES), other, parsed.unparsed,
        ]


def finding_rows(repo: str | None = None, q: str | None = None) -> Iterator[list[Any]]:
    """指摘 1 件を 1 行にする。指摘の無いレビューは行を出さない。"""
    for row in _rows(repo, q):
        repo_path = config.WORK_PATH / row["repo"]
        for item in findings.parse(row["body"]).items:
            # 投稿と同じくリポジトリからの相対パスにする。直せないものは codex が
            # 出したまま残す (指摘であることには変わりない)。
            resolved = item.resolve(repo_path)
            yield [
                row["id"], row["repo"], row["pr_number"], row["pr_author"],
                row["rev"], row["is_latest_rev"], _iso(row["created_at"]),
                row["include_quality"], item.priority, item.title,
                resolved.path if resolved else item.raw_path,
                item.start_line, item.end_line, item.description,
            ]


def to_csv(header: list[str], rows: Iterable[list[Any]]) -> Iterator[str]:
    """CSV を行ごとに文字列で返す。先頭に BOM を付け、None は空欄にする。"""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\r\n")

    def flush() -> str:
        text = buf.getvalue()
        buf.seek(0)
        buf.truncate()
        return text

    writer.writerow(header)
    yield "﻿" + flush()
    for row in rows:
        writer.writerow(["" if v is None else v for v in row])
        yield flush()
