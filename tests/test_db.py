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
                "INSERT INTO reviews (repo, pr_number, rev, created_at, body) "
                "VALUES ('r', 1, 0, 1.0, 'x')"
            )
            raise ValueError("途中で失敗")

    assert db.connect().execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0
    # ロールバック後もトランザクションを開始できる (開きっぱなしになっていない)。
    with db.write() as conn:
        conn.execute("SELECT 1")


def test_書き込みは正常終了でコミットされる(temp_db):
    with db.write() as conn:
        conn.execute(
            "INSERT INTO reviews (repo, pr_number, rev, created_at, body) "
            "VALUES ('r', 1, 0, 1.0, 'x')"
        )

    _reset_db_connections()
    assert db.connect().execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 1
