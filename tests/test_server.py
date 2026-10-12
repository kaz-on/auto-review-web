"""Flask の REST API。

ワーカーは差し替えて、実際に git や codex を起動しないようにする。
"""

from __future__ import annotations

import pytest

from webreview import config, db, reviews, server, store


class FakeWorker:
    """ReviewWorker の代役。何が積まれたかだけを覚える。"""

    def __init__(self) -> None:
        self.started = False
        self.enqueued: list[str] = []
        self.posted: list[str] = []

    def start(self) -> None:
        self.started = True

    def enqueue(self, job_id: str) -> None:
        self.enqueued.append(job_id)

    def post_async(self, job_id: str) -> None:
        self.posted.append(job_id)


@pytest.fixture
def worker(monkeypatch):
    fake = FakeWorker()
    monkeypatch.setattr(server, "worker", fake)
    return fake


@pytest.fixture
def client(temp_db, temp_paths, review_owner, worker, monkeypatch):
    # 集計を開くたびに GitHub を叩きに行かないようにする。
    monkeypatch.setattr(reviews, "refresh_titles_async", lambda *a, **k: None)
    app = server.create_app()
    app.config.update(TESTING=True)
    return app.test_client()


def _post_review(client, number: int = 1, **body):
    return client.post("/api/reviews", json={
        "url": f"https://github.com/example-org/my-repo/pull/{number}", **body,
    })


# --- 起動時 -----------------------------------------------------------------
def test_起動時にスキーマを作りワーカーを立ち上げる(client, worker):
    assert worker.started is True
    tables = {
        row[0] for row in db.connect().execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {"reviews", "jobs", "job_progress", "pr_titles"} <= tables


def test_起動時に未完了のジョブを失敗にする(
    temp_db, temp_paths, review_owner, worker, monkeypatch
):
    job = store.create(
        url="u", owner="example-org", repo="my-repo", number=1,
    )
    monkeypatch.setattr(reviews, "refresh_titles_async", lambda *a, **k: None)

    server.create_app()

    assert store.get(job.id).status == store.FAILED


# --- 静的ファイル -----------------------------------------------------------
def test_ルートで画面を返す(client):
    resp = client.get("/")

    assert resp.status_code == 200
    assert b"<html" in resp.data.lower()


def test_静的ファイルを返す(client):
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/style.css").status_code == 200


# --- 設定 -------------------------------------------------------------------
def test_画面に必要な設定を返す(client, monkeypatch):
    monkeypatch.setattr(config, "REVIEW_MODEL", "gpt-6.1-sol")
    monkeypatch.setattr(config, "REVIEW_DESCRIPTION", "説明文")

    data = client.get("/api/config").get_json()

    assert data["owner"] == "example-org"
    assert data["model"] == "gpt-6.1-sol"
    assert data["description"] == "説明文"
    # トークンや秘密鍵は返さない。
    assert set(data) == {"owner", "model", "reasoning_effort", "description"}


# --- レビューの投入 ---------------------------------------------------------
def test_レビューを投入するとキューに積まれる(client, worker):
    resp = _post_review(client, number=42)

    assert resp.status_code == 201
    job = resp.get_json()["job"]
    assert job["status"] == store.QUEUED
    assert job["number"] == 42
    assert job["auto_post"] is True
    # 品質改善は既定で含める (積極的に使ってもらうため)。
    assert job["include_quality"] is True
    assert job["has_result"] is False
    assert worker.enqueued == [job["id"]]


def test_自動投稿を切って投入できる(client):
    job = _post_review(client, auto_post=False).get_json()["job"]

    assert job["auto_post"] is False


def test_品質改善を含めずに投入できる(client):
    job = _post_review(client, include_quality=False).get_json()["job"]

    assert job["include_quality"] is False


def test_先行するジョブがあることを知らせる(client):
    assert _post_review(client, number=1).get_json()["was_busy"] is False
    # 1 件目が待機中なので 2 件目は待たされる。
    assert _post_review(client, number=2).get_json()["was_busy"] is True


def test_既定ではレビュー済みの_pr_も再レビューする(client, worker):
    reviews.create(repo="my-repo", pr_number=1, body="## 指摘")

    resp = _post_review(client)

    assert resp.status_code == 201
    assert resp.get_json()["skipped"] is False
    assert len(worker.enqueued) == 1


def test_レビュー済みの_pr_は指定があれば投入しない(client, worker):
    review = reviews.create(repo="my-repo", pr_number=1, body="## 指摘")

    resp = _post_review(client, skip_if_reviewed=True)

    assert resp.status_code == 200
    assert resp.get_json() == {"skipped": True, "job": None, "review_id": review.id}
    assert worker.enqueued == []


def test_同じ_pr_の実行中のジョブを返して投入しない(client, worker):
    first = _post_review(client, skip_if_reviewed=True).get_json()
    assert first["skipped"] is False

    data = _post_review(client, skip_if_reviewed=True).get_json()

    assert data["skipped"] is True
    assert data["job"]["id"] == first["job"]["id"]
    assert data["review_id"] is None
    assert worker.enqueued == [first["job"]["id"]]


@pytest.mark.parametrize("body", [{}, {"url": ""}, {"url": "https://example.com/x"}])
def test_url_が不正なら投入しない(client, worker, body):
    resp = client.post("/api/reviews", json=body)

    assert resp.status_code == 400
    assert "error" in resp.get_json()
    assert worker.enqueued == []


def test_対象外のオーナーは投入しない(client, worker):
    resp = client.post(
        "/api/reviews", json={"url": "https://github.com/other-org/r/pull/1"}
    )

    assert resp.status_code == 400
    assert worker.enqueued == []


def test_json_でない本文でも落ちない(client):
    resp = client.post("/api/reviews", data="not json",
                       content_type="application/json")

    assert resp.status_code == 400


def test_投入のたびに古いジョブを片付ける(client):
    old = _post_review(client, number=1).get_json()["job"]["id"]
    store.update(old, status=store.SUCCEEDED, posted=True, finished_at=1.0)

    _post_review(client, number=2)

    assert store.get(old) is None


def test_起動時に古いジョブを片付ける(
    temp_db, temp_paths, review_owner, worker, monkeypatch
):
    job = store.create(url="u", owner="example-org", repo="my-repo", number=1)
    store.update(job.id, status=store.SUCCEEDED, posted=True, finished_at=1.0)
    monkeypatch.setattr(reviews, "refresh_titles_async", lambda *a, **k: None)

    server.create_app()

    assert store.get(job.id) is None


# --- ジョブの参照 -----------------------------------------------------------
def test_一覧は新しい順で実行中かどうかを添える(client):
    first = _post_review(client, number=1).get_json()["job"]["id"]
    second = _post_review(client, number=2).get_json()["job"]["id"]

    data = client.get("/api/reviews").get_json()

    assert [j["id"] for j in data["jobs"]] == [second, first]
    assert data["busy"] is True


def test_一覧は件数を絞って総数を添える(client):
    ids = [_post_review(client, number=n).get_json()["job"]["id"] for n in (1, 2, 3)]

    data = client.get("/api/reviews?limit=2").get_json()

    assert [j["id"] for j in data["jobs"]] == [ids[2], ids[1]]
    assert data["total"] == 3
    assert data["limit"] == 2


def test_一覧の件数は上限で切り詰めて知らせる(client):
    data = client.get(f"/api/reviews?limit={store.MAX_LIST_LIMIT + 1}").get_json()

    assert data["limit"] == store.MAX_LIST_LIMIT


@pytest.mark.parametrize("query", ["limit=abc", "limit=", "limit=0"])
def test_不正な件数指定でも落ちない(client, query):
    _post_review(client)

    data = client.get(f"/api/reviews?{query}").get_json()

    assert len(data["jobs"]) == 1


def test_全て終わっていれば実行中ではない(client):
    job_id = _post_review(client).get_json()["job"]["id"]
    store.update(job_id, status=store.SUCCEEDED)

    assert client.get("/api/reviews").get_json()["busy"] is False


def test_詳細ではレビュー本文を返す(client):
    job_id = _post_review(client).get_json()["job"]["id"]
    review = reviews.create(repo="my-repo", pr_number=1, body="## 指摘", model="m")
    store.update(job_id, status=store.SUCCEEDED, review_id=review.id)

    data = client.get(f"/api/reviews/{job_id}").get_json()

    assert data["has_result"] is True
    assert data["review_text"] == "codex review (m, rev-0):\n\n## 指摘"


def test_レビュー未生成なら本文は付かない(client):
    job_id = _post_review(client).get_json()["job"]["id"]

    data = client.get(f"/api/reviews/{job_id}").get_json()

    assert "review_text" not in data
    assert data["has_result"] is False


def test_レビューを消してもジョブ履歴は残る(client):
    job_id = _post_review(client).get_json()["job"]["id"]
    review = reviews.create(repo="my-repo", pr_number=1, body="## 指摘")
    store.update(job_id, status=store.SUCCEEDED, review_id=review.id)

    with db.write() as conn:
        conn.execute("DELETE FROM reviews WHERE id = ?", (review.id,))

    data = client.get(f"/api/reviews/{job_id}").get_json()

    assert data["status"] == store.SUCCEEDED
    # 参照は外れる (ON DELETE SET NULL)。本文は返しようがない。
    assert data["review_id"] is None
    assert data["has_result"] is False
    assert "review_text" not in data


def test_存在しないジョブは_404(client):
    assert client.get("/api/reviews/no-such-job").status_code == 404


# --- ジョブの削除 -----------------------------------------------------------
def test_終了したジョブを履歴から消せる(client):
    job_id = _post_review(client).get_json()["job"]["id"]
    store.update(job_id, status=store.SUCCEEDED)

    assert client.delete(f"/api/reviews/{job_id}").get_json() == {"ok": True}
    assert client.get(f"/api/reviews/{job_id}").status_code == 404


def test_実行中のジョブは消せない(client):
    job_id = _post_review(client).get_json()["job"]["id"]

    resp = client.delete(f"/api/reviews/{job_id}")

    assert resp.status_code == 400
    assert store.get(job_id) is not None


def test_存在しないジョブの削除は_404(client):
    assert client.delete("/api/reviews/no-such-job").status_code == 404


def test_ジョブを消してもレビュー本体は残る(client):
    job_id = _post_review(client).get_json()["job"]["id"]
    review = reviews.create(repo="my-repo", pr_number=1, body="## 指摘")
    store.update(job_id, status=store.SUCCEEDED, review_id=review.id)

    client.delete(f"/api/reviews/{job_id}")

    assert reviews.get(review.id) is not None


# --- 投稿 -------------------------------------------------------------------
def _succeeded_job(client) -> str:
    job_id = _post_review(client).get_json()["job"]["id"]
    review = reviews.create(repo="my-repo", pr_number=1, body="## 指摘")
    store.update(job_id, status=store.SUCCEEDED, review_id=review.id)
    return job_id


def test_完了したレビューを投稿できる(client, worker):
    job_id = _succeeded_job(client)

    resp = client.post(f"/api/reviews/{job_id}/post")

    assert resp.status_code == 200
    assert resp.get_json()["job"]["posting"] is True
    assert worker.posted == [job_id]


def test_投稿時に前回の失敗を消す(client):
    job_id = _succeeded_job(client)
    store.update(job_id, post_error="前回の失敗")

    resp = client.post(f"/api/reviews/{job_id}/post")

    assert resp.get_json()["job"]["post_error"] is None


@pytest.mark.parametrize("status", [store.QUEUED, store.RUNNING, store.FAILED])
def test_完了していないジョブは投稿できない(client, worker, status):
    job_id = _post_review(client).get_json()["job"]["id"]
    store.update(job_id, status=status)

    resp = client.post(f"/api/reviews/{job_id}/post")

    assert resp.status_code == 400
    assert worker.posted == []


def test_レビューを持たないジョブは投稿できない(client, worker):
    job_id = _post_review(client).get_json()["job"]["id"]
    store.update(job_id, status=store.SUCCEEDED)

    assert client.post(f"/api/reviews/{job_id}/post").status_code == 400
    assert worker.posted == []


def test_投稿済みのレビューは二重に投稿しない(client, worker):
    job_id = _succeeded_job(client)
    store.update(job_id, posted=True)

    assert client.post(f"/api/reviews/{job_id}/post").status_code == 400
    assert worker.posted == []


def test_投稿中なら_409(client, worker):
    job_id = _succeeded_job(client)
    store.update(job_id, posting=True)

    assert client.post(f"/api/reviews/{job_id}/post").status_code == 409
    assert worker.posted == []


def test_存在しないジョブの投稿は_404(client):
    assert client.post("/api/reviews/no-such-job/post").status_code == 404


# --- 集計 -------------------------------------------------------------------
def test_集計一覧を返す(client):
    reviews.create(repo="my-repo", pr_number=1, body="本文", created_at=100.0)
    reviews.create(repo="other-repo", pr_number=2, body="本文", created_at=200.0)

    data = client.get("/api/summary").get_json()

    assert data["total"] == 2
    assert [e["pr_num"] for e in data["entries"]] == [2, 1]
    assert data["repos"] == ["my-repo", "other-repo"]
    assert data["limit"] == reviews.DEFAULT_LIMIT
    assert data["offset"] == 0


def test_集計を絞り込める(client):
    reviews.create(repo="my-repo", pr_number=1, body="injection の懸念")
    reviews.create(repo="other-repo", pr_number=2, body="命名の指摘")

    by_repo = client.get("/api/summary?repo=my-repo").get_json()
    assert by_repo["total"] == 1

    by_q = client.get("/api/summary?q=injection").get_json()
    assert by_q["total"] == 1
    # 絞り込んでも選択肢のリポジトリ一覧は減らさない。
    assert by_q["repos"] == ["my-repo", "other-repo"]


def test_集計をページングできる(client):
    for n in range(5):
        reviews.create(repo="my-repo", pr_number=n, body="本文",
                       created_at=float(n) + 1)

    data = client.get("/api/summary?limit=2&offset=1").get_json()

    assert [e["pr_num"] for e in data["entries"]] == [3, 2]
    assert data["total"] == 5


@pytest.mark.parametrize("query", ["limit=abc", "offset=abc", "offset=-5"])
def test_数値でないページング指定でも落ちない(client, query):
    resp = client.get(f"/api/summary?{query}")

    assert resp.status_code == 200
    assert resp.get_json()["offset"] >= 0


def test_空の絞り込みは指定なしと同じ(client):
    reviews.create(repo="my-repo", pr_number=1, body="本文")

    assert client.get("/api/summary?repo=&q=%20").get_json()["total"] == 1


def test_集計の詳細で本文を返す(client):
    review = reviews.create(
        repo="my-repo", pr_number=1, body="## 指摘", model="m", details="バナー"
    )

    data = client.get(f"/api/summary/{review.id}").get_json()

    assert data["review_text"] == "codex review (m, rev-0):\n\n## 指摘"
    assert data["details"] == "バナー"


def test_存在しないレビューの詳細は_404(client):
    assert client.get("/api/summary/999").status_code == 404


def test_集計を開いてもタイトル取得を待たない(
    temp_db, temp_paths, review_owner, worker, monkeypatch
):
    calls: list[int] = []
    monkeypatch.setattr(reviews, "refresh_titles_async", lambda *a, **k: calls.append(1))
    app = server.create_app()

    app.test_client().get("/api/summary")

    assert calls == [1]


# --- エクスポート -----------------------------------------------------------
def test_レビュー単位の_csv_をダウンロードさせる(client):
    reviews.create(repo="my-repo", pr_number=1, body="- [P1] t — a.py:1\n")

    resp = client.get("/api/export/reviews.csv")

    assert resp.status_code == 200
    assert resp.content_type == "text/csv; charset=utf-8"
    assert resp.headers["Content-Disposition"].startswith('attachment; filename="reviews-')
    lines = resp.get_data(as_text=True).removeprefix("\ufeff").splitlines()
    assert lines[0].startswith("review_id,repo,pr_number,")
    assert len(lines) == 2


def test_指摘単位の_csv_も集計タブと同じ条件で絞り込める(client):
    reviews.create(repo="my-repo", pr_number=1, body="- [P1] t — a.py:1\n")
    reviews.create(repo="other", pr_number=2, body="- [P2] u — b.py:1\n")

    resp = client.get("/api/export/findings.csv?repo=other")

    assert resp.headers["Content-Disposition"].startswith('attachment; filename="findings-')
    lines = resp.get_data(as_text=True).removeprefix("\ufeff").splitlines()
    assert len(lines) == 2
    assert lines[1].split(",")[1] == "other"
