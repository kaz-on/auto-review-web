"""テスト全体で使う共通のフィクスチャ。

このアプリの設定 (config) は import 時に環境変数から読み込まれる定数なので、
テストでは環境変数ではなく config の属性を monkeypatch で差し替える。

db は接続をスレッドローカルに持つため、テストごとに一時ファイルへ向け直したうえで
キャッシュ済みの接続を捨てる必要がある。
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from webreview import config, db, forge, github_auth


def _reset_db_connections() -> None:
    """スレッドローカルにキャッシュされた接続を捨てる。

    _local を作り直すと、他スレッドも次回 connect() で現在の DB_PATH へ繋ぎ直す。
    """
    conn = getattr(db._local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    db._local = threading.local()


@pytest.fixture(autouse=True)
def isolate_data_paths(tmp_path, monkeypatch):
    """どのテストも本番の data/ を触れないようにする。

    個別のフィクスチャ (temp_db / temp_paths) が同じ属性を更に差し替えるが、
    ここで先に逃がしておくことで、指定を忘れたテストでも実データに届かない。
    """
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "guard" / "review.db")
    monkeypatch.setattr(config, "WORK_PATH", tmp_path / "guard" / "work")
    monkeypatch.setattr(config, "LOGS_PATH", tmp_path / "guard" / "logs")


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """テスト専用の空の SQLite を用意する。"""
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "review.db")
    _reset_db_connections()
    db.init()
    yield
    _reset_db_connections()


@pytest.fixture
def temp_paths(tmp_path, monkeypatch):
    """作業ディレクトリとログ置き場を一時ディレクトリに向ける。"""
    work = tmp_path / "work"
    logs = tmp_path / "logs"
    work.mkdir()
    logs.mkdir()
    monkeypatch.setattr(config, "WORK_PATH", work)
    monkeypatch.setattr(config, "LOGS_PATH", logs)
    return work, logs


@pytest.fixture(autouse=True)
def reset_auth_provider():
    """プロセス全体で共有される TokenProvider がテスト間に漏れないようにする。"""
    github_auth._PROVIDERS.clear()
    yield
    github_auth._PROVIDERS.clear()


@pytest.fixture(autouse=True)
def reset_forge():
    """forge は設定から 1 度だけ組み立てるので、テストで設定を変えたら作り直させる。"""
    forge._FORGE = None
    yield
    forge._FORGE = None


@pytest.fixture
def review_owner(monkeypatch):
    """レビュー対象のオーナーを固定する。"""
    monkeypatch.setattr(config, "REVIEW_OWNERS", ("example-org",))
    return "example-org"


class FakeProvider:
    """github_api.TokenProvider の最小実装。invalidate の呼ばれ方も観測できる。"""

    def __init__(self, *tokens: str):
        self.tokens = list(tokens) or ["token-0"]
        self.index = 0
        self.invalidated = 0

    def token(self) -> str:
        return self.tokens[min(self.index, len(self.tokens) - 1)]

    def invalidate(self) -> None:
        self.invalidated += 1
        self.index += 1


@pytest.fixture
def fake_provider():
    return FakeProvider("token-0")
