"""レビュー結果の永続化と集計。

レビュー実行時にこのモジュールが 1 行 INSERT し、集計は SELECT だけで済ませる。

codex の err ログはレビュー本文と違って PR の差分規模に比例して伸びるため DB には
入れない。実行条件を示す先頭ブロック (details) だけを抽出して保存し、以降アプリは
err ログを読まない。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import config, db, forge
from .errors import ReviewError

# PR タイトルの再取得間隔。PR 名は変更されうるので恒久キャッシュにはしない。
TITLE_TTL_SECONDS = 24 * 60 * 60

# タイトル取り直しの多重起動を防ぐ。
_REFRESH_LOCK = threading.Lock()
_refreshing = False

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
    head_commit: str | None
    model: str | None
    reasoning_effort: str | None
    codex_version: str | None
    session_id: str | None
    body: str
    details: str | None
    include_quality: bool
    pr_author: str | None
    pr_additions: int | None
    pr_deletions: int | None
    pr_changed_files: int | None
    stderr_path: str | None
    stderr_pruned_at: float | None


def _row_to_review(row: Any) -> Review:
    values = {k: row[k] for k in Review.__dataclass_fields__}
    values["include_quality"] = bool(values["include_quality"])
    return Review(**values)


# --- 書き込み ---------------------------------------------------------------
def create(
    *,
    repo: str,
    pr_number: int,
    body: str,
    base_ref: str | None = None,
    base_commit: str | None = None,
    head_commit: str | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    codex_version: str | None = None,
    session_id: str | None = None,
    details: str | None = None,
    include_quality: bool = False,
    pr_author: str | None = None,
    pr_additions: int | None = None,
    pr_deletions: int | None = None,
    pr_changed_files: int | None = None,
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
            "base_commit, head_commit, model, reasoning_effort, codex_version, "
            "session_id, body, details, include_quality, pr_author, pr_additions, "
            "pr_deletions, pr_changed_files, stderr_path) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                repo, pr_number, rev, created_at or time.time(), base_ref,
                base_commit, head_commit, model, reasoning_effort, codex_version,
                session_id, body, details, int(include_quality), pr_author or None,
                pr_additions, pr_deletions, pr_changed_files, stderr_path,
            ),
        )
        review_id = cur.lastrowid
    review = get(review_id)
    assert review is not None
    return review


def set_stderr_path(review_id: int, path: Path | None) -> None:
    """err ログの最終的な置き場所を記録する。

    rev はレビューを登録して初めて決まるため、実行中は一時名で書いておき、
    確定した名前をここで結び付ける。
    """
    with db.write() as conn:
        conn.execute(
            "UPDATE reviews SET stderr_path = ? WHERE id = ?",
            (str(path) if path else None, review_id),
        )


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
        "r.reasoning_effort, r.include_quality, "
        "LENGTH(CAST(r.body AS BLOB)) AS body_bytes, "
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
            "url": forge.get().pr_url(config.REVIEW_OWNER, row["repo"], row["pr_number"]),
            "title": row["title"] or "",
            "model": row["model"],
            "reasoning_effort": row["reasoning_effort"],
            "include_quality": bool(row["include_quality"]),
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
    parts = []
    # 初期のログには実行時設定の記録が無い。分からないものは書かずに省く。
    if review.model:
        parts.append(review.model)
    if review.reasoning_effort:
        parts.append(f"reasoning={review.reasoning_effort}")
    # 品質改善を含めた実行だけ印を付ける。含めないのが codex の既定なので、
    # 何も書かなければ従来どおりのバグ限定レビューを意味する。
    if review.include_quality:
        parts.append("quality")
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


def refresh_titles_async(limit: int = 20) -> None:
    """タイトルの取り直しをバックグラウンドで走らせる。

    API は 1 件ずつ順に叩くため、集計リクエストの中で待つと表示が数秒
    止まりうる。取得できなかったぶんは前回の値のまま表示し、次に開いたときに
    反映されればよい。多重起動は避ける (集計は連続で開かれることがある)。
    """
    global _refreshing
    with _REFRESH_LOCK:
        if _refreshing:
            return
        _refreshing = True

    def worker() -> None:
        global _refreshing
        try:
            refresh_titles(limit)
        finally:
            with _REFRESH_LOCK:
                _refreshing = False

    threading.Thread(target=worker, daemon=True).start()


def refresh_titles(limit: int = 20) -> int:
    """タイトル未取得・失効ぶんを PR のホストから取り直す。取得できた件数を返す。

    1 回あたりの取得数に上限を設ける。認証未設定や削除された PR で失敗しても
    集計自体は成立させる。
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
            pull = forge.get().get_pull(
                config.REVIEW_OWNER, row["repo"], row["pr_number"]
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
