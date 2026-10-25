"""レビュー実行を直列化するワーカー。

review モジュールは差し替え、ジョブの状態遷移と自動投稿の扱いだけを見る。
"""

from __future__ import annotations

import pytest

from webreview import review as review_module
from webreview import reviews, store
from webreview.errors import ReviewError
from webreview.worker import ReviewWorker

pytestmark = pytest.mark.usefixtures("temp_db")


@pytest.fixture
def worker():
    return ReviewWorker()


def _job(auto_post: bool = False, include_quality: bool = True) -> store.Job:
    return store.create(
        url="https://github.com/example-org/my-repo/pull/1",
        owner="example-org", repo="my-repo", number=1, auto_post=auto_post,
        include_quality=include_quality,
    )


def _stub_run_review(monkeypatch, body: str = "## 指摘", seen: dict | None = None):
    """run_review を差し替え、渡された進捗関数を通して 1 件保存する。"""
    def fake(pr, progress=None, include_quality=False):
        if seen is not None:
            seen["include_quality"] = include_quality
        if progress:
            progress("レビュー中...")
        return reviews.create(
            owner=pr.owner, repo=pr.repo, pr_number=pr.number, body=body,
            include_quality=include_quality,
        )

    monkeypatch.setattr(review_module, "run_review", fake)


# --- 実行 -------------------------------------------------------------------
def test_成功したジョブはレビューを参照する(worker, monkeypatch):
    _stub_run_review(monkeypatch)
    job = _job()

    worker._process(job.id)

    done = store.get(job.id)
    assert done.status == store.SUCCEEDED
    assert done.finished_at is not None
    assert done.started_at is not None
    assert reviews.get(done.review_id).body == "## 指摘"
    assert "レビュー中..." in done.progress


@pytest.mark.parametrize("include_quality", [True, False])
def test_品質改善を含めるかはジョブの設定どおりに渡す(
    worker, monkeypatch, include_quality
):
    seen: dict = {}
    _stub_run_review(monkeypatch, seen=seen)
    job = _job(include_quality=include_quality)

    worker._process(job.id)

    assert seen["include_quality"] is include_quality
    assert reviews.get(store.get(job.id).review_id).include_quality is include_quality


def test_想定内のエラーは画面に出す文言で失敗させる(worker, monkeypatch):
    def fail(pr, progress=None, include_quality=False):
        raise ReviewError("codex がインストールされていません。")

    monkeypatch.setattr(review_module, "run_review", fail)
    job = _job()

    worker._process(job.id)

    done = store.get(job.id)
    assert done.status == store.FAILED
    assert done.error == "codex がインストールされていません。"
    assert done.review_id is None


def test_想定外の例外でもワーカーは止まらない(worker, monkeypatch):
    def boom(pr, progress=None, include_quality=False):
        raise RuntimeError("bug")

    monkeypatch.setattr(review_module, "run_review", boom)
    job = _job()

    # _loop の最終防御と同じ経路を通す。
    worker._queue.put(job.id)
    worker._queue.put(job.id)
    worker.start()
    worker._queue.join()

    done = store.get(job.id)
    assert done.status == store.FAILED
    assert "想定外のエラー" in done.error
    assert "RuntimeError" in done.error


def test_消えたジョブは黙って飛ばす(worker, monkeypatch):
    called: list[int] = []
    monkeypatch.setattr(
        review_module, "run_review", lambda *a, **k: called.append(1)
    )

    worker._process("no-such-job")

    assert called == []


def test_キューに積んだジョブが処理される(worker, monkeypatch):
    _stub_run_review(monkeypatch)
    job = _job()

    worker.start()
    worker.enqueue(job.id)
    worker._queue.join()

    assert store.get(job.id).status == store.SUCCEEDED


def test_start_を重ねて呼んでもスレッドは一つ(worker):
    worker.start()
    worker.start()

    assert worker._thread.is_alive()


# --- 自動投稿 ---------------------------------------------------------------
def test_自動投稿が有効ならそのまま投稿する(worker, monkeypatch):
    _stub_run_review(monkeypatch)
    posted: list[int] = []
    monkeypatch.setattr(
        review_module, "post_review",
        lambda pr, review_id, progress=None: posted.append(review_id),
    )
    job = _job(auto_post=True)

    worker._process(job.id)

    done = store.get(job.id)
    assert posted == [done.review_id]
    assert done.posted is True
    assert done.posting is False
    assert done.post_error is None


def test_自動投稿が無効なら投稿しない(worker, monkeypatch):
    _stub_run_review(monkeypatch)
    posted: list[int] = []
    monkeypatch.setattr(
        review_module, "post_review",
        lambda *a, **k: posted.append(1),
    )
    job = _job(auto_post=False)

    worker._process(job.id)

    assert posted == []
    assert store.get(job.id).posted is False


def test_投稿に失敗してもレビューの成功は保つ(worker, monkeypatch):
    _stub_run_review(monkeypatch)

    def fail(pr, review_id, progress=None):
        raise ReviewError("投稿権限がありません。")

    monkeypatch.setattr(review_module, "post_review", fail)
    job = _job(auto_post=True)

    worker._process(job.id)

    done = store.get(job.id)
    assert done.status == store.SUCCEEDED
    assert done.posted is False
    assert done.posting is False
    assert done.post_error == "投稿権限がありません。"


def test_投稿中の想定外の例外も投稿中フラグを落とす(worker, monkeypatch):
    _stub_run_review(monkeypatch)

    def boom(pr, review_id, progress=None):
        raise RuntimeError("bug")

    monkeypatch.setattr(review_module, "post_review", boom)
    job = _job(auto_post=True)

    worker._process(job.id)

    done = store.get(job.id)
    assert done.posting is False
    assert "想定外のエラー" in done.post_error


def test_レビューを持たないジョブは投稿しない(worker, monkeypatch):
    posted: list[int] = []
    monkeypatch.setattr(
        review_module, "post_review", lambda *a, **k: posted.append(1)
    )
    job = _job()

    worker._post(job.id)

    assert posted == []


def test_投稿は別スレッドで走る(worker, monkeypatch):
    _stub_run_review(monkeypatch)
    monkeypatch.setattr(
        review_module, "post_review", lambda *a, **k: None
    )
    job = _job()
    worker._process(job.id)

    worker.post_async(job.id)

    # スレッドの完了を待ってから確認する。
    for _ in range(500):
        if store.get(job.id).posted:
            break
        __import__("time").sleep(0.01)
    assert store.get(job.id).posted is True


def test_再投稿は前回の失敗を消す(worker, monkeypatch):
    _stub_run_review(monkeypatch)
    monkeypatch.setattr(review_module, "post_review", lambda *a, **k: None)
    job = _job()
    worker._process(job.id)
    store.update(job.id, post_error="前回の失敗")

    worker._post(job.id)

    assert store.get(job.id).post_error is None
