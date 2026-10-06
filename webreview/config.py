"""設定値。環境変数で上書き可能。

状態 (ジョブ・レビュー本文・PR タイトル) は SQLite に保存する。ディスク上に残る
ファイルは git のクローンと codex の生ログ (err) だけで、後者はアプリからは
書きっぱなしの診断用ログとして扱う (圧縮・期限削除の対象)。
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
# 必須。未設定 (空) のときはどのリポジトリもレビューできない (fail-closed)。
REVIEW_ORG = os.environ.get("REVIEW_ORG", "").strip()

# codex に渡すレビューモデル。
REVIEW_MODEL = os.environ.get("REVIEW_MODEL", "gpt-6.1-sol")

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

# codex の生ログ (err) を置くルート。レビュー本文は DB に入るのでここには残らない。
LOGS_PATH = _env_path("REVIEW_LOGS_PATH", BASE_DIR / "data" / "review_logs")

# 状態の保存先。ジョブ・進捗・レビュー本文・PR タイトルをすべてここに持つ。
DB_PATH = _env_path("REVIEW_DB_PATH", BASE_DIR / "data" / "review.db")


# --- ログの保持 --------------------------------------------------------------
# codex の err ログは PR の差分規模に比例して伸びるため、完了時に gzip し、
# 一定期間で削除する。本文と詳細は DB にあるので、消えても画面表示は変わらない。
# 0 を指定すると削除しない。
STDERR_RETENTION_DAYS = int(os.environ.get("REVIEW_STDERR_RETENTION_DAYS", "30"))


# --- 外部コマンド ------------------------------------------------------------
GIT_BIN = os.environ.get("REVIEW_GIT_BIN", "git")
CODEX_BIN = os.environ.get("REVIEW_CODEX_BIN", "codex")

# codex の実行が固まるとワーカーが 1 つしかない都合でキュー全体が止まるため、
# 上限時間を設ける (秒)。0 で無制限。
REVIEW_TIMEOUT = int(os.environ.get("REVIEW_TIMEOUT", "3600"))


# --- サーバ ------------------------------------------------------------------
HOST = os.environ.get("REVIEW_HOST", "0.0.0.0")
PORT = int(os.environ.get("REVIEW_PORT", "8080"))


def ensure_dirs() -> None:
    """必要なディレクトリを作成する。"""
    WORK_PATH.mkdir(parents=True, exist_ok=True)
    LOGS_PATH.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
