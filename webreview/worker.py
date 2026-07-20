"""レビュー実行を直列化する単一ワーカー。

キューに積まれたジョブを 1 件ずつバックグラウンドで処理する。これにより
同時実行非対応のレビュースクリプトを安全に運用でき、計算リソースの使いすぎも防ぐ。
投稿 (post) はレビュー本体とは独立にすぐ行いたいので別スレッドで処理する。
"""

from __future__ import annotations

import queue
import threading
import time
import traceback

from . import review
from .review import ParsedPR, ReviewError
from .store import RUNNING, SUCCEEDED, FAILED, JobStore


class ReviewWorker:
    def __init__(self, store: JobStore):
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
            result = review.run_review(pr, progress=progress)
        except ReviewError as e:
            self._store.update(
                job_id, status=FAILED, error=str(e), finished_at=time.time()
            )
            return

        self._store.update(
            job_id,
            status=SUCCEEDED,
            finished_at=time.time(),
            base_ref=result.base_ref,
            pr_title=result.pr_title,
            rev=result.rev,
            stdout_path=str(result.stdout_path),
            stderr_path=str(result.stderr_path),
        )

    # --- 投稿 (非同期) -------------------------------------------------------
    def post_async(self, job_id: str) -> None:
        threading.Thread(target=self._post, args=(job_id,), daemon=True).start()

    def _post(self, job_id: str) -> None:
        job = self._store.get(job_id)
        if job is None or not job.stdout_path:
            return

        def progress(line: str) -> None:
            self._store.append_progress(job_id, line)

        pr = ParsedPR(owner=job.owner, repo=job.repo, number=job.number)
        try:
            review.post_review(pr, job.stdout_path, progress=progress)
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
