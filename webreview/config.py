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


def _owners(value: str) -> tuple[str, ...]:
    """カンマ区切りのオーナー名を、空要素と重複を除いて書かれた順に並べる。"""
    owners: list[str] = []
    for name in value.split(","):
        name = name.strip()
        if name and name not in owners:
            owners.append(name)
    return tuple(owners)


# --- レビュー対象の制約 ------------------------------------------------------
# 許可するリポジトリのオーナー (Organization 名または個人アカウント名)。
# カンマ区切りで複数指定できる。これ以外のリポジトリはレビューできない。
# 必須。未設定 (空) のときはどのリポジトリもレビューできない (fail-closed)。
# 「すべて許可」の指定は設けない — 認証の無いこのツールでは、オーナーの制約が
# レビュー・投稿できる範囲を絞る唯一の関門なので。
REVIEW_OWNERS = _owners(os.environ.get("REVIEW_OWNER", ""))

# codex に渡すレビューモデル。
REVIEW_MODEL = os.environ.get("REVIEW_MODEL", "gpt-6.1-sol")

# codex に渡す推論の深さ (model_reasoning_effort)。
# 空にするとオプション自体を渡さず、モデル/codex 側の既定に従う。
REVIEW_REASONING_EFFORT = os.environ.get("REVIEW_REASONING_EFFORT", "medium").strip()

# 画面上部に表示する説明文 (任意)。書式は README「説明文の書式」を参照。
# .env には 1 行でしか書けないため、リテラルの "\n" を改行として解釈する。
REVIEW_DESCRIPTION = os.environ.get("REVIEW_DESCRIPTION", "").replace("\\n", "\n").strip()


# --- PR をホストするサービス ------------------------------------------------
# "github" = github.com と、GitHub 互換の REST API を持つサービス (GITHUB_* で設定)。
# "gitlab" = GitLab (GITLAB_* で設定)。
# 1 つのインスタンスで扱えるのはどちらか一方だけ。
REVIEW_FORGE = os.environ.get("REVIEW_FORGE", "").strip().lower() or "github"

# GitLab の URL (例: https://gitlab.example.com)。API はこの下の /api/v4。
GITLAB_URL = os.environ.get("GITLAB_URL", "").strip().rstrip("/")

# GitLab のアクセストークン。グループアクセストークン (Reporter、スコープ api と
# read_repository) を想定する。README「GitLab」を参照。
GITLAB_TOKEN = os.environ.get("GITLAB_TOKEN", "").strip()

# GitHub の既定は github.com。GitHub 互換の REST API を持つセルフホストのサービスを
# 使うときは、以下をそのサービスに合わせる (README「GitHub 互換のサービス」)。

# Web の URL。PR の URL はこの下の /<owner>/<repo>/pull/<番号> の形で受け付ける。
GITHUB_WEB_URL = (
    os.environ.get("GITHUB_WEB_URL", "").strip() or "https://github.com"
).rstrip("/")

# REST API のベース URL。
GITHUB_API_URL = (
    os.environ.get("GITHUB_API_URL", "").strip() or "https://api.github.com"
).rstrip("/")

# 作業ディレクトリの origin にする URL。{web} / {owner} / {repo} を置き換える。
GITHUB_CLONE_URL = (
    os.environ.get("GITHUB_CLONE_URL", "").strip() or "{web}/{owner}/{repo}.git"
)

# git の認証に使うユーザー名。github.com はトークンの種類によらずこの固定値で通るが、
# サービスによってはトークンの持ち主のアカウント名でないと通らない。
GITHUB_GIT_USERNAME = (
    os.environ.get("GITHUB_GIT_USERNAME", "").strip() or "x-access-token"
)

# 投稿の方法。
# "review" = レビュー API (pulls/{n}/reviews) で、指摘をソース行へのコメントにする。
# "issue_comment" = 本文全体を 1 件のコメント (issues/{n}/comments) として投稿する。
#                   レビュー API の無いサービス向け。
GITHUB_POST_MODE = os.environ.get("GITHUB_POST_MODE", "").strip().lower() or "review"


# --- GitHub 認証 --------------------------------------------------------------
# "pat" = GH_TOKEN の Personal Access Token をそのまま使う。
# "app" = GitHub App の秘密鍵から installation access token を都度発行する。
GITHUB_AUTH_MODE = os.environ.get("GITHUB_AUTH_MODE", "pat").strip().lower()

# pat モード用。
GH_TOKEN = os.environ.get("GH_TOKEN", "")

# app モード用。CLIENT_ID は JWT の iss に使う (GitHub のドキュメントは App ID より
# Client ID を推奨しているが、App ID を入れても動作する)。
GITHUB_APP_CLIENT_ID = os.environ.get("GITHUB_APP_CLIENT_ID", "").strip()
# 秘密鍵はファイルで渡す (read-only マウントを想定)。
GITHUB_APP_PRIVATE_KEY_PATH = os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH", "").strip()
# 省略時は REVIEW_OWNER の installation を API で解決する。オーナーごとに
# installation が分かれるので、指定できるのはオーナーが 1 つのときだけ。
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

# 終了したジョブを「レビュー実行」タブの一覧から自動で消すまでの日数。レビュー本体は
# 消さないので、集計タブからは引き続き見られる。失敗したジョブと未投稿のレビューは
# 対応が要るので、同じ PR をやり直していない限り残す。0 を指定すると削除しない。
JOB_RETENTION_DAYS = int(os.environ.get("REVIEW_JOB_RETENTION_DAYS", "30"))


# --- 外部コマンド ------------------------------------------------------------
GIT_BIN = os.environ.get("REVIEW_GIT_BIN", "git")
CODEX_BIN = os.environ.get("REVIEW_CODEX_BIN", "codex")

# codex の実行が固まるとワーカーが 1 つしかない都合でキュー全体が止まるため、
# 上限時間を設ける (秒)。0 で無制限。
REVIEW_TIMEOUT = int(os.environ.get("REVIEW_TIMEOUT", "3600"))


# --- サーバ ------------------------------------------------------------------
# 認証が無いため、既定では実行している PC 自身からしか接続できないようにする。
HOST = os.environ.get("REVIEW_HOST", "127.0.0.1")
PORT = int(os.environ.get("REVIEW_PORT", "8080"))


def repo_work_path(owner: str, repo: str) -> Path:
    """リポジトリの作業ツリー。オーナーが違えば同じ名前のリポジトリも別の場所になる。"""
    return WORK_PATH / owner / repo


def legacy_repo_work_path(repo: str) -> Path:
    """オーナーを 1 つに固定していた頃の作業ツリー (REVIEW_WORK_PATH/<repo>)。

    その頃のレビュー本文に残るパスを、リポジトリからの相対パスに直すときにだけ使う。
    """
    return WORK_PATH / repo


def repo_logs_path(owner: str, repo: str) -> Path:
    """リポジトリの codex 生ログの置き場所。"""
    return LOGS_PATH / owner / repo


def ensure_dirs() -> None:
    """必要なディレクトリを作成する。"""
    WORK_PATH.mkdir(parents=True, exist_ok=True)
    LOGS_PATH.mkdir(parents=True, exist_ok=True)
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
