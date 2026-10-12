"""レビュージョブの状態管理。SQLite に永続化する。

Web リクエストスレッドとワーカースレッドの両方から使われるが、排他は SQLite に
任せる (db モジュールの WAL + busy_timeout)。以前はジョブ全件を 1 つの JSON に
書き出していたため進捗 1 行ごとに全体を再シリアライズしていたが、進捗は
job_progress への 1 行 INSERT になった。

再起動時に未完了 (queued/running) だったジョブは中断扱いにする。
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from dataclasses import asdict, dataclass
from typing import Any

from . import config, db

# ジョブのステータス
QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"

_ACTIVE = (QUEUED, RUNNING)

# 一覧で 1 度に返す件数。画面は 2 秒間隔でポーリングするので、全件は返さない。
DEFAULT_LIST_LIMIT = 20
MAX_LIST_LIMIT = 500

# update() で書き換えを許すカラム (列名を SQL に直接埋めるため固定集合で縛る)。
_UPDATABLE = frozenset({
    "status", "started_at", "finished_at", "error", "auto_post",
    "posted", "posting", "post_error", "review_id",
})

# ジョブ本体と、表示のために reviews / pr_titles から引く派生項目。
_SELECT = """
SELECT j.*,
       t.title  AS pr_title,
       r.rev    AS rev,
       r.base_ref AS base_ref,
       (SELECT COUNT(*) FROM job_progress p WHERE p.job_id = j.id) AS progress_count
  FROM jobs j
  LEFT JOIN pr_titles t ON t.repo = j.repo AND t.pr_number = j.number
  LEFT JOIN reviews   r ON r.id = j.review_id
"""


@dataclass
class Job:
    id: str
    url: str
    owner: str
    repo: str
    number: int
    status: str
    created_at: float
    started_at: float | None = None
    finished_at: float | None = None
    error: str | None = None
    auto_post: bool = True
    # バグ以外に書き方・品質の改善も指摘対象に含めるか (投入時に決まり、以後変えない)。
    include_quality: bool = True
    posted: bool = False
    posting: bool = False
    post_error: str | None = None
    # 成功時に生成されたレビュー (reviews.id)。
    review_id: int | None = None
    # --- 派生項目 (join で埋める。DB 上は jobs に持たない) ---
    pr_title: str | None = None
    rev: int | None = None
    base_ref: str | None = None
    progress_count: int = 0
    # 未取得なら None。空リストは「取得したが 0 件」を意味する。
    progress: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _row_to_job(row: Any, progress: list[str] | None = None) -> Job:
    return Job(
        id=row["id"],
        url=row["url"],
        owner=row["owner"],
        repo=row["repo"],
        number=row["number"],
        status=row["status"],
        created_at=row["created_at"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        error=row["error"],
        auto_post=bool(row["auto_post"]),
        include_quality=bool(row["include_quality"]),
        posted=bool(row["posted"]),
        posting=bool(row["posting"]),
        post_error=row["post_error"],
        review_id=row["review_id"],
        pr_title=row["pr_title"],
        rev=row["rev"],
        base_ref=row["base_ref"],
        progress_count=row["progress_count"],
        progress=progress,
    )


def _progress_for(job_ids: list[str]) -> dict[str, list[str]]:
    if not job_ids:
        return {}
    marks = ",".join("?" * len(job_ids))
    rows = db.connect().execute(
        f"SELECT job_id, line FROM job_progress "
        f"WHERE job_id IN ({marks}) ORDER BY job_id, seq",
        job_ids,
    ).fetchall()
    out: dict[str, list[str]] = {jid: [] for jid in job_ids}
    for row in rows:
        out[row["job_id"]].append(row["line"])
    return out


# --- 起動時の後始末 ---------------------------------------------------------
def recover_interrupted() -> int:
    """前回実行で未完了だったジョブを中断扱いにする。件数を返す。"""
    now = time.time()
    with db.write() as conn:
        cur = conn.execute(
            "UPDATE jobs SET status = ?, error = ?, "
            "finished_at = COALESCE(finished_at, ?) "
            "WHERE status IN (?, ?)",
            (FAILED, "サーバの再起動により中断されました。", now, QUEUED, RUNNING),
        )
        interrupted = cur.rowcount
        # 投稿中フラグはプロセスと運命を共にするので必ず落とす。
        conn.execute("UPDATE jobs SET posting = 0 WHERE posting = 1")
    return interrupted


# --- 参照 -------------------------------------------------------------------
def get(job_id: str, with_progress: bool = True) -> Job | None:
    row = db.connect().execute(_SELECT + " WHERE j.id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    progress = _progress_for([job_id])[job_id] if with_progress else None
    return _row_to_job(row, progress)


def list_jobs(limit: int = DEFAULT_LIST_LIMIT) -> list[Job]:
    """新しい順に最大 limit 件のジョブを返す。

    進捗の全文は実行中・待機中のジョブにだけ載せる。終了したジョブの進捗は
    変化しないうえ件数とともに際限なく増えるため、必要になった時点で
    get() から取得する (一覧は 2 秒間隔でポーリングされる)。
    """
    limit = max(1, min(limit, MAX_LIST_LIMIT))
    rows = db.connect().execute(
        _SELECT + " ORDER BY j.created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    active_ids = [r["id"] for r in rows if r["status"] in _ACTIVE]
    progress = _progress_for(active_ids)
    return [_row_to_job(r, progress.get(r["id"])) for r in rows]


def count() -> int:
    return db.connect().execute("SELECT COUNT(*) FROM jobs").fetchone()[0]


def has_active() -> bool:
    row = db.connect().execute(
        "SELECT 1 FROM jobs WHERE status IN (?, ?) LIMIT 1", _ACTIVE
    ).fetchone()
    return row is not None


# --- 更新 -------------------------------------------------------------------
def _insert(
    conn: sqlite3.Connection,
    url: str,
    owner: str,
    repo: str,
    number: int,
    auto_post: bool,
    include_quality: bool,
) -> str:
    job_id = uuid.uuid4().hex[:12]
    conn.execute(
        "INSERT INTO jobs (id, url, owner, repo, number, status, created_at, "
        "auto_post, include_quality) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (job_id, url, owner, repo, number, QUEUED, time.time(), int(auto_post),
         int(include_quality)),
    )
    return job_id


def create(
    url: str,
    owner: str,
    repo: str,
    number: int,
    auto_post: bool = True,
    include_quality: bool = True,
) -> Job:
    with db.write() as conn:
        job_id = _insert(conn, url, owner, repo, number, auto_post, include_quality)
    job = get(job_id)
    assert job is not None
    return job


@dataclass
class Duplicate:
    """create_unless_reviewed が投入を見送ったときの、先にある同じ PR の記録。

    少なくとも一方は None でない。
    """

    # 待機中・実行中のジョブ。
    job: Job | None
    # 最新のレビュー (reviews.id)。
    review_id: int | None


def create_unless_reviewed(
    url: str,
    owner: str,
    repo: str,
    number: int,
    auto_post: bool = True,
    include_quality: bool = True,
) -> Job | Duplicate:
    """同じ PR をまだレビューしていなければジョブを作る。

    レビュー済みの判定は reviews を見る (ジョブ履歴を消しても残るため)。まだ
    終わっていない待機中・実行中のジョブも済みとして扱い、push が続いたときに
    同じ PR を重ねて積まないようにする。失敗したジョブは数えないので、次の投入で
    やり直せる。

    reviews はオーナーを持たない (REVIEW_OWNER で 1 つに固定される) ため、
    PR はリポジトリ名と番号で照合する。判定と INSERT を同じ書き込みトランザクション
    で行うので、同時に投入されても二重には積まれない。
    """
    with db.write() as conn:
        active = conn.execute(
            "SELECT id FROM jobs WHERE repo = ? AND number = ? AND status IN (?, ?) "
            "ORDER BY created_at DESC LIMIT 1",
            (repo, number, *_ACTIVE),
        ).fetchone()
        review = conn.execute(
            "SELECT MAX(id) AS id FROM reviews WHERE repo = ? AND pr_number = ?",
            (repo, number),
        ).fetchone()
        if active is None and review["id"] is None:
            job_id = _insert(conn, url, owner, repo, number, auto_post, include_quality)
        else:
            job_id = None

    if job_id is None:
        return Duplicate(
            job=get(active["id"]) if active is not None else None,
            review_id=review["id"],
        )
    job = get(job_id)
    assert job is not None
    return job


def delete(job_id: str) -> bool:
    """終了済み (succeeded/failed) のジョブを履歴から削除する。

    レビュー本体 (reviews) は消さない。集計タブからは引き続き参照できる。
    進捗は外部キーの CASCADE で一緒に消える。
    """
    with db.write() as conn:
        cur = conn.execute(
            "DELETE FROM jobs WHERE id = ? AND status NOT IN (?, ?)",
            (job_id, QUEUED, RUNNING),
        )
        return cur.rowcount > 0


def prune(retention_days: int | None = None) -> int:
    """終了から retention_days 日を過ぎたジョブを履歴から削除する。件数を返す。

    delete() と同じくレビュー本体は消さない。ただし対応が要るジョブは残す:
    失敗したジョブと、投稿できるのにまだ投稿していないレビューがそれにあたる。
    同じ PR に後からジョブを投入していれば、やり直したものとみなして消す。
    """
    days = config.JOB_RETENTION_DAYS if retention_days is None else retention_days
    if days <= 0:
        return 0

    cutoff = time.time() - days * 86400
    with db.write() as conn:
        cur = conn.execute(
            "DELETE FROM jobs "
            "WHERE status IN (?, ?) AND posting = 0 AND finished_at < ? "
            "AND ((status = ? AND (posted = 1 OR review_id IS NULL)) "
            "     OR EXISTS (SELECT 1 FROM jobs n WHERE n.owner = jobs.owner "
            "                AND n.repo = jobs.repo AND n.number = jobs.number "
            "                AND n.created_at > jobs.created_at))",
            (SUCCEEDED, FAILED, cutoff, SUCCEEDED),
        )
        return cur.rowcount


def update(job_id: str, **fields: Any) -> Job | None:
    unknown = set(fields) - _UPDATABLE
    if unknown:
        raise ValueError(f"更新できないカラムです: {sorted(unknown)}")
    if not fields:
        return get(job_id)

    assignments = ", ".join(f"{name} = ?" for name in fields)
    values: list[Any] = [
        int(v) if isinstance(v, bool) else v for v in fields.values()
    ]
    values.append(job_id)
    with db.write() as conn:
        cur = conn.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", values)
        if cur.rowcount == 0:
            return None
    return get(job_id)


def append_progress(job_id: str, line: str) -> None:
    try:
        with db.write() as conn:
            conn.execute(
                "INSERT INTO job_progress (job_id, seq, at, line) VALUES "
                "(?, (SELECT COALESCE(MAX(seq), -1) + 1 FROM job_progress "
                "WHERE job_id = ?), ?, ?)",
                (job_id, job_id, time.time(), line),
            )
    except sqlite3.IntegrityError:
        # 実行中にジョブが消された場合。進捗の取りこぼしで実行を止める必要はない。
        return
