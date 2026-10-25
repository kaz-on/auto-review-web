"""接続とスキーマ。

WAL と外部キーは接続ごとの設定なので、connect() が毎回入れていることを確かめる。
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from webreview import config, db

from conftest import _reset_db_connections


def test_接続には_wal_と外部キーが効いている(temp_db):
    conn = db.connect()

    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == db._BUSY_TIMEOUT_MS
    # 行は列名で引ける。
    assert conn.row_factory is sqlite3.Row


def test_同じスレッドでは接続を使い回す(temp_db):
    assert db.connect() is db.connect()


def test_スレッドごとに別の接続を持つ(temp_db):
    """sqlite3 の接続はスレッド間で共有できない。"""
    main = db.connect()
    other: list = []

    t = threading.Thread(target=lambda: other.append(db.connect()))
    t.start()
    t.join()

    assert other[0] is not main


def test_初期化は繰り返しても壊れない(temp_db):
    db.init()
    db.init()

    assert db.connect().execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION


def test_接続先が無ければディレクトリごと作る(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "deep" / "nested" / "review.db")
    _reset_db_connections()
    try:
        db.init()
        assert config.DB_PATH.exists()
    finally:
        _reset_db_connections()


def test_古い_db_には後から足した列を補う(tmp_path, monkeypatch):
    """CREATE TABLE IF NOT EXISTS は既存のテーブルに列を追加しないため。"""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "review.db")
    _reset_db_connections()
    try:
        conn = db.connect()
        # head_commit を持たない、列を足す前のスキーマ。
        conn.execute(
            "CREATE TABLE reviews (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "repo TEXT NOT NULL, pr_number INTEGER NOT NULL, rev INTEGER NOT NULL, "
            "created_at REAL NOT NULL, body TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO reviews (repo, pr_number, rev, created_at, body) "
            "VALUES ('r', 1, 0, 1.0, '既存の本文')"
        )
        conn.execute("PRAGMA user_version=1")

        db.init()

        columns = {row["name"] for row in conn.execute("PRAGMA table_info(reviews)")}
        assert {"head_commit", "pr_author", "pr_additions"} <= columns
        # 既存の行は消えず、足した列は NULL で埋まる。
        row = conn.execute("SELECT body, head_commit FROM reviews").fetchone()
        assert (row["body"], row["head_commit"]) == ("既存の本文", None)
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    finally:
        _reset_db_connections()


# オーナーを持たなかった頃 (スキーマ 4) の、移行に関わるテーブル。
_V4_SCHEMA = """
CREATE TABLE reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo TEXT NOT NULL, pr_number INTEGER NOT NULL, rev INTEGER NOT NULL,
    created_at REAL NOT NULL, head_commit TEXT, body TEXT NOT NULL,
    UNIQUE (repo, pr_number, rev)
);
CREATE INDEX idx_reviews_repo_pr ON reviews (repo, pr_number);
CREATE TABLE jobs (
    id TEXT PRIMARY KEY, url TEXT NOT NULL, owner TEXT NOT NULL,
    repo TEXT NOT NULL, number INTEGER NOT NULL, status TEXT NOT NULL,
    created_at REAL NOT NULL,
    review_id INTEGER REFERENCES reviews (id) ON DELETE SET NULL
);
CREATE TABLE pr_titles (
    repo TEXT NOT NULL, pr_number INTEGER NOT NULL, title TEXT NOT NULL,
    fetched_at REAL NOT NULL, PRIMARY KEY (repo, pr_number)
);
INSERT INTO reviews (id, repo, pr_number, rev, created_at, head_commit, body)
    VALUES (1, 'r', 1, 0, 1.0, 'h1', 'ジョブの残るレビュー'),
           (2, 'r', 1, 1, 2.0, 'h2', 'ジョブの消えたレビュー');
INSERT INTO jobs (id, url, owner, repo, number, status, created_at, review_id)
    VALUES ('j1', 'u', 'org-b', 'r', 1, 'succeeded', 1.0, 1);
INSERT INTO pr_titles VALUES ('r', 1, 'タイトル', 1.0);
PRAGMA user_version=4;
"""


def test_オーナーを持たない_db_はオーナー付きに作り直す(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "review.db")
    monkeypatch.setattr(config, "REVIEW_OWNERS", ("org-a", "org-b"))
    _reset_db_connections()
    try:
        conn = db.connect()
        conn.executescript(_V4_SCHEMA)

        db.init()

        rows = conn.execute(
            "SELECT id, owner, repo, head_commit, body FROM reviews ORDER BY id"
        ).fetchall()
        # ジョブが残っていればそのオーナー、無ければ REVIEW_OWNER の先頭で埋める。
        assert [tuple(r) for r in rows] == [
            (1, "org-b", "r", "h1", "ジョブの残るレビュー"),
            (2, "org-a", "r", "h2", "ジョブの消えたレビュー"),
        ]
        # 作り直しで ON DELETE SET NULL が働いていない。
        assert conn.execute("SELECT review_id FROM jobs").fetchone()[0] == 1
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        # pr_titles は取り直せるキャッシュなので空で作り直す。
        assert "owner" in db._columns(conn, "pr_titles")
        assert conn.execute("SELECT COUNT(*) FROM pr_titles").fetchone()[0] == 0
        # 足した列も揃い、id は続きから振られる。
        assert {"pr_author", "include_quality"} <= set(db._columns(conn, "reviews"))
        cur = conn.execute(
            "INSERT INTO reviews (owner, repo, pr_number, rev, created_at, body) "
            "VALUES ('org-c', 'r', 1, 0, 3.0, '別オーナーの rev-0')"
        )
        assert cur.lastrowid == 3
        assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION

        # 作り直したあとは何度 init しても変わらない。
        db.init()
        assert conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 3
    finally:
        _reset_db_connections()


def test_オーナー未設定で作り直すとジョブの無いレビューは空のオーナーになる(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "review.db")
    monkeypatch.setattr(config, "REVIEW_OWNERS", ())
    _reset_db_connections()
    try:
        conn = db.connect()
        conn.executescript(_V4_SCHEMA)

        db.init()

        owners = [r[0] for r in conn.execute("SELECT owner FROM reviews ORDER BY id")]
        assert owners == ["org-b", ""]
    finally:
        _reset_db_connections()


def test_新しいスキーマの_db_では起動しない(tmp_path, monkeypatch):
    """アプリを巻き戻したときにデータを壊さないための関門。"""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "review.db")
    _reset_db_connections()
    try:
        db.connect().execute(f"PRAGMA user_version={db.SCHEMA_VERSION + 1}")
        with pytest.raises(RuntimeError, match="このアプリ"):
            db.init()
    finally:
        _reset_db_connections()


def test_書き込みは例外でロールバックされる(temp_db):
    with pytest.raises(ValueError):
        with db.write() as conn:
            conn.execute(
                "INSERT INTO reviews (owner, repo, pr_number, rev, created_at, body) "
                "VALUES ('o', 'r', 1, 0, 1.0, 'x')"
            )
            raise ValueError("途中で失敗")

    assert db.connect().execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0
    # ロールバック後もトランザクションを開始できる (開きっぱなしになっていない)。
    with db.write() as conn:
        conn.execute("SELECT 1")


def test_書き込みは正常終了でコミットされる(temp_db):
    with db.write() as conn:
        conn.execute(
            "INSERT INTO reviews (owner, repo, pr_number, rev, created_at, body) "
            "VALUES ('o', 'r', 1, 0, 1.0, 'x')"
        )

    _reset_db_connections()
    assert db.connect().execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 1
