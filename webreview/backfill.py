"""既存のレビューに、PR の作成者と変更の規模を埋める管理用スクリプト。

    python -m webreview.backfill [--dry-run]

これらの列はレビューの実行時に記録するが、列を足す前のレビューには入っていない。
GitHub API を PR 1 件につき 1 回 (規模を求め直すレビューではさらに 1 回ずつ) 呼ぶ
ので、サーバの起動時やリクエストの中では走らせず、必要なときに手で実行する。

- 作成者は PR の API から取る (PR の作成者は後から変わらない)。
- 変更の規模は、PR の現在の head がレビューした head_commit と同じなら PR の API の
  値を使う。違えば (レビューのあとに push された) compare API で base_commit...
  head_commit の規模を求め直す。head_commit が記録されていない古いレビューや、
  ファイル数が多すぎて合計が出せない場合は空のまま残す。
- 既に値の入っている列は上書きしない。何度実行してもよい。
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from typing import Any, Callable

from . import config, db, github_api, github_auth
from .errors import ReviewError


@dataclass
class Result:
    updated: int = 0
    # 作成者は埋まったが、規模が分からなかったレビュー。
    size_unknown: int = 0
    failed: int = 0


def _size(
    provider: Any, row: Any, pull: github_api.PullRequest
) -> github_api.DiffStats | None:
    if row["head_commit"] and row["head_commit"] == pull.head_sha:
        if None in (pull.additions, pull.deletions, pull.changed_files):
            return None
        return github_api.DiffStats(
            additions=pull.additions, deletions=pull.deletions,
            changed_files=pull.changed_files,
        )
    if not (row["base_commit"] and row["head_commit"]):
        return None
    return github_api.compare_stats(
        provider, config.REVIEW_OWNER, row["repo"],
        row["base_commit"], row["head_commit"],
    )


def backfill(dry_run: bool = False, out: Callable[[str], None] = print) -> Result:
    """作成者か規模の欠けたレビューを埋める。"""
    rows = db.connect().execute(
        "SELECT id, repo, pr_number, rev, base_commit, head_commit, pr_author, "
        "pr_additions FROM reviews "
        "WHERE pr_author IS NULL OR pr_additions IS NULL "
        "OR pr_deletions IS NULL OR pr_changed_files IS NULL "
        "ORDER BY repo, pr_number, rev"
    ).fetchall()
    result = Result()
    if not rows:
        out("埋めるレビューはありません。")
        return result

    provider = github_auth.get_provider()
    # 同じ PR の rev 違いで PR の API を何度も呼ばない。
    pulls: dict[tuple[str, int], github_api.PullRequest] = {}

    for row in rows:
        label = f"{row['repo']}#{row['pr_number']} rev-{row['rev']}"
        key = (row["repo"], row["pr_number"])
        try:
            if key not in pulls:
                pulls[key] = github_api.get_pull(
                    provider, config.REVIEW_OWNER, row["repo"], row["pr_number"]
                )
            pull = pulls[key]
            # 規模の 3 列は常にそろえて書くので、1 列で埋まっているかが分かる。
            size = None if row["pr_additions"] is not None else _size(provider, row, pull)
        except ReviewError as e:
            out(f"[warn] {label}: 取得できませんでした: {e}")
            result.failed += 1
            continue

        author = row["pr_author"] or pull.author or None
        parts = [f"author={author or '(不明)'}"]
        if size is not None:
            parts.append(
                f"+{size.additions}/-{size.deletions} ({size.changed_files} files)"
            )
        elif row["pr_additions"] is None:
            parts.append("変更の規模は分かりませんでした")
            result.size_unknown += 1
        out(f"{label}: {', '.join(parts)}")

        if not dry_run:
            with db.write() as conn:
                conn.execute(
                    "UPDATE reviews SET pr_author = COALESCE(pr_author, ?), "
                    "pr_additions = COALESCE(pr_additions, ?), "
                    "pr_deletions = COALESCE(pr_deletions, ?), "
                    "pr_changed_files = COALESCE(pr_changed_files, ?) WHERE id = ?",
                    (
                        author,
                        size.additions if size else None,
                        size.deletions if size else None,
                        size.changed_files if size else None,
                        row["id"],
                    ),
                )
        result.updated += 1

    verb = "埋める対象" if dry_run else "埋めたレビュー"
    out(
        f"{verb}: {result.updated} 件 (うち規模が不明 {result.size_unknown} 件) / "
        f"取得に失敗: {result.failed} 件"
    )
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m webreview.backfill",
        description="既存のレビューに PR の作成者と変更の規模を埋める。",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="GitHub から取得して表示するだけで、DB は書き換えない",
    )
    args = parser.parse_args(argv)

    if not config.REVIEW_OWNER:
        print("REVIEW_OWNER が未設定です。", file=sys.stderr)
        return 2
    db.init()
    try:
        result = backfill(dry_run=args.dry_run)
    except ReviewError as e:
        # 認証の設定不備など、1 件目より前に止まる失敗。
        print(f"[error] {e}", file=sys.stderr)
        return 1
    return 1 if result.failed else 0


if __name__ == "__main__":
    sys.exit(main())
