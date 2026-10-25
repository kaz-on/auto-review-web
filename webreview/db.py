"""SQLite の接続管理とスキーマ定義。

アプリの状態 (ジョブ・進捗・レビュー本文・PR タイトル) はすべてこの 1 ファイルに
入る。Flask のリクエストスレッドとワーカースレッドから同時に触られるため、
WAL を有効にして「複数リーダー + 単一ライター」で運用する。接続はスレッドごとに
持つ (sqlite3 の接続はスレッド間で共有できないため)。

書き込みはワーカー 1 本と投稿スレッドに限られるので、競合は busy_timeout の待ちで
十分に吸収できる。
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from . import config

# ロック待ちの上限。単一ワーカー構成なので長時間の競合は起きない想定。
_BUSY_TIMEOUT_MS = 10_000

_local = threading.local()

SCHEMA_VERSION = 5

# reviews はオーナーを足したときに作り直す必要があったので (UNIQUE 制約は後から
# 変えられない)、移行先の一時テーブルにも同じ定義を使えるよう名前を差し込む形にしてある。
_REVIEWS_TABLE = """
CREATE TABLE IF NOT EXISTS {name} (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    -- PR のリポジトリのオーナー。REVIEW_OWNER が 1 つに固定だった頃は持っていなかった。
    owner            TEXT    NOT NULL,
    repo             TEXT    NOT NULL,
    pr_number        INTEGER NOT NULL,
    rev              INTEGER NOT NULL,
    created_at       REAL    NOT NULL,
    base_ref         TEXT,
    base_commit      TEXT,
    -- レビューした PR の head。行コメントの commit_id に使う。
    head_commit      TEXT,
    -- 実行時の codex 設定。以前は本文先頭のヘッダ行に埋め込んでいたが、
    -- 本文を検索可能に保つためカラムへ出した (表示・投稿時に組み立てる)。
    model            TEXT,
    reasoning_effort TEXT,
    codex_version    TEXT,
    session_id       TEXT,
    -- レビュー本文 (ヘッダを含まない生の codex 出力)。
    body             TEXT    NOT NULL,
    -- err ログ先頭の実行条件ブロック。集計時に err を開かないよう実行時に抽出する。
    details          TEXT,
    -- バグ以外の品質改善も指摘対象に含めて実行したか。プロンプトが変わるため、
    -- 本文を読み比べるときの前提として残す。
    include_quality  INTEGER NOT NULL DEFAULT 0,
    -- 集計用の PR の情報。作成者は変わらず、変更の規模は head_commit 時点のもの
    -- なので、PR ごとのキャッシュ (pr_titles) ではなくレビューに記録する。
    -- 記録を始める前のレビューで、あとから補完できなかったものは NULL。
    pr_author        TEXT,
    pr_additions     INTEGER,
    pr_deletions     INTEGER,
    pr_changed_files INTEGER,
    -- err ログの実体。期限削除したら NULL に戻し pruned_at を立てる。
    stderr_path      TEXT,
    stderr_pruned_at REAL,
    UNIQUE (owner, repo, pr_number, rev)
);
"""

_SCHEMA = _REVIEWS_TABLE.format(name="reviews") + """
CREATE INDEX IF NOT EXISTS idx_reviews_created_at ON reviews (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reviews_pr         ON reviews (owner, repo, pr_number);

CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT    PRIMARY KEY,
    url         TEXT    NOT NULL,
    owner       TEXT    NOT NULL,
    repo        TEXT    NOT NULL,
    number      INTEGER NOT NULL,
    status      TEXT    NOT NULL,
    created_at  REAL    NOT NULL,
    started_at  REAL,
    finished_at REAL,
    error       TEXT,
    auto_post   INTEGER NOT NULL DEFAULT 1,
    -- 品質改善も指摘対象に含めるか。既定は API と store.create で決めるため、
    -- ここの DEFAULT は列を後から足した既存行 (品質指摘なしで実行された) 用。
    include_quality INTEGER NOT NULL DEFAULT 0,
    posted      INTEGER NOT NULL DEFAULT 0,
    posting     INTEGER NOT NULL DEFAULT 0,
    post_error  TEXT,
    -- 成功したジョブが生成したレビュー。レビューを消してもジョブ履歴は残す。
    review_id   INTEGER REFERENCES reviews (id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs (created_at DESC);

CREATE TABLE IF NOT EXISTS job_progress (
    job_id TEXT    NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    seq    INTEGER NOT NULL,
    at     REAL    NOT NULL,
    line   TEXT    NOT NULL,
    PRIMARY KEY (job_id, seq)
);

-- PR タイトルのキャッシュ。fetched_at で失効させる (PR 名は変更されうる)。
CREATE TABLE IF NOT EXISTS pr_titles (
    owner      TEXT    NOT NULL,
    repo       TEXT    NOT NULL,
    pr_number  INTEGER NOT NULL,
    title      TEXT    NOT NULL,
    fetched_at REAL    NOT NULL,
    PRIMARY KEY (owner, repo, pr_number)
);
"""


# 後から足した列。CREATE TABLE IF NOT EXISTS は既存のテーブルに列を追加しないため、
# 足りないぶんだけ ALTER TABLE で補う。列の有無だけを見るので、新規作成の DB でも
# 途中で失敗した DB でも同じ手順で辻褄が合う (スキーマ全体と同じく再実行できる)。
_ADDED_COLUMNS = (
    ("reviews", "head_commit", "TEXT"),
    ("reviews", "include_quality", "INTEGER NOT NULL DEFAULT 0"),
    ("jobs", "include_quality", "INTEGER NOT NULL DEFAULT 0"),
    ("reviews", "pr_author", "TEXT"),
    ("reviews", "pr_additions", "INTEGER"),
    ("reviews", "pr_deletions", "INTEGER"),
    ("reviews", "pr_changed_files", "INTEGER"),
)


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    """テーブルの列名。テーブルが無ければ空。"""
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, column, decl in _ADDED_COLUMNS:
        if column not in _columns(conn, table):
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def _add_owner(conn: sqlite3.Connection) -> None:
    """オーナーを持たない reviews / pr_titles を、オーナー付きの形に作り直す。

    REVIEW_OWNER が 1 つに固定だった頃の DB が対象。UNIQUE 制約と主キーにオーナーを
    含めるため、列の追加では済まずテーブルを作り直す。

    既存のレビューのオーナーは、そのレビューを生んだジョブから引く。ジョブが消えて
    いれば、REVIEW_OWNER の先頭を使う (当時はオーナーが 1 つだったので、設定を
    変える前に移行すれば正しく埋まる)。pr_titles は取り直せるキャッシュなので、
    中身は引き継がずに作り直す。

    reviews を消すと jobs.review_id の ON DELETE SET NULL が働いてしまうため、
    外部キーを切った状態で作り直す (SQLite が推奨する手順)。PRAGMA foreign_keys は
    トランザクションの中では変えられないので、その外側で切り替える。
    """
    columns = _columns(conn, "reviews")
    if not columns or "owner" in columns:
        return

    fallback = config.REVIEW_OWNERS[0] if config.REVIEW_OWNERS else ""
    # 作り直す前のスキーマに jobs が無いことはまずないが、無ければ設定から埋める。
    if _columns(conn, "jobs"):
        owner_expr = (
            "COALESCE((SELECT j.owner FROM jobs j WHERE j.review_id = r.id "
            "ORDER BY j.created_at DESC LIMIT 1), ?)"
        )
    else:
        owner_expr = "?"
    copied = ", ".join(columns)

    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        with write() as w:
            w.execute(_REVIEWS_TABLE.format(name="reviews_new"))
            w.execute(
                f"INSERT INTO reviews_new (owner, {copied}) "
                f"SELECT {owner_expr}, {copied} FROM reviews r",
                (fallback,),
            )
            w.execute("DROP TABLE reviews")
            w.execute("ALTER TABLE reviews_new RENAME TO reviews")
            w.execute("DROP TABLE IF EXISTS pr_titles")
            # 作り直しで参照先が欠けていないか (jobs.review_id は reviews の id を保つ)。
            if w.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RuntimeError("reviews の作り直しで外部キーの整合が崩れました。")
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _configure(conn: sqlite3.Connection) -> None:
    conn.row_factory = sqlite3.Row
    # WAL はデータベース単位の永続設定だが、接続ごとに実行しても害はない。
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    # ON DELETE CASCADE / SET NULL を効かせるため接続ごとに必要。
    conn.execute("PRAGMA foreign_keys=ON")


def connect() -> sqlite3.Connection:
    """呼び出しスレッド用の接続を返す (なければ作る)。"""
    conn = getattr(_local, "conn", None)
    if conn is None:
        config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(config.DB_PATH), isolation_level=None)
        _configure(conn)
        _local.conn = conn
    return conn


@contextmanager
def write() -> Iterator[sqlite3.Connection]:
    """書き込みトランザクション。例外時はロールバックする。

    isolation_level=None で自動コミットを切っているため、範囲は明示的に区切る。
    IMMEDIATE にして書き込みロックを最初に取り、途中でのロック昇格失敗を避ける。
    """
    conn = connect()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def init() -> None:
    """スキーマを作成し、バージョンを記録する。"""
    conn = connect()
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > SCHEMA_VERSION:
        raise RuntimeError(
            f"データベースのスキーマ ({current}) がこのアプリ "
            f"({SCHEMA_VERSION}) より新しいため起動できません: {config.DB_PATH}"
        )
    # オーナーを足す作り直しは、新しいスキーマの索引 (オーナーを含む) より先に行う。
    # 作り直しは 1 つのトランザクションなので、途中で失敗しても元の形のまま残る。
    _add_owner(conn)
    # executescript は暗黙に COMMIT を挟むため、明示トランザクションでは包まない。
    # CREATE ... IF NOT EXISTS と不足列の追加だけなので、途中で失敗しても
    # 再実行で回復できる。
    conn.executescript(_SCHEMA)
    _add_missing_columns(conn)
    if current < SCHEMA_VERSION:
        # PRAGMA はプレースホルダを取らないため直接埋め込む (定数なので安全)。
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
