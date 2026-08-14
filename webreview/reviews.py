"""レビュー結果の永続化と集計。旧 aggregate.py の置き換え。

以前はレビュー本文をログファイルに書き、集計のたびにディレクトリを走査して
ファイル名から repo / PR 番号 / rev を、mtime から日時を復元していた。現在は
レビュー実行時にこのモジュールが 1 行 INSERT し、集計は SELECT だけで済む。

codex の err ログはレビュー本文と違って PR の差分規模に比例して伸びるため DB には
入れない。実行条件を示す先頭ブロック (details) だけを抽出して保存し、以降アプリは
err ログを読まない。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config, db, github_api, github_auth
from .errors import ReviewError

# PR タイトルの再取得間隔。PR 名は変更されうるので恒久キャッシュにはしない。
TITLE_TTL_SECONDS = 24 * 60 * 60

# 一覧の既定件数。
DEFAULT_LIMIT = 50
MAX_LIMIT = 200


@dataclass
class Review:
    id: int
    repo: str
    pr_number: int
    rev: int
    created_at: float
    base_ref: str | None
    base_commit: str | None
    model: str | None
    reasoning_effort: str | None
    codex_version: str | None
    session_id: str | None
    body: str
    details: str | None
    stderr_path: str | None
    stderr_pruned_at: float | None


def _row_to_review(row: Any) -> Review:
    return Review(**{k: row[k] for k in Review.__dataclass_fields__})


# --- 書き込み ---------------------------------------------------------------
def create(
    *,
    repo: str,
    pr_number: int,
    body: str,
    base_ref: str | None = None,
    base_commit: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    codex_version: str | None = None,
    session_id: str | None = None,
    details: str | None = None,
    stderr_path: str | None = None,
    created_at: float | None = None,
    rev: int | None = None,
) -> Review:
    """レビューを 1 件保存する。rev を省略すると採番する。

    採番と INSERT を同じ書き込みトランザクションで行うため、ファイル存在確認で
    次の rev を探していた頃と違って競合しない (UNIQUE 制約が最終的な保証)。
    """
    with db.write() as conn:
        if rev is None:
            row = conn.execute(
                "SELECT COALESCE(MAX(rev), -1) + 1 FROM reviews "
                "WHERE repo = ? AND pr_number = ?",
                (repo, pr_number),
            ).fetchone()
            rev = row[0]
        cur = conn.execute(
            "INSERT INTO reviews (repo, pr_number, rev, created_at, base_ref, "
            "base_commit, model, reasoning_effort, codex_version, session_id, "
            "body, details, stderr_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                repo, pr_number, rev, created_at or time.time(), base_ref,
                base_commit, model, reasoning_effort, codex_version, session_id,
                body, details, stderr_path,
            ),
        )
        review_id = cur.lastrowid
    review = get(review_id)
    assert review is not None
    return review


# --- 参照 -------------------------------------------------------------------
def get(review_id: int) -> Review | None:
    row = db.connect().execute(
        "SELECT * FROM reviews WHERE id = ?", (review_id,)
    ).fetchone()
    return _row_to_review(row) if row else None


def _filter(repo: str | None, q: str | None, alias: str = "") -> tuple[str, list[Any]]:
    """絞り込み条件を WHERE 句とパラメータに組み立てる。

    alias は join を伴うクエリで列を修飾するための接頭辞 ("r." など)。
    """
    clauses: list[str] = []
    params: list[Any] = []
    if repo:
        clauses.append(f"{alias}repo = ?")
        params.append(repo)
    if q:
        clauses.append(f"{alias}body LIKE ?")
        params.append(f"%{q}%")
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def count(repo: str | None = None, q: str | None = None) -> int:
    where, params = _filter(repo, q)
    return db.connect().execute(
        f"SELECT COUNT(*) FROM reviews{where}", params
    ).fetchone()[0]


def list_summary(
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
    repo: str | None = None,
    q: str | None = None,
) -> list[dict[str, Any]]:
    """集計一覧を新しい順に返す。本文と詳細は含めない。

    本文を載せると件数に比例してレスポンスと DOM が膨らむため、一覧は
    メタデータだけを返し、本文は開かれた時点で get_detail() から取得する。
    """
    limit = max(1, min(limit, MAX_LIMIT))
    where, params = _filter(repo, q, alias="r.")
    rows = db.connect().execute(
        "SELECT r.id, r.repo, r.pr_number, r.rev, r.created_at, r.model, "
        # LENGTH() は TEXT に対して文字数を返すので、BLOB に落としてバイト数を取る。
        "r.reasoning_effort, LENGTH(CAST(r.body AS BLOB)) AS body_bytes, "
        "(r.details IS NOT NULL) AS has_details, t.title AS title "
        "FROM reviews r "
        "LEFT JOIN pr_titles t ON t.repo = r.repo AND t.pr_number = r.pr_number"
        + where
        + " ORDER BY r.created_at DESC LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()

    return [
        {
            "id": row["id"],
            "repo": row["repo"],
            "pr_num": row["pr_number"],
            "rev": row["rev"],
            "created_at": row["created_at"],
            "url": f"https://github.com/{config.REVIEW_ORG}/{row['repo']}"
                   f"/pull/{row['pr_number']}",
            "title": row["title"] or "",
            "model": row["model"],
            "reasoning_effort": row["reasoning_effort"],
            "body_bytes": row["body_bytes"],
            "has_details": bool(row["has_details"]),
        }
        for row in rows
    ]


def get_detail(review_id: int) -> dict[str, Any] | None:
    """一覧から開かれたときに本文と詳細を返す。"""
    review = get(review_id)
    if review is None:
        return None
    return {
        "id": review.id,
        "review_text": render_body(review),
        "details": review.details or "",
    }


def repos() -> list[str]:
    """絞り込み用に、レビューが存在するリポジトリ名を返す。"""
    return [
        row[0]
        for row in db.connect().execute(
            "SELECT DISTINCT repo FROM reviews ORDER BY repo"
        )
    ]


# --- 表示・投稿用の整形 -----------------------------------------------------
def header_line(review: Review) -> str:
    """本文の先頭に付けるヘッダ。

    以前はこの文字列をログファイルに書き込んでいたため本文と混ざり、書式を変えると
    過去分と食い違っていた。カラムから毎回組み立てるので書式変更が過去分にも及ぶ。
    """
    parts = [review.model or "unknown"]
    if review.reasoning_effort:
        parts.append(f"reasoning={review.reasoning_effort}")
    parts.append(f"rev-{review.rev}")
    return f"codex review ({', '.join(parts)}):"


def render_body(review: Review) -> str:
    """PR 投稿・画面表示に使う本文 (ヘッダ + 本体)。"""
    return f"{header_line(review)}\n\n{review.body}"


# --- PR タイトルのキャッシュ ------------------------------------------------
def cached_title(repo: str, pr_number: int) -> str | None:
    row = db.connect().execute(
        "SELECT title, fetched_at FROM pr_titles WHERE repo = ? AND pr_number = ?",
        (repo, pr_number),
    ).fetchone()
    if row is None:
        return None
    if time.time() - row["fetched_at"] > TITLE_TTL_SECONDS:
        return None
    return row["title"]


def put_title(repo: str, pr_number: int, title: str) -> None:
    if not title:
        return
    with db.write() as conn:
        conn.execute(
            "INSERT INTO pr_titles (repo, pr_number, title, fetched_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT (repo, pr_number) DO UPDATE SET "
            "title = excluded.title, fetched_at = excluded.fetched_at",
            (repo, pr_number, title, time.time()),
        )


def refresh_titles(limit: int = 20) -> int:
    """タイトル未取得・失効ぶんを GitHub から取り直す。取得できた件数を返す。

    集計の表示を止めないよう、1 回あたりの取得数に上限を設ける。認証未設定や
    削除された PR で失敗しても集計自体は成立させる。
    """
    cutoff = time.time() - TITLE_TTL_SECONDS
    rows = db.connect().execute(
        "SELECT DISTINCT r.repo, r.pr_number FROM reviews r "
        "LEFT JOIN pr_titles t ON t.repo = r.repo AND t.pr_number = r.pr_number "
        "WHERE t.title IS NULL OR t.fetched_at < ? "
        "ORDER BY r.created_at DESC LIMIT ?",
        (cutoff, limit),
    ).fetchall()

    fetched = 0
    for row in rows:
        try:
            pull = github_api.get_pull(
                github_auth.get_provider(),
                config.REVIEW_ORG,
                row["repo"],
                row["pr_number"],
            )
        except ReviewError:
            continue
        put_title(row["repo"], row["pr_number"], pull.title)
        fetched += 1
    return fetched


# --- err ログの保持 ---------------------------------------------------------
def prune_stderr(retention_days: int | None = None) -> int:
    """期限を過ぎた err ログを削除する。削除した件数を返す。

    本文と details は DB にあるため、消しても画面表示は変わらない。行は残したまま
    stderr_path を NULL にし、pruned_at を立てて「元から無い」と区別する。
    """
    days = (
        config.STDERR_RETENTION_DAYS if retention_days is None else retention_days
    )
    if days <= 0:
        return 0

    cutoff = time.time() - days * 86400
    rows = db.connect().execute(
        "SELECT id, stderr_path FROM reviews "
        "WHERE stderr_path IS NOT NULL AND created_at < ?",
        (cutoff,),
    ).fetchall()

    pruned = 0
    for row in rows:
        path = Path(row["stderr_path"])
        try:
            path.unlink(missing_ok=True)
        except OSError:
            continue
        with db.write() as conn:
            conn.execute(
                "UPDATE reviews SET stderr_path = NULL, stderr_pruned_at = ? "
                "WHERE id = ?",
                (time.time(), row["id"]),
            )
        pruned += 1
    return pruned
