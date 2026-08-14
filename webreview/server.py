"""Flask アプリ。REST API と静的フロントエンドを提供する。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request, send_from_directory

from . import config, db, github_auth, migrate, reviews, store
from .errors import ReviewError
from .review import parse_pr_url
from .store import SUCCEEDED
from .worker import ReviewWorker

STATIC_DIR = Path(__file__).resolve().parent / "static"

worker = ReviewWorker()


def _job_public(job: store.Job) -> dict[str, Any]:
    """API 用にジョブを辞書化する。

    progress は実行中のジョブにしか載らない (store.list_jobs を参照)。終了した
    ジョブの進捗は progress_count を見て詳細エンドポイントから取得する。
    """
    data = job.to_dict()
    data["has_result"] = job.review_id is not None
    return data


def _check_github_auth() -> None:
    """認証設定の不備を起動時のログで気付けるようにする。

    ここでは設定の読み込みだけを行い、ネットワークアクセスはしない。不備があっても
    起動自体は続け、ジョブ実行時に画面へエラーを出す (UI には到達できた方が良い)。
    """
    try:
        github_auth.get_provider()
    except ReviewError as e:
        print(f"[warn] GitHub 認証の設定に問題があります: {e}", flush=True)
    else:
        print(f"[info] GitHub 認証モード: {config.GITHUB_AUTH_MODE}", flush=True)


def _int_arg(name: str, default: int) -> int:
    try:
        return int(request.args.get(name, default))
    except (TypeError, ValueError):
        return default


def create_app() -> Flask:
    config.ensure_dirs()
    db.init()

    # 初回起動時だけ、ファイル保持だった頃のデータを取り込む。取り込み済みの
    # 環境では走らない。手動で確認したい場合は python -m webreview.migrate。
    if migrate.needed():
        report = migrate.run()
        # 新規インストールでは取り込むものが無いので、何かあったときだけ報告する。
        if report.reviews_imported or report.jobs_imported or report.titles_imported:
            print("[info] 既存のレビューログを取り込みました:", flush=True)
            for line in report.lines():
                print(f"[info] {line}", flush=True)

    interrupted = store.recover_interrupted()
    if interrupted:
        print(f"[info] 中断された {interrupted} 件のジョブを失敗にしました。", flush=True)

    pruned = reviews.prune_stderr()
    if pruned:
        print(f"[info] 期限切れの codex ログを {pruned} 件削除しました。", flush=True)

    _check_github_auth()
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
        return jsonify({
            "org": config.REVIEW_ORG,
            "model": config.REVIEW_MODEL,
            "reasoning_effort": config.REVIEW_REASONING_EFFORT,
            "description": config.REVIEW_DESCRIPTION,
        })

    # --- レビュー実行 ----------------------------------------------------
    @app.post("/api/reviews")
    def create_review():
        body = request.get_json(silent=True) or {}
        url = (body.get("url") or "").strip()
        try:
            pr = parse_pr_url(url)
        except ReviewError as e:
            return jsonify({"error": str(e)}), 400

        auto_post = bool(body.get("auto_post", True))
        was_busy = store.has_active()
        job = store.create(
            url=pr.url, owner=pr.owner, repo=pr.repo, number=pr.number,
            auto_post=auto_post,
        )
        worker.enqueue(job.id)
        return jsonify({"job": _job_public(job), "was_busy": was_busy}), 201

    @app.get("/api/reviews")
    def list_reviews():
        jobs = store.list_jobs()
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
        if job.review_id is not None:
            review = reviews.get(job.review_id)
            if review is not None:
                data["review_text"] = reviews.render_body(review)
        return jsonify(data)

    @app.delete("/api/reviews/<job_id>")
    def delete_review(job_id: str):
        job = store.get(job_id)
        if job is None:
            return jsonify({"error": "ジョブが見つかりません。"}), 404
        if not store.delete(job_id):
            return jsonify({"error": "実行中・待機中のジョブは削除できません。"}), 400
        return jsonify({"ok": True})

    @app.post("/api/reviews/<job_id>/post")
    def post_review_endpoint(job_id: str):
        job = store.get(job_id)
        if job is None:
            return jsonify({"error": "ジョブが見つかりません。"}), 404
        if job.status != SUCCEEDED or job.review_id is None:
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
        """集計一覧。本文は含めず、開かれた時点で個別に取得させる。"""
        limit = _int_arg("limit", reviews.DEFAULT_LIMIT)
        offset = max(0, _int_arg("offset", 0))
        repo = (request.args.get("repo") or "").strip() or None
        q = (request.args.get("q") or "").strip() or None

        # 未取得・失効ぶんのタイトルは裏で埋める (この応答は待たせない)。
        reviews.refresh_titles_async()

        return jsonify({
            "entries": reviews.list_summary(limit=limit, offset=offset, repo=repo, q=q),
            "total": reviews.count(repo=repo, q=q),
            "limit": limit,
            "offset": offset,
            "repos": reviews.repos(),
        })

    @app.get("/api/summary/<int:review_id>")
    def api_summary_detail(review_id: int):
        detail = reviews.get_detail(review_id)
        if detail is None:
            return jsonify({"error": "レビューが見つかりません。"}), 404
        return jsonify(detail)

    return app


def main() -> None:
    app = create_app()
    # 単一ワーカーで直列実行するため、開発サーバはスレッド有効・リローダ無効で起動。
    app.run(host=config.HOST, port=config.PORT, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
