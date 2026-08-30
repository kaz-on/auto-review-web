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
        print("model: gpt-5.6-terra", file=sys.stderr)
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
    repo_path = work / PR.repo
    repo_path.mkdir()
    seen: dict = {}

    def fake_prepare(pr, progress):
        seen["prepare"] = pr
        return repo_path

    def fake_checkout(pr, path, base_ref, progress):
        seen["base_ref"] = base_ref
        return "basecommit1234", "headcommit5678"

    monkeypatch.setattr(review, "_prepare_repo", fake_prepare)
    monkeypatch.setattr(review, "_checkout_pr", fake_checkout)
    monkeypatch.setattr(review, "_check_tools", lambda: None)
    return seen


@pytest.fixture
def stub_github(monkeypatch):
    monkeypatch.setattr(github_auth, "get_provider", lambda: object())
    monkeypatch.setattr(
        github_api, "get_pull",
        lambda *a, **k: github_api.PullRequest(
            base_ref="main", title="PR のタイトル", body="PR の本文"
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
    assert result.base_commit == "basecommit1234"
    # レビューした版を残しておき、投稿時に行コメントの commit_id として使う。
    assert result.head_commit == "headcommit5678"
    assert result.model == "gpt-5.6-terra"
    assert result.reasoning_effort == "medium"
    assert result.codex_version == "1.2.3"
    assert result.session_id == "0199-abcd"
    # err ログの先頭だけを残し、トランスクリプトは持ち込まない。
    assert "git diff" not in result.details
    assert stub_git["base_ref"] == "main"
    assert any("rev-0" in line for line in lines)


def test_pr_のタイトルをキャッシュする(fake_codex, stub_git, stub_github):
    review.run_review(PR)

    assert reviews.cached_title(PR.repo, PR.number) == "PR のタイトル"


def test_err_ログは_rev_付きの_gzip_で残る(fake_codex, stub_git, stub_github, temp_paths):
    _, logs = temp_paths

    result = review.run_review(PR)

    path = logs / PR.repo / "codex_review_pr7_err.log-0.gz"
    # 置き場所はレビュー登録後に結び付けるので、返り値ではなく DB を見る。
    assert reviews.get(result.id).stderr_path == str(path)
    assert "OpenAI Codex v1.2.3" in gzip.open(path, "rt").read()
    # 一時ファイルは残さない。
    assert [p.name for p in (logs / PR.repo).iterdir()] == [path.name]


def test_同じ_pr_を再実行すると_rev_が上がる(fake_codex, stub_git, stub_github, temp_paths):
    _, logs = temp_paths

    assert review.run_review(PR).rev == 0
    assert review.run_review(PR).rev == 1

    names = sorted(p.name for p in (logs / PR.repo).iterdir())
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
    assert list((logs / PR.repo).iterdir()) == []


def test_pr_の情報が取れなければ_git_にも触らない(monkeypatch, stub_git):
    monkeypatch.setattr(github_auth, "get_provider", lambda: object())

    def fail(*a, **k):
        raise github_api.ApiError("PR が見つかりません", status=404)

    monkeypatch.setattr(github_api, "get_pull", fail)

    with pytest.raises(ReviewError, match="見つかりません"):
        review.run_review(PR)
    assert "prepare" not in stub_git


# --- 投稿 -------------------------------------------------------------------
def test_投稿ではヘッダ付きの本文を送る(monkeypatch):
    posted: list[tuple] = []
    monkeypatch.setattr(github_auth, "get_provider", lambda: object())
    monkeypatch.setattr(
        github_api, "create_review_comment",
        lambda provider, owner, repo, number, body: posted.append(
            (owner, repo, number, body)
        ),
    )
    created = reviews.create(
        repo=PR.repo, pr_number=PR.number, body="## 指摘", model="m"
    )

    review.post_review(PR, created.id)

    assert posted == [
        ("example-org", "my-repo", 7, "codex review (m, rev-0):\n\n## 指摘")
    ]


def test_存在しないレビューは投稿しない():
    with pytest.raises(ReviewError, match="レビューが見つかりません"):
        review.post_review(PR, 999)
