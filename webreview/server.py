"""Flask アプリ。REST API と静的フロントエンドを提供する。"""

from __future__ import annotations

import signal
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request, send_from_directory

from . import config, db, export, github_auth, reviews, store
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


def _check_review_owner() -> None:
    """REVIEW_OWNER の設定漏れを起動時のログで気付けるようにする。

    _check_github_auth と同じ方針で、未設定でも起動は続ける。実際のレビュー投入時
    に parse_pr_url が画面へエラーを出す。
    """
    if config.REVIEW_OWNER:
        print(f"[info] レビュー対象のオーナー: {config.REVIEW_OWNER}", flush=True)
    else:
        print("[warn] REVIEW_OWNER が未設定です。レビューは実行できません。", flush=True)


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


def _summary_filter() -> tuple[str | None, str | None]:
    """集計タブの絞り込み条件 (repo / q)。一覧とエクスポートで共通。"""
    repo = (request.args.get("repo") or "").strip() or None
    q = (request.args.get("q") or "").strip() or None
    return repo, q


def _csv_response(name: str, csv_lines: Iterable[str]) -> Response:
    """CSV をダウンロードさせる。件数に上限が無いので、組み立てながら送る。

    ファイル名の日時はサーバのタイムゾーン設定に左右されないよう UTC で付け、
    末尾の Z でそれと分かるようにする (Docker のコンテナは既定で UTC)。
    """
    filename = f"{name}-{time.strftime('%Y%m%d-%H%M%SZ', time.gmtime())}.csv"
    return Response(
        csv_lines,
        content_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def create_app() -> Flask:
    config.ensure_dirs()
    db.init()

    interrupted = store.recover_interrupted()
    if interrupted:
        print(f"[info] 中断された {interrupted} 件のジョブを失敗にしました。", flush=True)

    pruned = reviews.prune_stderr()
    if pruned:
        print(f"[info] 期限切れの codex ログを {pruned} 件削除しました。", flush=True)

    pruned = store.prune()
    if pruned:
        print(f"[info] 期限切れのジョブを {pruned} 件履歴から削除しました。", flush=True)

    _check_review_owner()
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
            "owner": config.REVIEW_OWNER,
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
        include_quality = bool(body.get("include_quality", True))
        skip_if_reviewed = bool(body.get("skip_if_reviewed", False))
        was_busy = store.has_active()
        create = store.create_unless_reviewed if skip_if_reviewed else store.create
        job = create(
            url=pr.url, owner=pr.owner, repo=pr.repo, number=pr.number,
            auto_post=auto_post, include_quality=include_quality,
        )
        if isinstance(job, store.Duplicate):
            return jsonify({
                "skipped": True,
                "job": _job_public(job.job) if job.job is not None else None,
                "review_id": job.review_id,
            })
        worker.enqueue(job.id)
        # 常駐していても履歴が溜まらないよう、ジョブが増えるたびに古いものを片付ける。
        store.prune()
        return jsonify({
            "skipped": False, "job": _job_public(job), "was_busy": was_busy,
        }), 201

    @app.get("/api/reviews")
    def list_reviews():
        limit = max(1, min(_int_arg("limit", store.DEFAULT_LIST_LIMIT),
                           store.MAX_LIST_LIMIT))
        jobs = store.list_jobs(limit=limit)
        return jsonify({
            "jobs": [_job_public(j) for j in jobs],
            "total": store.count(),
            "limit": limit,
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
        repo, q = _summary_filter()

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

    # --- エクスポート ----------------------------------------------------
    # 集計はアプリの外で行う前提で、レビュー単位と指摘単位の CSV を出す。
    # 絞り込み条件は集計タブと同じ。
    @app.get("/api/export/reviews.csv")
    def api_export_reviews():
        repo, q = _summary_filter()
        return _csv_response(
            "reviews", export.to_csv(export.REVIEW_COLUMNS, export.review_rows(repo, q))
        )

    @app.get("/api/export/findings.csv")
    def api_export_findings():
        repo, q = _summary_filter()
        return _csv_response(
            "findings", export.to_csv(export.FINDING_COLUMNS, export.finding_rows(repo, q))
        )

    return app


def main() -> None:
    # SIGTERM (docker stop) で正常終了させる。ハンドラが無いと PID 1 では無視され、
    # init (tini) 経由でもシグナル終了扱いの 143 になる。
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    app = create_app()
    # 単一ワーカーで直列実行するため、開発サーバはスレッド有効・リローダ無効で起動。
    app.run(host=config.HOST, port=config.PORT, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
