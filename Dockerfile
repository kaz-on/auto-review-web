FROM python:3.12-slim

# --- curl & git ----------------------------------------------
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        curl git \
    && rm -rf /var/lib/apt/lists/*

# Codex CLI を公式インストールスクリプトで導入し、PATH 上に配置する。
ENV PATH="/root/.local/bin:/usr/local/bin:${PATH}"
RUN curl -fsSL https://chatgpt.com/codex/install.sh | sh \
    && target="$(command -v codex || true)" \
    && if [ -z "$target" ]; then \
         target=$(find / -name codex -perm -u+x 2>/dev/null | head -1); \
       fi \
    && [ -n "$target" ] \
    && ln -sf "$target" /usr/local/bin/codex \
    && codex --version

# bubblewrap は明示的にインストールせず、Codex CLI 同梱の bubblewrap を使用する。
# apt で bubblewrap をインストールすると一部の環境で権限エラーで動作しないため。
# codex 実行時に以下の警告が表示されるが問題はない。
# warning: Codex could not find bubblewrap on PATH. Install bubblewrap with your OS package manager. See the sandbox prerequisites: https://developers.openai.com/codex/concepts/sandboxing#prerequisites. Codex will use the bundled bubblewrap in the meantime.

# --- アプリケーション -------------------------------------------------------
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY webreview ./webreview

# データ (DB・クローンしたリポジトリ・codex の生ログ) は /data 以下。volume で永続化。
# DB は既存の review_logs volume に置くので、更新時に volume 定義を変えなくてよい。
ENV REVIEW_WORK_PATH=/data/work \
    REVIEW_LOGS_PATH=/data/review_logs \
    REVIEW_DB_PATH=/data/review_logs/review.db \
    REVIEW_HOST=0.0.0.0 \
    REVIEW_PORT=8080

EXPOSE 8080

# git の認証はアプリが実行時に環境変数経由で渡すため、起動時の認証設定は不要。
CMD ["python", "-m", "webreview"]
