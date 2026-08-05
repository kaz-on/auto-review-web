FROM python:3.12-slim

# --- OS パッケージ + Codex CLI ----------------------------------------------
# GitHub へのアクセスは REST API (標準ライブラリ) と git で行うため gh は入れない。
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl wget gnupg git \
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

# Codex CLI は bubblewrap を使ってサンドボックス化するため、bubblewrap をインストールする。
RUN apt-get update && apt-get install -y bubblewrap && \
    rm -rf /var/lib/apt/lists/*

# --- アプリケーション -------------------------------------------------------
WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY webreview ./webreview

# データ (クローンしたリポジトリ・ログ・ジョブ履歴) は /data 以下。volume で永続化。
ENV REVIEW_WORK_PATH=/data/work \
    REVIEW_LOGS_PATH=/data/review_logs \
    REVIEW_JOBS_FILE=/data/review_logs/jobs.json \
    REVIEW_HOST=0.0.0.0 \
    REVIEW_PORT=8080

EXPOSE 8080

# git の認証はアプリが実行時に環境変数経由で渡すため、起動時の認証設定は不要。
CMD ["python", "-m", "webreview"]
