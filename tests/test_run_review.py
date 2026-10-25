"""run_review / post_review の通し。

git の準備と GitHub API は差し替え、codex だけ本物のプロセスとして起動する
(代役スクリプト)。実行の組み立てと後始末が見どころ。
"""

from __future__ import annotations

import gzip
import stat
import textwrap

import pytest

from webreview import config, github_api, github_auth, review, reviews
from webreview.errors import ReviewError
from webreview.review import ParsedPR

pytestmark = pytest.mark.usefixtures("temp_db")

PR = ParsedPR(owner="example-org", repo="my-repo", number=7)


@pytest.fixture
def fake_codex(tmp_path, monkeypatch):
    """codex の代役。stdout に本文、stderr にバナーとトランスクリプトを吐く。"""
    script = tmp_path / "codex.py"
    script.write_text(textwrap.dedent("""\
        #!/usr/bin/env python3
        import sys
        print("OpenAI Codex v1.2.3", file=sys.stderr)
        print("model: gpt-6.1-sol", file=sys.stderr)
        print("reasoning effort: medium", file=sys.stderr)
        print("session id: 0199-abcd", file=sys.stderr)
        print("exec", file=sys.stderr)
        print("bash -lc 'git diff'", file=sys.stderr)
        print("## 指摘事項\\n- 何か")
    """))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(config, "CODEX_BIN", str(script))
    monkeypatch.setattr(config, "REVIEW_TIMEOUT", 60)
    return script


@pytest.fixture
def stub_git(monkeypatch, temp_paths):
    """git の準備と checkout を差し替え、渡された引数を記録する。"""
    work, _ = temp_paths
    repo_path = work / PR.owner / PR.repo
    repo_path.mkdir(parents=True)
    seen: dict = {}

    def fake_prepare(pr, progress):
        seen["prepare"] = pr
        return repo_path

    def fake_checkout(pr, path, pull, progress):
        seen["pull"] = pull

    monkeypatch.setattr(review, "_prepare_repo", fake_prepare)
    monkeypatch.setattr(review, "_checkout_pr", fake_checkout)
    monkeypatch.setattr(review, "_check_tools", lambda: None)
    return seen


@pytest.fixture
def stub_github(monkeypatch):
    monkeypatch.setattr(github_auth, "get_provider", lambda *_: object())
    monkeypatch.setattr(
        github_api, "get_pull",
        lambda *a, **k: github_api.PullRequest(
            base_ref="main",
            base_sha="basecommit1234",
            head_sha="headcommit5678",
            title="PR のタイトル",
            body="PR の本文",
            author="alice",
            additions=120,
            deletions=30,
            changed_files=4,
        ),
    )


def test_レビューを最後まで実行して保存する(
    fake_codex, stub_git, stub_github, temp_paths
):
    lines: list[str] = []

    result = review.run_review(PR, progress=lines.append)

    assert result.rev == 0
    assert result.body == "## 指摘事項\n- 何か"
    assert result.base_ref == "main"
    # base / head はどちらも API が返した SHA をそのまま残す。
    assert result.base_commit == "basecommit1234"
    # レビューした版を残しておき、投稿時に行コメントの commit_id として使う。
    assert result.head_commit == "headcommit5678"
    assert result.model == "gpt-6.1-sol"
    assert result.reasoning_effort == "medium"
    assert result.codex_version == "1.2.3"
    assert result.session_id == "0199-abcd"
    # err ログの先頭だけを残し、トランスクリプトは持ち込まない。
    assert "git diff" not in result.details
    assert stub_git["pull"].base_ref == "main"
    assert any("rev-0" in line for line in lines)


def test_品質改善を含める指定はプロンプトとレビューに残る(
    fake_codex, stub_git, stub_github, monkeypatch
):
    seen: dict = {}
    original = review._build_prompt

    def spy(pull, include_quality=False):
        seen["include_quality"] = include_quality
        return original(pull, include_quality)

    monkeypatch.setattr(review, "_build_prompt", spy)

    result = review.run_review(PR, include_quality=True)

    assert seen["include_quality"] is True
    assert reviews.get(result.id).include_quality is True


def test_pr_のタイトルをキャッシュする(fake_codex, stub_git, stub_github):
    review.run_review(PR)

    assert reviews.cached_title(PR.owner, PR.repo, PR.number) == "PR のタイトル"


def test_pr_の作成者と変更の規模をレビューに記録する(fake_codex, stub_git, stub_github):
    result = review.run_review(PR)

    got = reviews.get(result.id)
    assert got.pr_author == "alice"
    assert (got.pr_additions, got.pr_deletions, got.pr_changed_files) == (120, 30, 4)


def test_err_ログは_rev_付きの_gzip_で残る(fake_codex, stub_git, stub_github, temp_paths):
    _, logs = temp_paths

    result = review.run_review(PR)

    path = logs / PR.owner / PR.repo / "codex_review_pr7_err.log-0.gz"
    # 置き場所はレビュー登録後に結び付けるので、返り値ではなく DB を見る。
    assert reviews.get(result.id).stderr_path == str(path)
    assert "OpenAI Codex v1.2.3" in gzip.open(path, "rt").read()
    # 一時ファイルは残さない。
    assert [p.name for p in (logs / PR.owner / PR.repo).iterdir()] == [path.name]


def test_同じ_pr_を再実行すると_rev_が上がる(fake_codex, stub_git, stub_github, temp_paths):
    _, logs = temp_paths

    assert review.run_review(PR).rev == 0
    assert review.run_review(PR).rev == 1

    names = sorted(p.name for p in (logs / PR.owner / PR.repo).iterdir())
    assert names == [
        "codex_review_pr7_err.log-0.gz", "codex_review_pr7_err.log-1.gz",
    ]


def test_codex_が失敗したらレビューを残さず一時ログも片付ける(
    tmp_path, stub_git, stub_github, temp_paths, monkeypatch
):
    script = tmp_path / "codex.py"
    script.write_text("#!/usr/bin/env python3\nimport sys; sys.exit(1)\n")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setattr(config, "CODEX_BIN", str(script))
    monkeypatch.setattr(config, "REVIEW_TIMEOUT", 60)
    _, logs = temp_paths

    with pytest.raises(ReviewError):
        review.run_review(PR)

    assert reviews.count() == 0
    assert list((logs / PR.owner / PR.repo).iterdir()) == []


def test_pr_の情報が取れなければ_git_にも触らない(monkeypatch, stub_git):
    monkeypatch.setattr(github_auth, "get_provider", lambda *_: object())

    def fail(*a, **k):
        raise github_api.ApiError("PR が見つかりません", status=404)

    monkeypatch.setattr(github_api, "get_pull", fail)

    with pytest.raises(ReviewError, match="見つかりません"):
        review.run_review(PR)
    assert "prepare" not in stub_git


# --- 投稿 -------------------------------------------------------------------
@pytest.fixture
def capture_post(monkeypatch):
    """create_review の呼び出しを記録する。errors に積むと順に失敗させられる。"""
    calls: list[dict] = []
    errors: list[Exception] = []

    monkeypatch.setattr(github_auth, "get_provider", lambda *_: object())

    def fake_create_review(
        provider, owner, repo, number, body, *, comments=None, commit_id=None
    ):
        calls.append({
            "target": (owner, repo, number),
            "body": body,
            "comments": comments,
            "commit_id": commit_id,
        })
        if errors:
            raise errors.pop(0)

    monkeypatch.setattr(github_api, "create_review", fake_create_review)
    return calls, errors


def _review_with_findings(root):
    """位置の読める指摘を 1 件持つレビューを作る。root は codex が動いた作業ツリー。"""
    return reviews.create(
        owner=PR.owner,
        repo=PR.repo,
        pr_number=PR.number,
        body=(
            "全体の要約。\n\n"
            "Full review comments:\n\n"
            f"- [P1] 指摘 — {root / 'order.py'}:16-16\n"
            "  説明。\n"
        ),
        head_commit="headcommit5678",
        model="m",
    )


def test_位置の読める指摘はソース行へのコメントにする(capture_post, temp_paths):
    calls, _ = capture_post
    work, _ = temp_paths
    created = _review_with_findings(work / PR.owner / PR.repo)
    lines: list[str] = []

    review.post_review(PR, created.id, progress=lines.append)

    assert len(calls) == 1
    call = calls[0]
    assert call["target"] == ("example-org", "my-repo", 7)
    # 本文にはヘッダと要約だけが残り、指摘は行コメントへ回る。
    assert call["body"] == "codex review (m, rev-0):\n\n全体の要約。"
    assert call["comments"] == [{
        "path": "order.py",
        "line": 16,
        "side": "RIGHT",
        "body": "**[P1] 指摘**\n\n説明。",
    }]
    # レビューした版に紐付ける。
    assert call["commit_id"] == "headcommit5678"
    assert any("ソース行" in line for line in lines)


def test_オーナーを固定していた頃のレビューも行コメントにできる(capture_post, temp_paths):
    """当時は REVIEW_WORK_PATH/<repo> で codex が動いていた。"""
    calls, _ = capture_post
    work, _ = temp_paths
    created = _review_with_findings(work / PR.repo)

    review.post_review(PR, created.id)

    assert [c["path"] for c in calls[0]["comments"]] == ["order.py"]


def test_位置が読めなければヘッダ付きの本文だけを送る(capture_post):
    calls, _ = capture_post
    created = reviews.create(
        owner=PR.owner, repo=PR.repo, pr_number=PR.number, body="## 指摘", model="m"
    )

    review.post_review(PR, created.id)

    assert calls == [{
        "target": ("example-org", "my-repo", 7),
        "body": "codex review (m, rev-0):\n\n## 指摘",
        "comments": None,
        "commit_id": None,
    }]


def test_行コメントが弾かれたら本文にまとめて投稿し直す(capture_post, temp_paths):
    """差分に無い行を指した (422) だけで投稿を諦めない。"""
    calls, errors = capture_post
    work, _ = temp_paths
    errors.append(github_api.ApiError("Path could not be resolved", status=422))
    created = _review_with_findings(work / PR.owner / PR.repo)
    lines: list[str] = []

    review.post_review(PR, created.id, progress=lines.append)

    assert len(calls) == 2
    # 2 回目は行コメントを付けず、指摘を含んだ本文をそのまま送る。
    assert calls[1]["comments"] is None
    assert "[P1] 指摘" in calls[1]["body"]
    # 2 回目も commit_id を維持する。
    assert calls[1]["commit_id"] == "headcommit5678"
    assert any("投稿し直します" in line for line in lines)


def test_422_以外の失敗は投稿の失敗として伝える(capture_post, temp_paths):
    calls, errors = capture_post
    work, _ = temp_paths
    errors.append(github_api.ApiError("Not Found", status=404))
    created = _review_with_findings(work / PR.owner / PR.repo)

    with pytest.raises(ReviewError, match="Not Found"):
        review.post_review(PR, created.id)

    assert len(calls) == 1


def test_存在しないレビューは投稿しない():
    with pytest.raises(ReviewError, match="レビューが見つかりません"):
        review.post_review(PR, 999)
