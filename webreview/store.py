"""レビュージョブの状態管理と永続化。

スレッドセーフ。Web リクエストスレッドとワーカースレッドの両方から使われる。
ジョブは JSON ファイルに永続化し、サーバ再起動後も履歴と投稿状態を保持する。
再起動時に未完了 (queued/running) だったジョブは中断扱いにする。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import config

# ジョブのステータス
QUEUED = "queued"
RUNNING = "running"
SUCCEEDED = "succeeded"
FAILED = "failed"


@dataclass
class Job:
    id: str
    url: str
    owner: str
    repo: str
    number: int
    status: str = QUEUED
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    progress: list[str] = field(default_factory=list)
    error: str | None = None
    # レビュー成功時のメタ情報 (レビュー本文はファイルから都度読み出す)
    base_ref: str | None = None
    pr_title: str | None = None
    rev: int | None = None
    stdout_path: str | None = None
    stderr_path: str | None = None
    # PR への投稿状態
    posted: bool = False
    posting: bool = False
    post_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class JobStore:
    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.RLock()
        self._jobs: dict[str, Job] = {}
        self._load()

    # --- 永続化 -------------------------------------------------------------
    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for item in raw:
            try:
                job = Job(**item)
            except TypeError:
                continue
            # 前回実行で未完了だったジョブは中断扱い。
            if job.status in (QUEUED, RUNNING):
                job.status = FAILED
                job.error = "サーバの再起動により中断されました。"
                job.finished_at = job.finished_at or time.time()
            job.posting = False
            self._jobs[job.id] = job

    def _persist_locked(self) -> None:
        data = [j.to_dict() for j in self._jobs.values()]
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self._path)

    # --- 参照 ---------------------------------------------------------------
    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        """新しい順にジョブを返す。"""
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)

    def has_active(self) -> bool:
        """実行中または待機中のジョブがあるか。"""
        with self._lock:
            return any(j.status in (QUEUED, RUNNING) for j in self._jobs.values())

    # --- 更新 ---------------------------------------------------------------
    def create(self, url: str, owner: str, repo: str, number: int) -> Job:
        with self._lock:
            job = Job(
                id=uuid.uuid4().hex[:12],
                url=url,
                owner=owner,
                repo=repo,
                number=number,
            )
            self._jobs[job.id] = job
            self._persist_locked()
            return job

    def update(self, job_id: str, **fields: Any) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            for key, value in fields.items():
                setattr(job, key, value)
            self._persist_locked()
            return job

    def append_progress(self, job_id: str, line: str) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            job.progress.append(line)
            self._persist_locked()


# アプリ全体で共有する単一のストア。
store = JobStore(config.JOBS_FILE)
