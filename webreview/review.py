"""auto_review.sh を非対話的に実行するためのモジュール。

Web からの呼び出しで人手の確認 (y/N) を挟めないため、元スクリプトの対話部分は
次のように置き換える:

* ログの世代 (rev) が既に存在する場合 → 確認せず新しい rev を自動採番する。
* codex レビューの実行確認 → 常に実行する (ジョブ投入=実行の意思とみなす)。
* PR への投稿確認 → Web UI 上の「投稿」操作で明示的に行う (post_review)。

git の作業ツリーは毎回 base ブランチへ reset --hard + clean するため、前回実行の
残骸があっても非対話で復帰できる。単一ワーカーで直列実行される前提なので、共有の
作業ディレクトリに対する git 操作が競合することはない。
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import config

ProgressFn = Callable[[str], None]

_PR_URL_RE = re.compile(
    r"^https://github\.com/([^/]+)/([^/]+)/pull/([0-9]+)(?:/.*)?$"
)
_REMOTE_RE = re.compile(
    r"^(?:git@github\.com:|https://github\.com/)([^/]+)/([^/.]+)(?:\.git)?$"
)


class ReviewError(Exception):
    """ユーザーに提示する想定のエラー。message はそのまま画面に表示される。"""


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
        )
    except FileNotFoundError:
        raise ReviewError(f"コマンドが見つかりません: {args[0]}")

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise ReviewError(
            f"コマンドが失敗しました ({' '.join(args)}):\n{detail}"
        )
    return proc.stdout.strip()


def _check_tools() -> None:
    for name, path in (
        ("GitHub CLI (gh)", config.GH_BIN),
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


def _checkout_pr(pr: ParsedPR, repo_path: Path, progress: ProgressFn) -> tuple[str, str]:
    """base ブランチを最新化し PR ブランチを checkout する。(base_ref, base_commit)。"""
    base_ref = _run(
        [config.GH_BIN, "pr", "view", str(pr.number), "--json", "baseRefName",
         "--jq", ".baseRefName"],
        cwd=repo_path,
        progress=progress,
    )
    if not base_ref:
        raise ReviewError("PR の base ブランチを取得できませんでした。")

    progress(f"base ブランチ {base_ref} を取得・更新します...")
    _run([config.GIT_BIN, "fetch", "origin", base_ref], cwd=repo_path, progress=progress)
    _run([config.GIT_BIN, "switch", base_ref], cwd=repo_path, progress=progress)
    # 前回実行の残骸があっても復帰できるよう作業ツリーをクリーンにする。
    _run([config.GIT_BIN, "reset", "--hard", f"origin/{base_ref}"], cwd=repo_path,
         progress=progress)
    _run([config.GIT_BIN, "clean", "-fd"], cwd=repo_path, progress=progress)
    base_commit = _run([config.GIT_BIN, "rev-parse", "HEAD"], cwd=repo_path)

    progress(f"PR #{pr.number} のブランチを checkout します...")
    _run(
        [config.GH_BIN, "pr", "checkout", str(pr.number), "--force",
         "--recurse-submodules"],
        cwd=repo_path,
        progress=progress,
    )
    return base_ref, base_commit


def _build_prompt(
    pr: ParsedPR, repo_path: Path, base_ref: str, base_commit: str
) -> tuple[str, str]:
    pr_title = _run(
        [config.GH_BIN, "pr", "view", str(pr.number), "--json", "title", "--jq", ".title"],
        cwd=repo_path,
    )
    pr_body = _run(
        [config.GH_BIN, "pr", "view", str(pr.number), "--json", "body", "--jq", ".body"],
        cwd=repo_path,
    )
    prompt = (
        f"Review the code changes against the target branch '{base_ref}'. "
        f"The merge target commit for this comparison is {base_commit}. "
        f"Run `git diff {base_commit}...HEAD` to inspect the changes relative to {base_ref}.\n"
        "Provide prioritized, actionable findings. Write review comments in Japanese.\n\n"
        f"Pull Request Title:\n{pr_title}\n\n"
        f"Pull Request Body:\n<PR_BODY>\n{pr_body}\n</PR_BODY>\n"
    )
    return prompt, pr_title


def run_review(pr: ParsedPR, progress: ProgressFn | None = None) -> ReviewResult:
    """1 件のレビューを最後まで実行し、結果を返す。"""
    progress = progress or (lambda _msg: None)

    _check_tools()
    repo_path = _prepare_repo(pr, progress)
    base_ref, base_commit = _checkout_pr(pr, repo_path, progress)

    progress("PR のタイトル・本文からプロンプトを組み立てます...")
    prompt, pr_title = _build_prompt(pr, repo_path, base_ref, base_commit)

    output_dir = config.LOGS_PATH / pr.repo
    output_dir.mkdir(parents=True, exist_ok=True)
    rev = _next_rev(output_dir, pr.number)
    stdout_path = output_dir / f"codex_review_pr{pr.number}_out.log-{rev}"
    stderr_path = output_dir / f"codex_review_pr{pr.number}_err.log-{rev}"

    progress(f"codex レビューを実行します ({config.REVIEW_MODEL}, rev-{rev})...")
    header = f"codex review ({config.REVIEW_MODEL}, rev-{rev}):\n\n"
    with open(stdout_path, "w", encoding="utf-8") as out_f, \
            open(stderr_path, "w", encoding="utf-8") as err_f:
        out_f.write(header)
        out_f.flush()
        try:
            proc = subprocess.run(
                [config.CODEX_BIN, "--model", config.REVIEW_MODEL, "review", prompt],
                cwd=str(repo_path),
                stdout=out_f,
                stderr=err_f,
                text=True,
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
        base_ref=base_ref,
        base_commit=base_commit,
        pr_title=pr_title,
        rev=rev,
        stdout_path=stdout_path,
        stderr_path=stderr_path,
        review_text=review_text,
    )


def post_review(pr: ParsedPR, stdout_path: Path, progress: ProgressFn | None = None) -> None:
    """レビュー結果を PR にコメントとして投稿する (gh pr review --comment)。

    -R でリポジトリを明示するため、作業ディレクトリの git 状態には依存しない
    (別のレビューが実行中でも安全に投稿できる)。
    """
    progress = progress or (lambda _msg: None)
    if not Path(stdout_path).exists():
        raise ReviewError(f"レビューログが見つかりません: {stdout_path}")

    progress(f"PR #{pr.number} にレビューを投稿します...")
    _run(
        [config.GH_BIN, "pr", "review", str(pr.number), "--comment",
         "-R", f"{pr.owner}/{pr.repo}",
         "--body-file", str(stdout_path)],
        progress=progress,
    )
    progress("投稿が完了しました。")
