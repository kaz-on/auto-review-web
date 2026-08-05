#!/usr/bin/env bash
set -e

# 認証はすべて環境変数で受け取り、ホストの認証ディレクトリは共有しない。
#   - GitHub CLI (gh) は GH_TOKEN を自動的に利用する。
#   - Codex CLI は CODEX_API_KEY を利用する。
#   - private リポジトリの git clone/fetch を通すため、
#     GitHub CLI を git の認証情報ヘルパーとして設定する。

gh config set git_protocol https
gh auth setup-git

exec "$@"
