"""list_review.sh のポート。レビューログを走査して集計データを返す。

LOGS_PATH 配下の `codex_review_pr<num>_out.log-<rev>` を更新日時順に集めて、
リポジトリ・PR 番号・タイトル・レビュー本文・詳細 (err ログ) を構造化して返す。
PR タイトルは API 呼び出しが重いのでファイルにキャッシュする。
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config, github_api, github_auth
from .errors import ReviewError

_OUT_RE = re.compile(r"^codex_review_pr(\d+)_out\.log-(\d+)$")
_TITLE_CACHE_LOCK = threading.Lock()


def _title_cache_path() -> Path:
    # ログと同じディレクトリに置く (Docker ではログ volume 内に永続化される)。
    return config.LOGS_PATH / "pr_titles.json"


def _load_title_cache() -> dict[str, str]:
    path = _title_cache_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_title_cache(cache: dict[str, str]) -> None:
    path = _title_cache_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _fetch_title(repo: str, pr_num: int, cache: dict[str, str]) -> str:
    key = f"{repo}/{pr_num}"
    if key in cache:
        return cache[key]
    try:
        title = github_api.get_pull(
            github_auth.get_provider(), config.REVIEW_ORG, repo, pr_num
        ).title
    except ReviewError:
        # 削除された PR や認証未設定でも集計自体は成立させる。
        title = ""

    if title:
        cache[key] = title
        return title
    return "[title fetch failed]"


def _details_from_err(err_path: Path) -> str:
    """err ログの先頭から 'exec' 行の直前までを返す (awk '/^exec$/ {exit}' 相当)。"""
    if not err_path.exists():
        return ""
    lines: list[str] = []
    for line in err_path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line == "exec":
            break
        lines.append(line)
    return "\n".join(lines)


def build_summary() -> list[dict[str, Any]]:
    """レビューログの集計を新しい順に返す。"""
    root = config.LOGS_PATH
    if not root.exists():
        return []

    out_files = [p for p in root.rglob("*") if p.is_file() and _OUT_RE.match(p.name)]
    # 更新日時の新しい順。
    out_files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    cache = _load_title_cache()
    cache_before = dict(cache)
    entries: list[dict[str, Any]] = []

    for out_file in out_files:
        m = _OUT_RE.match(out_file.name)
        if not m:
            continue
        pr_num = int(m.group(1))
        rev = int(m.group(2))
        repo = out_file.parent.name
        url = f"https://github.com/{config.REVIEW_ORG}/{repo}/pull/{pr_num}"
        mtime = datetime.fromtimestamp(out_file.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        title = _fetch_title(repo, pr_num, cache)
        err_file = out_file.with_name(out_file.name.replace("_out.log-", "_err.log-"))

        entries.append({
            "id": f"{repo}-{pr_num}-{rev}",
            "repo": repo,
            "pr_num": pr_num,
            "rev": rev,
            "url": url,
            "title": title,
            "mtime": mtime,
            "review_text": out_file.read_text(encoding="utf-8", errors="replace"),
            "details": _details_from_err(err_file),
        })

    if cache != cache_before:
        with _TITLE_CACHE_LOCK:
            _save_title_cache(cache)

    return entries
