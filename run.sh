#!/usr/bin/env bash
# Web レビューシステムの起動スクリプト。
# 初回は venv を作成して依存をインストールし、以降はそれを再利用する。
set -euo pipefail

cd "$(dirname "$0")"

VENV_DIR=".venv"

if [[ ! -d "$VENV_DIR" ]]; then
  echo "Creating virtualenv..."
  python3 -m venv "$VENV_DIR"
  "$VENV_DIR/bin/pip" install --upgrade pip >/dev/null
fi

# 依存が増えたときに既存 venv でも取り込めるよう、起動のたびに確認する。
"$VENV_DIR/bin/pip" install -q -r requirements.txt

# .env があれば読み込む (set -a により、中の代入はそのまま export される)。
# 環境変数を自分で export して起動する使い方もあるので、無くても止めない。
if [[ -f .env ]]; then
  set -a
  source .env
  set +a
fi

# LAN 内の他 PC からアクセスできるよう 0.0.0.0 で待ち受ける (config の既定値)。
exec "$VENV_DIR/bin/python" -m webreview
