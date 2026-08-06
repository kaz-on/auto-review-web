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

# codex に渡す推論の深さ (model_reasoning_effort)。
# 空にするとオプション自体を渡さず、モデル/codex 側の既定に従う。
REVIEW_REASONING_EFFORT = os.environ.get("REVIEW_REASONING_EFFORT", "medium").strip()

# 画面上部に表示する説明文 (任意)。書式は README「説明文の書式」を参照。
# .env には 1 行でしか書けないため、リテラルの "\n" を改行として解釈する。
REVIEW_DESCRIPTION = os.environ.get("REVIEW_DESCRIPTION", "").replace("\\n", "\n").strip()


# --- GitHub 認証 --------------------------------------------------------------
# "pat" = GH_TOKEN の Personal Access Token をそのまま使う。
# "app" = GitHub App の秘密鍵から installation access token を都度発行する。
GITHUB_AUTH_MODE = os.environ.get("GITHUB_AUTH_MODE", "pat").strip().lower()

# GitHub REST API のベース URL (GitHub Enterprise Server では変更する)。
GITHUB_API_URL = os.environ.get("GITHUB_API_URL", "https://api.github.com").rstrip("/")

# pat モード用。
GH_TOKEN = os.environ.get("GH_TOKEN", "")

# app モード用。CLIENT_ID は JWT の iss に使う (GitHub のドキュメントは App ID より
# Client ID を推奨しているが、App ID を入れても動作する)。
GITHUB_APP_CLIENT_ID = os.environ.get("GITHUB_APP_CLIENT_ID", "").strip()
# 秘密鍵は PATH (推奨: read-only マウント) か PEM 文字列そのもので渡す。
GITHUB_APP_PRIVATE_KEY_PATH = os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH", "").strip()
GITHUB_APP_PRIVATE_KEY = os.environ.get("GITHUB_APP_PRIVATE_KEY", "")
# 省略時は REVIEW_ORG の installation を API で解決する。
GITHUB_APP_INSTALLATION_ID = os.environ.get("GITHUB_APP_INSTALLATION_ID", "").strip()

# installation access token (有効期間 1 時間) の残りがこれを下回ったら再発行する (秒)。
GITHUB_TOKEN_REFRESH_MARGIN = int(os.environ.get("GITHUB_TOKEN_REFRESH_MARGIN", "300"))


# --- データの保存先 ----------------------------------------------------------
# リポジトリをクローンする作業ディレクトリ。
WORK_PATH = _env_path("REVIEW_WORK_PATH", BASE_DIR / "data" / "work")

# レビューログのルート。list_review.sh の ROOT_DIR="review_logs" に相当。
LOGS_PATH = _env_path("REVIEW_LOGS_PATH", BASE_DIR / "data" / "review_logs")

# ジョブ履歴の永続化先 (再起動しても投稿済みなどの状態を保持するため)。
JOBS_FILE = _env_path("REVIEW_JOBS_FILE", BASE_DIR / "data" / "jobs.json")


# --- 外部コマンド ------------------------------------------------------------
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
