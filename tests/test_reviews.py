"""レビュー本文の永続化・集計・PR タイトルのキャッシュ。

rev の採番と一覧の絞り込みが中心。ヘッダは保存せず表示時に組み立てる設計なので、
書式を変えると過去分にも及ぶことを確かめる。
"""

from __future__ import annotations

import time

import pytest

from webreview import config, db, github_api, github_auth, reviews
from webreview.errors import ReviewError

pytestmark = pytest.mark.usefixtures("temp_db")


def _create(
    repo: str = "my-repo",
    pr_number: int = 1,
    body: str = "本文",
    owner: str = "example-org",
    **kw,
):
    return reviews.create(owner=owner, repo=repo, pr_number=pr_number, body=body, **kw)


# --- 保存と採番 -------------------------------------------------------------
def test_保存した内容がそのまま読み出せる():
    review = _create(
        body="## 指摘",
        base_ref="main",
        base_commit="deadbeef",
        head_commit="cafebabe",
        model="gpt-6.1-sol",
        reasoning_effort="medium",
        codex_version="1.2.3",
        session_id="0199",
        details="バナー",
        pr_author="alice",
        pr_additions=10,
        pr_deletions=2,
        pr_changed_files=1,
    )

    got = reviews.get(review.id)

    assert got.body == "## 指摘"
    assert got.include_quality is False
    assert got.base_ref == "main"
    assert got.base_commit == "deadbeef"
    assert got.head_commit == "cafebabe"
    assert got.model == "gpt-6.1-sol"
    assert got.codex_version == "1.2.3"
    assert got.session_id == "0199"
    assert got.details == "バナー"
    assert got.pr_author == "alice"
    assert (got.pr_additions, got.pr_deletions, got.pr_changed_files) == (10, 2, 1)
    assert got.stderr_pruned_at is None


def test_rev_は同じ_pr_ごとに_0_から採番される():
    assert _create(pr_number=1).rev == 0
    assert _create(pr_number=1).rev == 1
    assert _create(pr_number=1).rev == 2
    # PR が違えば独立に採番する。
    assert _create(pr_number=2).rev == 0
    # リポジトリが違っても独立。
    assert _create(repo="other-repo", pr_number=1).rev == 0
    # 名前が同じでもオーナーが違えば別のリポジトリ。
    assert _create(owner="other-org", pr_number=1).rev == 0


def test_rev_を明示して保存できる():
    assert _create(rev=7).rev == 7
    # 明示したぶんを踏まえて次が採番される。
    assert _create().rev == 8


def test_同じ_repo_pr_rev_は重複させない():
    import sqlite3

    _create(rev=0)
    with pytest.raises(sqlite3.IntegrityError):
        _create(rev=0)


def test_存在しないレビューは_none():
    assert reviews.get(999) is None


def test_err_ログの置き場所を後から結び付けられる(tmp_path):
    review = _create()
    path = tmp_path / "err.log.gz"

    reviews.set_stderr_path(review.id, path)
    assert reviews.get(review.id).stderr_path == str(path)

    reviews.set_stderr_path(review.id, None)
    assert reviews.get(review.id).stderr_path is None


# --- 表示・投稿用の整形 -----------------------------------------------------
def test_ヘッダはカラムから毎回組み立てる():
    review = _create(model="gpt-6.1-sol", reasoning_effort="medium")

    assert reviews.header_line(review) == (
        "codex review (gpt-6.1-sol, reasoning=medium, rev-0):"
    )


def test_品質改善を含めた実行はヘッダで見分けられる():
    review = _create(model="m", include_quality=True)

    assert reviews.header_line(review) == "codex review (m, quality, rev-0):"


def test_実行時設定が残っていない古いレビューでもヘッダを出せる():
    review = _create(rev=3)

    assert reviews.header_line(review) == "codex review (rev-3):"


def test_投稿本文はヘッダと本体を空行で挟む():
    review = _create(body="## 指摘", model="m")

    assert reviews.render_body(review) == "codex review (m, rev-0):\n\n## 指摘"


# --- 集計一覧 ---------------------------------------------------------------
def test_一覧は新しい順で本文を含まない():
    _create(pr_number=1, body="あ" * 10, created_at=100.0)
    _create(pr_number=2, body="い", created_at=200.0)

    entries = reviews.list_summary()

    assert [e["pr_num"] for e in entries] == [2, 1]
    assert "body" not in entries[0]
    # 本文の量はバイト数で示す (LENGTH は文字数を返すため CAST している)。
    assert entries[1]["body_bytes"] == len(("あ" * 10).encode("utf-8"))


def test_一覧の_url_はレビューのオーナーから組み立てる():
    _create(owner="other-org", repo="my-repo", pr_number=42)

    entry = reviews.list_summary()[0]
    assert entry["owner"] == "other-org"
    assert entry["url"] == "https://github.com/other-org/my-repo/pull/42"


def test_一覧は詳細の有無を示す():
    _create(pr_number=1, details="バナー")
    _create(pr_number=2)

    by_pr = {e["pr_num"]: e for e in reviews.list_summary()}

    assert by_pr[1]["has_details"] is True
    assert by_pr[2]["has_details"] is False


def test_リポジトリで絞り込める():
    _create(repo="repo-a")
    _create(repo="repo-b")
    _create(owner="other-org", repo="repo-a")

    assert reviews.count(repo="example-org/repo-a") == 1
    entries = reviews.list_summary(repo="example-org/repo-a")
    assert [(e["owner"], e["repo"]) for e in entries] == [("example-org", "repo-a")]


def test_リポジトリ名だけならオーナーを問わず絞り込む():
    """オーナーを 1 つに固定していた頃の指定 (repo=名前) をそのまま使えるように。"""
    _create(repo="repo-a")
    _create(repo="repo-b")
    _create(owner="other-org", repo="repo-a")

    assert reviews.count(repo="repo-a") == 2


def test_サブグループを含むオーナーでも絞り込める():
    """GitLab のオーナーは "group/sub" の形になりうるので、最後の "/" で区切る。"""
    _create(owner="group/sub", repo="repo-a")
    _create(owner="group", repo="repo-a")

    assert reviews.count(repo="group/sub/repo-a") == 1


def test_本文の部分一致で絞り込める():
    _create(pr_number=1, body="SQL injection の懸念")
    _create(pr_number=2, body="命名の指摘")

    assert reviews.count(q="injection") == 1
    assert reviews.list_summary(q="injection")[0]["pr_num"] == 1


def test_絞り込みは併用できる():
    _create(repo="repo-a", body="injection")
    _create(repo="repo-b", body="injection")

    assert reviews.count(repo="example-org/repo-a", q="injection") == 1


def test_ページングできる():
    for n in range(5):
        # created_at は偽値だと採番に落ちるので 0 を避ける。
        _create(pr_number=n, created_at=float(n) + 1)

    assert [e["pr_num"] for e in reviews.list_summary(limit=2)] == [4, 3]
    assert [e["pr_num"] for e in reviews.list_summary(limit=2, offset=2)] == [2, 1]
    assert reviews.count() == 5


def test_件数の上限と下限を守る():
    for n in range(3):
        _create(pr_number=n)

    assert len(reviews.list_summary(limit=0)) == 1
    assert len(reviews.list_summary(limit=reviews.MAX_LIMIT * 10)) == 3


def test_絞り込み条件が無ければ_where_を付けない():
    where, params = reviews._filter(None, None)
    assert (where, params) == ("", [])


def test_詳細ではヘッダ付きの本文を返す():
    review = _create(body="## 指摘", model="m", details="バナー")

    detail = reviews.get_detail(review.id)

    assert detail["review_text"] == "codex review (m, rev-0):\n\n## 指摘"
    assert detail["details"] == "バナー"


def test_存在しないレビューの詳細は_none():
    assert reviews.get_detail(999) is None


def test_絞り込み用のリポジトリ名は重複せず並ぶ():
    _create(repo="repo-b", pr_number=1)
    _create(repo="repo-a", pr_number=2)
    _create(repo="repo-b", pr_number=3)
    _create(owner="another-org", repo="repo-b", pr_number=1)

    assert reviews.repos() == [
        "another-org/repo-b", "example-org/repo-a", "example-org/repo-b",
    ]


# --- PR タイトルのキャッシュ ------------------------------------------------
def test_タイトルを保存して読める():
    reviews.put_title("example-org", "my-repo", 1, "PR のタイトル")

    assert reviews.cached_title("example-org", "my-repo", 1) == "PR のタイトル"


def test_空のタイトルは保存しない():
    reviews.put_title("example-org", "my-repo", 1, "")

    assert reviews.cached_title("example-org", "my-repo", 1) is None


def test_タイトルは上書きできる():
    reviews.put_title("example-org", "my-repo", 1, "旧タイトル")
    reviews.put_title("example-org", "my-repo", 1, "新タイトル")

    assert reviews.cached_title("example-org", "my-repo", 1) == "新タイトル"


def test_期限切れのタイトルは使わない():
    """PR 名は変更されうるので恒久キャッシュにはしない。"""
    with db.write() as conn:
        conn.execute(
            "INSERT INTO pr_titles (owner, repo, pr_number, title, fetched_at) "
            "VALUES (?, ?, ?, ?, ?)",
            ("example-org", "my-repo", 1, "古いタイトル", time.time() - reviews.TITLE_TTL_SECONDS - 1),
        )

    assert reviews.cached_title("example-org", "my-repo", 1) is None


def test_未取得のタイトルは_none():
    assert reviews.cached_title("example-org", "my-repo", 999) is None


def test_タイトルはオーナーごとに持つ():
    reviews.put_title("example-org", "my-repo", 1, "こちらの PR")

    assert reviews.cached_title("other-org", "my-repo", 1) is None


def test_一覧のタイトルはキャッシュから引く():
    _create(repo="my-repo", pr_number=1)
    reviews.put_title("example-org", "my-repo", 1, "PR のタイトル")

    entries = reviews.list_summary()

    assert entries[0]["title"] == "PR のタイトル"


def test_タイトル未取得のレビューも一覧に出す():
    _create(repo="my-repo", pr_number=1)

    assert reviews.list_summary()[0]["title"] == ""


# --- タイトルの取り直し -----------------------------------------------------
def _pull(title: str) -> github_api.PullRequest:
    """取り直しで使うのは title だけなので、残りは埋めておく。"""
    return github_api.PullRequest(
        base_ref="main", base_sha="b" * 40, head_sha="h" * 40, title=title, body="",
    )


def test_未取得と失効ぶんだけを取り直す(monkeypatch):
    _create(repo="my-repo", pr_number=1)
    _create(repo="my-repo", pr_number=2)
    _create(owner="other-org", repo="my-repo", pr_number=2)
    reviews.put_title("example-org", "my-repo", 2, "取得済み")
    asked: list[tuple[str, int]] = []

    def fake_get_pull(provider, owner, repo, number):
        asked.append((owner, number))
        return _pull(f"PR {number}")

    monkeypatch.setattr(github_api, "get_pull", fake_get_pull)
    monkeypatch.setattr(github_auth, "get_provider", lambda *_: object())

    assert reviews.refresh_titles() == 2
    # レビューのオーナーで引く。
    assert sorted(asked) == [("example-org", 1), ("other-org", 2)]
    assert reviews.cached_title("example-org", "my-repo", 1) == "PR 1"
    assert reviews.cached_title("other-org", "my-repo", 2) == "PR 2"


def test_取得件数に上限を設ける(monkeypatch):
    for n in range(5):
        _create(pr_number=n)

    monkeypatch.setattr(
        github_api, "get_pull",
        lambda *a, **k: _pull("t"),
    )
    monkeypatch.setattr(github_auth, "get_provider", lambda *_: object())

    assert reviews.refresh_titles(limit=2) == 2


def test_一部の取得に失敗しても集計は成立させる(monkeypatch):
    _create(pr_number=1)
    _create(pr_number=2)

    def fake_get_pull(provider, owner, repo, number):
        if number == 1:
            raise ReviewError("削除された PR です")
        return _pull("PR 2")

    monkeypatch.setattr(github_api, "get_pull", fake_get_pull)
    monkeypatch.setattr(github_auth, "get_provider", lambda *_: object())

    assert reviews.refresh_titles() == 1
    assert reviews.cached_title("example-org", "my-repo", 1) is None
    assert reviews.cached_title("example-org", "my-repo", 2) == "PR 2"


def test_取り直しの多重起動を避ける(monkeypatch):
    """集計は連続で開かれることがある。"""
    calls: list[int] = []
    started = __import__("threading").Event()
    release = __import__("threading").Event()

    def slow_refresh(limit):
        calls.append(limit)
        started.set()
        release.wait(5)
        return 0

    monkeypatch.setattr(reviews, "refresh_titles", slow_refresh)

    reviews.refresh_titles_async()
    started.wait(5)
    reviews.refresh_titles_async()  # 実行中なので無視される
    release.set()

    # 走っているスレッドが終わるのを待ってからフラグを確かめる。
    for _ in range(500):
        if not reviews._refreshing:
            break
        time.sleep(0.01)
    assert calls == [20]
    assert reviews._refreshing is False


# --- err ログの期限削除 -----------------------------------------------------
def test_期限を過ぎた_err_ログを消して痕跡を残す(tmp_path, monkeypatch):
    old_log = tmp_path / "old.log.gz"
    old_log.write_text("x")
    new_log = tmp_path / "new.log.gz"
    new_log.write_text("x")
    now = time.time()
    old = _create(pr_number=1, stderr_path=str(old_log), created_at=now - 40 * 86400)
    new = _create(pr_number=2, stderr_path=str(new_log), created_at=now)
    monkeypatch.setattr(config, "STDERR_RETENTION_DAYS", 30)

    assert reviews.prune_stderr() == 1

    assert not old_log.exists()
    # 行は残したまま、消したことが分かるようにする。
    assert reviews.get(old.id).stderr_path is None
    assert reviews.get(old.id).stderr_pruned_at is not None
    assert reviews.get(old.id).body == "本文"
    # 期限内のぶんは触らない。
    assert new_log.exists()
    assert reviews.get(new.id).stderr_path == str(new_log)


def test_保持日数が_0_なら削除しない(tmp_path, monkeypatch):
    log = tmp_path / "old.log.gz"
    log.write_text("x")
    _create(stderr_path=str(log), created_at=time.time() - 999 * 86400)
    monkeypatch.setattr(config, "STDERR_RETENTION_DAYS", 0)

    assert reviews.prune_stderr() == 0
    assert log.exists()


def test_保持日数は引数で上書きできる(tmp_path, monkeypatch):
    log = tmp_path / "old.log.gz"
    log.write_text("x")
    _create(stderr_path=str(log), created_at=time.time() - 2 * 86400)
    monkeypatch.setattr(config, "STDERR_RETENTION_DAYS", 30)

    assert reviews.prune_stderr(retention_days=1) == 1


def test_実体が既に無くても記録は片付ける(tmp_path, monkeypatch):
    review = _create(
        stderr_path=str(tmp_path / "missing.log.gz"),
        created_at=time.time() - 40 * 86400,
    )
    monkeypatch.setattr(config, "STDERR_RETENTION_DAYS", 30)

    assert reviews.prune_stderr() == 1
    assert reviews.get(review.id).stderr_path is None
