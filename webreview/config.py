"""設定値。環境変数で上書き可能。

既存スクリプト (auto_review.sh / list_review.sh) と同じ既定値を採用し、
ログのファイル名レイアウトも互換に保つ。
"""

from __future__ import annotations

import os
from pathlib import Path

# プロジェクトルート (このファイルの 1 つ上のディレクトリ)
BASE_DIR = Path(__file__).resolve().parent.parent


def _env_path(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(value).expanduser().resolve() if value else default


# --- レビュー対象の制約 ------------------------------------------------------
# 許可する GitHub Organization。これ以外のリポジトリはレビューできない。
REVIEW_ORG = os.environ.get("REVIEW_ORG", "your-org")

# codex に渡すレビューモデル。
REVIEW_MODEL = os.environ.get("REVIEW_MODEL", "gpt-5.6-terra")


# --- データの保存先 ----------------------------------------------------------
# リポジトリをクローンする作業ディレクトリ。
WORK_PATH = _env_path("REVIEW_WORK_PATH", BASE_DIR / "data" / "work")

# レビューログのルート。list_review.sh の ROOT_DIR="review_logs" に相当。
LOGS_PATH = _env_path("REVIEW_LOGS_PATH", BASE_DIR / "data" / "review_logs")

# ジョブ履歴の永続化先 (再起動しても投稿済みなどの状態を保持するため)。
JOBS_FILE = _env_path("REVIEW_JOBS_FILE", BASE_DIR / "data" / "jobs.json")


# --- 外部コマンド ------------------------------------------------------------
GH_BIN = os.environ.get("REVIEW_GH_BIN", "gh")
GIT_BIN = os.environ.get("REVIEW_GIT_BIN", "git")
CODEX_BIN = os.environ.get("REVIEW_CODEX_BIN", "codex")


# --- サーバ ------------------------------------------------------------------
HOST = os.environ.get("REVIEW_HOST", "0.0.0.0")
PORT = int(os.environ.get("REVIEW_PORT", "8080"))


def ensure_dirs() -> None:
    """必要なディレクトリを作成する。"""
    WORK_PATH.mkdir(parents=True, exist_ok=True)
    LOGS_PATH.mkdir(parents=True, exist_ok=True)
    JOBS_FILE.parent.mkdir(parents=True, exist_ok=True)
