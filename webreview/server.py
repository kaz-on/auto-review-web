"""Flask アプリ。REST API と静的フロントエンドを提供する。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_from_directory

from . import config
from .review import ReviewError, parse_pr_url
from .store import SUCCEEDED, store
from .worker import ReviewWorker

STATIC_DIR = Path(__file__).resolve().parent / "static"

worker = ReviewWorker(store)


def _job_public(job) -> dict[str, Any]:
    """API 用にジョブを辞書化する (レビュー本文は含めない)。"""
    data = job.to_dict()
    data.pop("stdout_path", None)
    data.pop("stderr_path", None)
    data["has_result"] = bool(job.stdout_path)
    return data


def create_app() -> Flask:
    config.ensure_dirs()
    worker.start()

    app = Flask(__name__, static_folder=None)

    # --- 静的ファイル ----------------------------------------------------
    @app.get("/")
    def index():
        return send_from_directory(STATIC_DIR, "index.html")

    @app.get("/static/<path:filename>")
    def static_files(filename: str):
        return send_from_directory(STATIC_DIR, filename)

    # --- 設定 ------------------------------------------------------------
    @app.get("/api/config")
    def api_config():
        return jsonify({"org": config.REVIEW_ORG, "model": config.REVIEW_MODEL})

    # --- レビュー実行 ----------------------------------------------------
    @app.post("/api/reviews")
    def create_review():
        body = request.get_json(silent=True) or {}
        url = (body.get("url") or "").strip()
        try:
            pr = parse_pr_url(url)
        except ReviewError as e:
            return jsonify({"error": str(e)}), 400

        was_busy = store.has_active()
        job = store.create(url=pr.url, owner=pr.owner, repo=pr.repo, number=pr.number)
        worker.enqueue(job.id)
        return jsonify({"job": _job_public(job), "was_busy": was_busy}), 201

    @app.get("/api/reviews")
    def list_reviews():
        jobs = store.list()
        return jsonify({
            "jobs": [_job_public(j) for j in jobs],
            "busy": store.has_active(),
        })

    @app.get("/api/reviews/<job_id>")
    def get_review(job_id: str):
        job = store.get(job_id)
        if job is None:
            return jsonify({"error": "ジョブが見つかりません。"}), 404
        data = _job_public(job)
        if job.stdout_path and Path(job.stdout_path).exists():
            data["review_text"] = Path(job.stdout_path).read_text(
                encoding="utf-8", errors="replace"
            )
        return jsonify(data)

    @app.post("/api/reviews/<job_id>/post")
    def post_review_endpoint(job_id: str):
        job = store.get(job_id)
        if job is None:
            return jsonify({"error": "ジョブが見つかりません。"}), 404
        if job.status != SUCCEEDED or not job.stdout_path:
            return jsonify({"error": "完了済みのレビューのみ投稿できます。"}), 400
        if job.posted:
            return jsonify({"error": "このレビューは既に投稿済みです。"}), 400
        if job.posting:
            return jsonify({"error": "投稿処理中です。"}), 409

        store.update(job_id, posting=True, post_error=None)
        worker.post_async(job_id)
        return jsonify({"job": _job_public(store.get(job_id))})

    # --- 集計 ------------------------------------------------------------
    @app.get("/api/summary")
    def api_summary():
        # 遅延 import (gh 呼び出しを伴うため必要時のみ)
        from . import aggregate
        return jsonify({"entries": aggregate.build_summary()})

    return app


def main() -> None:
    app = create_app()
    # 単一ワーカーで直列実行するため、開発サーバはスレッド有効・リローダ無効で起動。
    app.run(host=config.HOST, port=config.PORT, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
