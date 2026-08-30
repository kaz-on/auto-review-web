"""codex / git を駆動する周辺処理。

外部コマンドの呼び出しは、本物の git と、codex の代役に仕立てたスクリプトで確かめる。
codex の err ログはアプリが「先頭だけ読んで捨てる」設計なので、その読み取りを重点的に見る。
"""

from __future__ import annotations

import gzip
import os
import shutil
import stat
import subprocess
import textwrap

import pytest

from webreview import config, github_auth, review
from webreview.errors import ReviewError
from webreview.review import ParsedPR

需要git = pytest.mark.skipif(shutil.which("git") is None, reason="git が無い")


def _write_script(path, body: str) -> str:
    """codex の代役になる実行可能スクリプトを作る。"""
    path.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(body))
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


# --- err ログの読み取り ------------------------------------------------------
BANNER = """\
OpenAI Codex v1.2.3 (research preview)
--------
workdir: /work
model: gpt-5.6-terra
reasoning effort: medium
session id: 0199-abcd
--------
exec
bash -lc 'git diff'
"""


def test_err_ログの先頭から実行条件とバナー項目を取り出す(tmp_path):
    path = tmp_path / "err.log"
    path.write_text(BANNER)

    details, fields = review.read_err_header(path)

    assert fields == {
        "codex_version": "1.2.3",
        "model": "gpt-5.6-terra",
        "reasoning_effort": "medium",
        "session_id": "0199-abcd",
    }
    # 最初の exec 以降 (トランスクリプト本体) は取り込まない。
    assert "git diff" not in details
    assert details.splitlines()[0].startswith("OpenAI Codex v1.2.3")


def test_バナーに項目が欠けていても読める(tmp_path):
    path = tmp_path / "err.log"
    path.write_text("model: gpt-5.6-terra\nexec\n")

    details, fields = review.read_err_header(path)

    assert fields == {"model": "gpt-5.6-terra"}
    assert details == "model: gpt-5.6-terra"


def test_exec_が現れないログでも_max_lines_で打ち切る(tmp_path):
    path = tmp_path / "err.log"
    path.write_text("\n".join(f"line {i}" for i in range(1000)))

    details, _ = review.read_err_header(path, max_lines=10)

    assert len(details.splitlines()) == 10


def test_err_ログが読めなければ空を返す(tmp_path):
    details, fields = review.read_err_header(tmp_path / "missing.log")
    assert (details, fields) == ("", {})


# --- 失敗時の抜粋 ------------------------------------------------------------
def test_tail_は末尾だけを返す(tmp_path):
    path = tmp_path / "err.log"
    path.write_text("x" * 5000 + "END")

    assert review._tail(path, limit=100).endswith("END")
    assert len(review._tail(path, limit=100)) == 100


def test_tail_はマルチバイト文字の途中で切れても壊れない(tmp_path):
    path = tmp_path / "err.log"
    path.write_text("あ" * 3000)

    tail = review._tail(path, limit=50)

    # 置換文字が混ざることはあっても、例外にはしない。
    assert tail.endswith("あ")


def test_tail_はファイルが無ければ空文字(tmp_path):
    assert review._tail(tmp_path / "missing.log") == ""


# --- codex の起動引数 --------------------------------------------------------
def test_codex_の引数に承認不要と読み取り専用サンドボックスを含める(monkeypatch):
    monkeypatch.setattr(config, "CODEX_BIN", "codex")
    monkeypatch.setattr(config, "REVIEW_MODEL", "gpt-5.6-terra")
    monkeypatch.setattr(config, "REVIEW_REASONING_EFFORT", "high")

    args = review._codex_args("PROMPT")

    assert args[0] == "codex"
    assert args[-2:] == ["review", "PROMPT"]
    assert "--config" in args and "model_reasoning_effort=high" in args
    assert args[args.index("--sandbox") + 1] == "read-only"
    assert args[args.index("--ask-for-approval") + 1] == "never"


def test_推論の深さが空ならオプションを渡さない(monkeypatch):
    monkeypatch.setattr(config, "REVIEW_REASONING_EFFORT", "")

    args = review._codex_args("PROMPT")

    assert not any(a.startswith("model_reasoning_effort=") for a in args)
    assert review._model_label() == config.REVIEW_MODEL


def test_モデル表記に推論の深さを併記する(monkeypatch):
    monkeypatch.setattr(config, "REVIEW_MODEL", "gpt-5.6-terra")
    monkeypatch.setattr(config, "REVIEW_REASONING_EFFORT", "low")

    assert review._model_label() == "gpt-5.6-terra, reasoning=low"


# --- 環境変数 ----------------------------------------------------------------
def test_codex_には_github_の認証情報を渡さない(monkeypatch):
    for name in ("GH_TOKEN", "GITHUB_TOKEN", "REVIEW_GH_TOKEN",
                 "GITHUB_APP_PRIVATE_KEY", "GITHUB_APP_PRIVATE_KEY_PATH"):
        monkeypatch.setenv(name, "secret")
    monkeypatch.setenv("PATH", os.environ["PATH"])

    env = review._codex_env()

    assert not any(name in env for name in (
        "GH_TOKEN", "GITHUB_TOKEN", "REVIEW_GH_TOKEN",
        "GITHUB_APP_PRIVATE_KEY", "GITHUB_APP_PRIVATE_KEY_PATH",
    ))
    assert "PATH" in env


def test_git_にはトークンを環境変数の_credential_helper_で渡す(monkeypatch):
    class Provider:
        def token(self) -> str:
            return "gh-token"

    monkeypatch.setattr(github_auth, "get_provider", lambda: Provider())

    env = review._git_env()

    assert env["REVIEW_GH_TOKEN"] == "gh-token"
    # 既存の helper を空値で打ち消してから自前を差し込む。
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert env["GIT_CONFIG_VALUE_0"] == ""
    assert "REVIEW_GH_TOKEN" in env["GIT_CONFIG_VALUE_1"]
    assert env["GIT_TERMINAL_PROMPT"] == "0"


# --- 外部コマンドの実行 ------------------------------------------------------
def test_run_は_stdout_を返し進捗にコマンドを記録する():
    lines: list[str] = []

    out = review._run(["echo", "hello"], progress=lines.append)

    assert out == "hello"
    assert lines == ["$ echo hello"]


def test_run_は失敗時に_stderr_を含む_reviewerror(tmp_path):
    script = _write_script(tmp_path / "fail.py", """
        import sys
        print("boom", file=sys.stderr)
        sys.exit(3)
    """)

    with pytest.raises(ReviewError, match="boom"):
        review._run([script])


def test_run_はコマンドが無ければ分かる形で失敗する():
    with pytest.raises(ReviewError, match="コマンドが見つかりません"):
        review._run(["/nonexistent/command/xyz"])


def test_必要な外部コマンドが無ければ実行前に止める(monkeypatch):
    monkeypatch.setattr(config, "CODEX_BIN", "/nonexistent/codex-xyz")
    with pytest.raises(ReviewError, match="codex がインストールされていません"):
        review._check_tools()


# --- codex の実行 ------------------------------------------------------------
def test_codex_の_stdout_を本文に_stderr_をログファイルにする(tmp_path, monkeypatch):
    script = _write_script(tmp_path / "codex.py", """
        import sys
        print("OpenAI Codex v9.9.9", file=sys.stderr)
        print("model: fake-model", file=sys.stderr)
        print("exec", file=sys.stderr)
        print("  ## 指摘事項  ")
    """)
    monkeypatch.setattr(config, "CODEX_BIN", script)
    monkeypatch.setattr(config, "REVIEW_TIMEOUT", 60)
    err_path = tmp_path / "err.log"

    run = review._run_codex("PROMPT", tmp_path, err_path)

    assert run.body == "## 指摘事項"
    assert run.codex_version == "9.9.9"
    # 実際に使われた設定は、こちらが渡した値ではなくログのバナーを優先する。
    assert run.model == "fake-model"
    assert "exec" not in run.details


def test_codex_が失敗したら_err_ログの末尾を添えて知らせる(tmp_path, monkeypatch):
    script = _write_script(tmp_path / "codex.py", """
        import sys
        print("fatal: rate limited", file=sys.stderr)
        sys.exit(1)
    """)
    monkeypatch.setattr(config, "CODEX_BIN", script)
    monkeypatch.setattr(config, "REVIEW_TIMEOUT", 60)

    with pytest.raises(ReviewError, match="rate limited") as e:
        review._run_codex("PROMPT", tmp_path, tmp_path / "err.log")
    assert "exit=1" in str(e.value)


def test_codex_が制限時間を超えたら中断する(tmp_path, monkeypatch):
    # 子プロセスを残して固まる codex を模す。親だけ kill すると stdout が閉じない。
    script = _write_script(tmp_path / "codex.py", """
        import subprocess, sys, time
        subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        time.sleep(60)
    """)
    monkeypatch.setattr(config, "CODEX_BIN", script)
    monkeypatch.setattr(config, "REVIEW_TIMEOUT", 1)

    with pytest.raises(ReviewError, match="制限時間"):
        review._run_codex("PROMPT", tmp_path, tmp_path / "err.log")


def test_codex_が見つからなければ実行時にも分かる形で失敗する(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CODEX_BIN", "/nonexistent/codex-xyz")

    with pytest.raises(ReviewError, match="コマンドが見つかりません"):
        review._run_codex("PROMPT", tmp_path, tmp_path / "err.log")


# --- err ログの後始末 --------------------------------------------------------
def test_err_ログは_rev_付きの名前で_gzip_される(tmp_path):
    tmp_log = tmp_path / ".codex_review_pr7_err.tmp"
    tmp_log.write_text("transcript")
    pr = ParsedPR(owner="example-org", repo="my-repo", number=7)

    final = review._finalize_err_log(tmp_log, pr, rev=2)

    assert final.name == "codex_review_pr7_err.log-2.gz"
    assert gzip.open(final, "rt").read() == "transcript"
    assert not tmp_log.exists()


def test_err_ログの保存に失敗してもレビューは成立させる(tmp_path):
    # 元ファイルが無い = 保存できない状況。例外にせず元のパスを返す。
    tmp_log = tmp_path / "missing.tmp"
    pr = ParsedPR(owner="example-org", repo="my-repo", number=7)

    assert review._finalize_err_log(tmp_log, pr, rev=0) == tmp_log


# --- プロンプト --------------------------------------------------------------
def test_プロンプトに比較対象のコミットと_pr_本文を埋める():
    from webreview.github_api import PullRequest

    pull = PullRequest(base_ref="main", title="タイトル", body="本文")

    prompt = review._build_prompt(pull, "deadbeef")

    assert "git diff deadbeef...HEAD" in prompt
    assert "'main'" in prompt
    assert "<PR_BODY>\n本文\n</PR_BODY>" in prompt
    assert "タイトル" in prompt


# --- リポジトリの準備 --------------------------------------------------------
@需要git
def test_作業ディレクトリの_remote_が_pr_と違えば実行しない(temp_paths, monkeypatch):
    work, _ = temp_paths
    repo_path = work / "my-repo"
    subprocess.run(["git", "init", "-q", str(repo_path)], check=True)
    subprocess.run(
        ["git", "-C", str(repo_path), "remote", "add", "origin",
         "https://github.com/example-org/other-repo.git"],
        check=True,
    )
    monkeypatch.setattr(github_auth, "get_provider", lambda: None)
    pr = ParsedPR(owner="example-org", repo="my-repo", number=1)

    with pytest.raises(ReviewError, match="一致しません"):
        review._prepare_repo(pr, lambda _msg: None)


@需要git
def test_remote_url_の形式が想定外なら実行しない(temp_paths, monkeypatch):
    work, _ = temp_paths
    repo_path = work / "my-repo"
    subprocess.run(["git", "init", "-q", str(repo_path)], check=True)
    subprocess.run(
        ["git", "-C", str(repo_path), "remote", "add", "origin",
         "https://gitlab.example.com/example-org/my-repo.git"],
        check=True,
    )
    pr = ParsedPR(owner="example-org", repo="my-repo", number=1)

    with pytest.raises(ReviewError, match="未対応の remote URL"):
        review._prepare_repo(pr, lambda _msg: None)


def test_git_リポジトリでないディレクトリは弾く(temp_paths):
    work, _ = temp_paths
    (work / "my-repo").mkdir()
    pr = ParsedPR(owner="example-org", repo="my-repo", number=1)

    with pytest.raises(ReviewError, match="git リポジトリではありません"):
        review._prepare_repo(pr, lambda _msg: None)
