#!/usr/bin/env bash
set -e

# 認証はすべて環境変数で受け取り、ホストの認証ディレクトリは共有しない。
#   - GitHub CLI (gh) は GH_TOKEN を自動的に利用する。
#   - Codex CLI は CODEX_API_KEY を利用する。
#   - private リポジトリの git clone/fetch を通すため、GH_TOKEN を使った
#     https 認証を git に設定する (トークンはコンテナ内 ~/.gitconfig のみ)。

if [ -n "${GH_TOKEN:-}" ]; then
  git config --global \
    url."https://x-access-token:${GH_TOKEN}@github.com/".insteadOf \
    "https://github.com/"
fi

# volume 上のリポジトリで "dubious ownership" エラーを避ける。
git config --global --add safe.directory '*'

exec "$@"
