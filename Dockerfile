FROM python:3.12-slim

# --- OS パッケージ + GitHub CLI + Codex CLI --------------------------------
# GitHub CLI は公式手順で apt リポジトリを追加してインストールする
# (コンテナ内は root のため sudo は使わない)。
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl wget gnupg git \
    && mkdir -p -m 755 /etc/apt/keyrings \
    && out=$(mktemp) \
    && wget -nv -O"$out" https://cli.github.com/packages/githubcli-archive-keyring.gpg \
    && cat "$out" | tee /etc/apt/keyrings/githubcli-archive-keyring.gpg > /dev/null \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && mkdir -p -m 755 /etc/apt/sources.list.d \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        | tee /etc/apt/sources.list.d/github-cli.list > /dev/null \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
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
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# データ (クローンしたリポジトリ・ログ・ジョブ履歴) は /data 以下。volume で永続化。
ENV REVIEW_WORK_PATH=/data/work \
    REVIEW_LOGS_PATH=/data/review_logs \
    REVIEW_JOBS_FILE=/data/review_logs/jobs.json \
    REVIEW_HOST=0.0.0.0 \
    REVIEW_PORT=8080

EXPOSE 8080

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python", "-m", "webreview"]
