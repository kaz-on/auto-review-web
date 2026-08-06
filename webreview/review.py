"""auto_review.sh を非対話的に実行するためのモジュール。

Web からの呼び出しで人手の確認 (y/N) を挟めないため、元スクリプトの対話部分は
次のように置き換える:

* ログの世代 (rev) が既に存在する場合 → 確認せず新しい rev を自動採番する。
* codex レビューの実行確認 → 常に実行する (ジョブ投入=実行の意思とみなす)。
* PR への投稿確認 → Web UI 上の「投稿」操作で明示的に行う (post_review)。

git の作業ツリーは毎回 PR のコミットへ checkout --force + clean するため、前回実行の
残骸があっても非対話で復帰できる。単一ワーカーで直列実行される前提なので、共有の
作業ディレクトリに対する git 操作が競合することはない。

GitHub へのアクセスは REST API (github_api) と git コマンドの 2 経路。どちらも
トークンは実行の直前に github_auth から取得し、その 1 回の呼び出しにだけ渡す。
git にはコマンドラインではなく環境変数経由の credential helper で渡すので、
トークンが ps や進捗ログに現れることはない。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import config, github_api, github_auth
from .errors import ReviewError

ProgressFn = Callable[[str], None]

__all__ = [
    "ReviewError",
    "ParsedPR",
    "ReviewResult",
    "parse_pr_url",
    "run_review",
    "post_review",
]

# git の認証は「実行時の環境変数からトークンを読む credential helper」で行う。
# こうするとイメージやホストに認証設定を残さず、submodule の子プロセスにも継承される。
_CREDENTIAL_KEY = "credential.https://github.com.helper"
_CREDENTIAL_HELPER = (
    '!f() { if test "$1" = get; then echo username=x-access-token; '
    'echo "password=$REVIEW_GH_TOKEN"; fi; }; f'
)

_PR_URL_RE = re.compile(
    r"^https://github\.com/([^/]+)/([^/]+)/pull/([0-9]+)(?:/.*)?$"
)
_REMOTE_RE = re.compile(
    r"^(?:git@github\.com:|https://github\.com/)([^/]+)/([^/.]+)(?:\.git)?$"
)


@dataclass
class ParsedPR:
    owner: str
    repo: str
    number: int

    @property
    def url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repo}/pull/{self.number}"


@dataclass
class ReviewResult:
    base_ref: str
    base_commit: str
    pr_title: str
    rev: int
    stdout_path: Path
    stderr_path: Path
    review_text: str


def parse_pr_url(pr_url: str) -> ParsedPR:
    """PR URL を owner/repo/number に分解し、Organization 制約を検証する。"""
    pr_url = (pr_url or "").strip()
    if not pr_url:
        raise ReviewError("PR の URL を入力してください。")

    m = _PR_URL_RE.match(pr_url)
    if not m:
        raise ReviewError(f"GitHub の PR URL 形式ではありません: {pr_url}")

    owner, repo, number = m.group(1), m.group(2), int(m.group(3))
    if owner != config.REVIEW_ORG:
        raise ReviewError(
            f"リポジトリの Organization ({owner}) が {config.REVIEW_ORG} ではありません。"
        )
    return ParsedPR(owner=owner, repo=repo, number=number)


def _run(
    args: list[str],
    cwd: Path | None = None,
    progress: ProgressFn | None = None,
    env: dict[str, str] | None = None,
) -> str:
    """コマンドを実行し stdout を返す。失敗時は ReviewError を送出する。"""
    if progress:
        progress("$ " + " ".join(args))
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            env=env,
        )
    except FileNotFoundError:
        raise ReviewError(f"コマンドが見つかりません: {args[0]}")

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise ReviewError(
            f"コマンドが失敗しました ({' '.join(args)}):\n{detail}"
        )
    return proc.stdout.strip()


def _git_env() -> dict[str, str]:
    """GitHub へアクセスする git コマンド用の環境変数。

    トークンはこの関数を呼んだ時点で取得するため、常に有効期限に余裕があるものが
    使われる。GIT_CONFIG_* で渡した設定は submodule などの子プロセスにも継承される。
    """
    env = dict(os.environ)
    env.update({
        "REVIEW_GH_TOKEN": github_auth.get_provider().token(),
        # 既存の helper (ホストの gh 設定など) を空値で打ち消してから自前を差し込む。
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": _CREDENTIAL_KEY,
        "GIT_CONFIG_VALUE_0": "",
        "GIT_CONFIG_KEY_1": _CREDENTIAL_KEY,
        "GIT_CONFIG_VALUE_1": _CREDENTIAL_HELPER,
        # 認証に失敗したときに対話プロンプトで固まらないようにする。
        "GIT_TERMINAL_PROMPT": "0",
    })
    return env


def _codex_env() -> dict[str, str]:
    """codex には GitHub の認証情報を渡さない (レビューに必要ないため)。"""
    env = dict(os.environ)
    for name in (
        "GH_TOKEN",
        "GITHUB_TOKEN",
        "REVIEW_GH_TOKEN",
        "GITHUB_APP_PRIVATE_KEY",
        "GITHUB_APP_PRIVATE_KEY_PATH",
    ):
        env.pop(name, None)
    return env


def _codex_args(prompt: str) -> list[str]:
    """codex の起動引数を組み立てる。

    Web からの非対話実行なので、承認待ちで止まったり作業ツリーを書き換えたりしない
    設定を固定で渡す。推論サマリはレビュー本文に混ざるノイズなので抑制する。
    """
    args = [config.CODEX_BIN, "--model", config.REVIEW_MODEL]
    if config.REVIEW_REASONING_EFFORT:
        args += ["--config", f"model_reasoning_effort={config.REVIEW_REASONING_EFFORT}"]
    args += [
        "--sandbox", "read-only",
        "--ask-for-approval", "never",
        "--config", "model_reasoning_summary=none",
        "review", prompt,
    ]
    return args


def _model_label() -> str:
    """進捗表示・ログヘッダに出すモデル設定の表記。"""
    if config.REVIEW_REASONING_EFFORT:
        return f"{config.REVIEW_MODEL}, reasoning={config.REVIEW_REASONING_EFFORT}"
    return config.REVIEW_MODEL


def _check_tools() -> None:
    for name, path in (
        ("git", config.GIT_BIN),
        ("codex", config.CODEX_BIN),
    ):
        if shutil.which(path) is None:
            raise ReviewError(f"{name} がインストールされていません。")


def _next_rev(output_dir: Path, pr_number: int) -> int:
    """既存ログを見て次の rev 番号を返す (auto_review.sh の file_rev 相当)。"""
    rev = 0
    while (output_dir / f"codex_review_pr{pr_number}_out.log-{rev}").exists():
        rev += 1
    return rev


def _prepare_repo(pr: ParsedPR, progress: ProgressFn) -> Path:
    """リポジトリをクローン/更新し、作業ツリーをクリーンにして返す。"""
    config.WORK_PATH.mkdir(parents=True, exist_ok=True)
    repo_path = config.WORK_PATH / pr.repo
    repo_url = f"https://github.com/{pr.owner}/{pr.repo}.git"

    if not repo_path.exists():
        progress(f"リポジトリをクローンします: {repo_url}")
        _run(
            [config.GIT_BIN, "clone", "--recurse-submodules", repo_url, str(repo_path)],
            progress=progress,
            env=_git_env(),
        )
    else:
        progress(f"リポジトリは既に存在します: {repo_path}")

    if not (repo_path / ".git").is_dir():
        raise ReviewError(f"git リポジトリではありません: {repo_path}")

    # remote が PR のリポジトリと一致するか検証。
    remote_url = _run([config.GIT_BIN, "remote", "get-url", "origin"], cwd=repo_path)
    m = _REMOTE_RE.match(remote_url)
    if not m:
        raise ReviewError(f"未対応の remote URL 形式です: {remote_url}")
    remote_repo = f"{m.group(1)}/{m.group(2)}"
    if remote_repo != f"{pr.owner}/{pr.repo}":
        raise ReviewError(
            f"remote リポジトリ ({remote_repo}) が PR ({pr.owner}/{pr.repo}) と一致しません。"
        )

    return repo_path


def _checkout_pr(
    pr: ParsedPR, repo_path: Path, base_ref: str, progress: ProgressFn
) -> str:
    """base と PR の head を取得し、PR のコミットを checkout する。base_commit を返す。

    `gh pr checkout` の代わりに refs/pull/<n>/head を直接 fetch する。fork からの PR
    でも同じ手順で扱え、detached HEAD になるが `git diff <base>...HEAD` には支障ない。
    fetch した先は remote-tracking ref に書き込み、以降の GC で失われないようにする。
    """
    git_env = _git_env()
    base_remote_ref = f"refs/remotes/origin/{base_ref}"
    pr_remote_ref = f"refs/remotes/origin/pr/{pr.number}"

    progress(f"base ブランチ {base_ref} を取得します...")
    _run(
        [config.GIT_BIN, "fetch", "--force", "origin",
         f"refs/heads/{base_ref}:{base_remote_ref}"],
        cwd=repo_path, progress=progress, env=git_env,
    )
    base_commit = _run([config.GIT_BIN, "rev-parse", base_remote_ref], cwd=repo_path)

    progress(f"PR #{pr.number} の head を取得します...")
    _run(
        [config.GIT_BIN, "fetch", "--force", "origin",
         f"refs/pull/{pr.number}/head:{pr_remote_ref}"],
        cwd=repo_path, progress=progress, env=git_env,
    )
    pr_commit = _run([config.GIT_BIN, "rev-parse", pr_remote_ref], cwd=repo_path)

    # 前回実行の残骸があっても復帰できるよう、作業ツリーを強制的に合わせる。
    _run([config.GIT_BIN, "checkout", "--force", "--detach", pr_commit],
         cwd=repo_path, progress=progress)
    _run([config.GIT_BIN, "clean", "-ffd"], cwd=repo_path, progress=progress)
    _run(
        [config.GIT_BIN, "submodule", "update", "--init", "--recursive", "--force"],
        cwd=repo_path, progress=progress, env=git_env,
    )
    return base_commit


def _build_prompt(pull: github_api.PullRequest, base_commit: str) -> str:
    base_ref = pull.base_ref
    prompt = (
        f"Review the code changes against the target branch '{base_ref}'. "
        f"The merge target commit for this comparison is {base_commit}. "
        f"Run `git diff {base_commit}...HEAD` to inspect the changes relative to {base_ref}.\n"
        "Provide prioritized, actionable findings. Write review comments in Japanese.\n\n"
        f"Pull Request Title:\n{pull.title}\n\n"
        f"Pull Request Body:\n<PR_BODY>\n{pull.body}\n</PR_BODY>\n"
    )
    return prompt


def run_review(pr: ParsedPR, progress: ProgressFn | None = None) -> ReviewResult:
    """1 件のレビューを最後まで実行し、結果を返す。"""
    progress = progress or (lambda _msg: None)

    _check_tools()

    progress(f"PR #{pr.number} の情報を取得します...")
    pull = github_api.get_pull(github_auth.get_provider(), pr.owner, pr.repo, pr.number)

    repo_path = _prepare_repo(pr, progress)
    base_commit = _checkout_pr(pr, repo_path, pull.base_ref, progress)

    progress("PR のタイトル・本文からプロンプトを組み立てます...")
    prompt = _build_prompt(pull, base_commit)

    output_dir = config.LOGS_PATH / pr.repo
    output_dir.mkdir(parents=True, exist_ok=True)
    rev = _next_rev(output_dir, pr.number)
    stdout_path = output_dir / f"codex_review_pr{pr.number}_out.log-{rev}"
    stderr_path = output_dir / f"codex_review_pr{pr.number}_err.log-{rev}"

    label = _model_label()
    progress(f"codex レビューを実行します ({label}, rev-{rev})...")
    header = f"codex review ({label}, rev-{rev}):\n\n"
    with open(stdout_path, "w", encoding="utf-8") as out_f, \
            open(stderr_path, "w", encoding="utf-8") as err_f:
        out_f.write(header)
        out_f.flush()
        try:
            proc = subprocess.run(
                _codex_args(prompt),
                cwd=str(repo_path),
                stdout=out_f,
                stderr=err_f,
                text=True,
                env=_codex_env(),
            )
        except FileNotFoundError:
            raise ReviewError(f"コマンドが見つかりません: {config.CODEX_BIN}")

    review_text = stdout_path.read_text(encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        err_text = stderr_path.read_text(encoding="utf-8", errors="replace").strip()
        raise ReviewError(
            f"codex レビューが失敗しました (exit={proc.returncode}):\n{err_text[-2000:]}"
        )

    progress("codex レビューが完了しました。")
    return ReviewResult(
        base_ref=pull.base_ref,
        base_commit=base_commit,
        pr_title=pull.title,
        rev=rev,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        review_text=review_text,
    )


def post_review(pr: ParsedPR, stdout_path: Path, progress: ProgressFn | None = None) -> None:
    """レビュー結果を PR にコメントとして投稿する。

    REST API でリポジトリを明示して投稿するため、作業ディレクトリの git 状態には
    依存しない (別のレビューが実行中でも安全に投稿できる)。レビュー完了から時間が
    経ってから呼ばれることもあるが、トークンはこの時点で取り直される。
    """
    progress = progress or (lambda _msg: None)
    path = Path(stdout_path)
    if not path.exists():
        raise ReviewError(f"レビューログが見つかりません: {stdout_path}")

    progress(f"PR #{pr.number} にレビューを投稿します...")
    github_api.create_review_comment(
        github_auth.get_provider(),
        pr.owner,
        pr.repo,
        pr.number,
        path.read_text(encoding="utf-8", errors="replace"),
    )
    progress("投稿が完了しました。")
