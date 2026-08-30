"""ジョブの状態管理。

進捗の載せ方 (一覧は実行中のみ・詳細は都度取得) と、更新できるカラムを固定集合で
縛っている点が要。列名を SQL に直接埋めるため、ここが緩むと injection になる。
"""

from __future__ import annotations

import threading

import pytest

from webreview import db, store

pytestmark = pytest.mark.usefixtures("temp_db")


def _create(number: int = 1, **kwargs) -> store.Job:
    return store.create(
        url=f"https://github.com/example-org/my-repo/pull/{number}",
        owner="example-org",
        repo="my-repo",
        number=number,
        **kwargs,
    )


def test_作ったジョブは待機中で返る():
    job = _create()

    assert job.status == store.QUEUED
    assert job.auto_post is True
    assert job.posted is False and job.posting is False
    assert job.review_id is None
    assert job.progress == []
    assert len(job.id) == 12


def test_自動投稿を切って作れる():
    assert _create(auto_post=False).auto_post is False


def test_存在しないジョブは_none():
    assert store.get("no-such-job") is None


def test_一覧は新しい順に返る():
    ids = [_create(number=n).id for n in (1, 2, 3)]

    assert [j.id for j in store.list_jobs()] == list(reversed(ids))


def test_一覧の進捗は実行中のジョブにだけ載る():
    running = _create(number=1)
    done = _create(number=2)
    store.append_progress(running.id, "実行中の行")
    store.append_progress(done.id, "終わった行")
    store.update(done.id, status=store.SUCCEEDED)

    by_id = {j.id: j for j in store.list_jobs()}

    assert by_id[running.id].progress == ["実行中の行"]
    # 終了済みは件数だけを返し、全文は詳細取得に回す。
    assert by_id[done.id].progress is None
    assert by_id[done.id].progress_count == 1


def test_詳細では終了済みのジョブの進捗も取れる():
    job = _create()
    store.append_progress(job.id, "1 行目")
    store.append_progress(job.id, "2 行目")
    store.update(job.id, status=store.SUCCEEDED)

    assert store.get(job.id).progress == ["1 行目", "2 行目"]
    assert store.get(job.id, with_progress=False).progress is None


def test_進捗は追加順に並ぶ():
    job = _create()
    for i in range(30):
        store.append_progress(job.id, f"行 {i}")

    assert store.get(job.id).progress == [f"行 {i}" for i in range(30)]


def test_消えたジョブへの進捗追加は無視する():
    job = _create()
    store.update(job.id, status=store.SUCCEEDED)
    store.delete(job.id)

    # 実行中にジョブが消されても、進捗の取りこぼしで実行を止めない。
    store.append_progress(job.id, "行")


def test_実行中かどうかを判定する():
    assert store.has_active() is False
    job = _create()
    assert store.has_active() is True

    store.update(job.id, status=store.RUNNING)
    assert store.has_active() is True

    store.update(job.id, status=store.FAILED)
    assert store.has_active() is False


# --- 更新 -------------------------------------------------------------------
def test_更新した値が返る():
    job = _create()

    updated = store.update(job.id, status=store.RUNNING, started_at=123.0)

    assert updated.status == store.RUNNING
    assert updated.started_at == 123.0


def test_真偽値は_0_1_として保存され真偽値で返る():
    job = _create()

    updated = store.update(job.id, posted=True, posting=False)

    assert updated.posted is True
    assert updated.posting is False


def test_更新を許さないカラムは弾く():
    job = _create()

    with pytest.raises(ValueError, match="更新できないカラム"):
        store.update(job.id, repo="別のリポジトリ")


def test_列名に見せかけた_sql_も弾く():
    job = _create()

    with pytest.raises(ValueError):
        store.update(job.id, **{"status = 'x' --": "y"})


def test_更新するものが無ければ現在の値を返す():
    job = _create()
    assert store.update(job.id).id == job.id


def test_存在しないジョブの更新は_none():
    assert store.update("no-such-job", status=store.FAILED) is None


# --- 削除 -------------------------------------------------------------------
@pytest.mark.parametrize("status", [store.SUCCEEDED, store.FAILED])
def test_終了済みのジョブは削除できる(status):
    job = _create()
    store.update(job.id, status=status)

    assert store.delete(job.id) is True
    assert store.get(job.id) is None


@pytest.mark.parametrize("status", [store.QUEUED, store.RUNNING])
def test_実行中待機中のジョブは削除できない(status):
    job = _create()
    store.update(job.id, status=status)

    assert store.delete(job.id) is False
    assert store.get(job.id) is not None


def test_ジョブを消しても進捗だけが残らない():
    job = _create()
    store.append_progress(job.id, "行")
    store.update(job.id, status=store.SUCCEEDED)

    store.delete(job.id)

    rows = db.connect().execute("SELECT COUNT(*) FROM job_progress").fetchone()[0]
    assert rows == 0


def test_存在しないジョブの削除は_false():
    assert store.delete("no-such-job") is False


# --- 起動時の後始末 ---------------------------------------------------------
def test_未完了のジョブは再起動時に失敗にする():
    queued = _create(number=1)
    running = _create(number=2)
    store.update(running.id, status=store.RUNNING)
    done = _create(number=3)
    store.update(done.id, status=store.SUCCEEDED)

    assert store.recover_interrupted() == 2

    assert store.get(queued.id).status == store.FAILED
    assert "再起動" in store.get(queued.id).error
    assert store.get(running.id).status == store.FAILED
    assert store.get(running.id).finished_at is not None
    # 既に終わっていたジョブには触らない。
    assert store.get(done.id).status == store.SUCCEEDED


def test_投稿中フラグは再起動で必ず落ちる():
    job = _create()
    store.update(job.id, status=store.SUCCEEDED, posting=True)

    store.recover_interrupted()

    assert store.get(job.id).posting is False


def test_終了時刻が既にあれば上書きしない():
    job = _create()
    store.update(job.id, status=store.RUNNING, finished_at=42.0)

    store.recover_interrupted()

    assert store.get(job.id).finished_at == 42.0


# --- 並行アクセス -----------------------------------------------------------
def test_別スレッドからの書き込みも同じ_db_に届く():
    """Flask のリクエストスレッドとワーカースレッドから同時に触られる前提の確認。"""
    job = _create()
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            for i in range(20):
                store.append_progress(job.id, f"別スレッド {i}")
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    assert len(store.get(job.id).progress) == 60
