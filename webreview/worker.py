"""レビュー実行を直列化する単一ワーカー。

キューに積まれたジョブを 1 件ずつバックグラウンドで処理する。共有の作業
ディレクトリに対する git 操作が競合せず、計算リソースの使いすぎも防げる。
投稿 (post) はレビュー本体とは独立にすぐ行いたいので別スレッドで処理する。
"""

from __future__ import annotations

import queue
import threading
import time
import traceback

from . import review, store
from .review import ParsedPR, ReviewError
from .store import RUNNING, SUCCEEDED, FAILED


class ReviewWorker:
    def __init__(self) -> None:
        self._store = store
        self._queue: queue.Queue[str] = queue.Queue()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._started = False

    def start(self) -> None:
        if not self._started:
            self._started = True
            self._thread.start()

    def enqueue(self, job_id: str) -> None:
        self._queue.put(job_id)

    # --- レビュー実行 (直列) --------------------------------------------------
    def _loop(self) -> None:
        while True:
            job_id = self._queue.get()
            try:
                self._process(job_id)
            except Exception:  # ワーカーが落ちないよう最終防御
                self._store.update(
                    job_id,
                    status=FAILED,
                    error="想定外のエラーが発生しました:\n" + traceback.format_exc(),
                    finished_at=time.time(),
                )
            finally:
                self._queue.task_done()

    def _process(self, job_id: str) -> None:
        job = self._store.get(job_id)
        if job is None:
            return

        self._store.update(job_id, status=RUNNING, started_at=time.time())

        def progress(line: str) -> None:
            self._store.append_progress(job_id, line)

        pr = ParsedPR(owner=job.owner, repo=job.repo, number=job.number)
        try:
            result = review.run_review(
                pr, progress=progress, include_quality=job.include_quality
            )
        except ReviewError as e:
            self._store.update(
                job_id, status=FAILED, error=str(e), finished_at=time.time()
            )
            return

        # レビュー本文・メタ情報は reviews 側に入っているので、ジョブは参照だけ持つ。
        self._store.update(
            job_id,
            status=SUCCEEDED,
            finished_at=time.time(),
            review_id=result.id,
        )

        # 自動投稿が有効ならそのまま PR に投稿する (ワーカースレッド内で直列に実行)。
        if job.auto_post:
            self._store.update(job_id, posting=True)
            progress("自動投稿が有効です。")
            self._post(job_id)

    # --- 投稿 (非同期) -------------------------------------------------------
    def post_async(self, job_id: str) -> None:
        threading.Thread(target=self._post, args=(job_id,), daemon=True).start()

    def _post(self, job_id: str) -> None:
        job = self._store.get(job_id)
        if job is None or job.review_id is None:
            return

        def progress(line: str) -> None:
            self._store.append_progress(job_id, line)

        pr = ParsedPR(owner=job.owner, repo=job.repo, number=job.number)
        try:
            review.post_review(pr, job.review_id, progress=progress)
        except ReviewError as e:
            self._store.update(job_id, posting=False, post_error=str(e))
            return
        except Exception:
            self._store.update(
                job_id,
                posting=False,
                post_error="想定外のエラーが発生しました:\n" + traceback.format_exc(),
            )
            return

        self._store.update(job_id, posting=False, posted=True, post_error=None)
