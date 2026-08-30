"""レビューの実行と投稿。codex と git を駆動する。

ブラウザからの操作を待たせないため、途中で確認を挟まず最後まで進める:

* ログの世代 (rev) が既に存在する場合は、新しい rev を自動採番する。
* レビューはジョブ投入をもって実行する (投入=実行の意思とみなす)。
* PR への投稿は Web UI 上の「投稿」操作で明示的に行う (post_review)。
* 行コメントを GitHub が解決できなくても、本文だけの投稿に落として先へ進む。

git の作業ツリーは毎回 PR のコミットへ checkout --force + clean するため、前回実行の
残骸があっても復帰できる。単一ワーカーで直列実行される前提なので、共有の
作業ディレクトリに対する git 操作が競合することはない。

GitHub へのアクセスは REST API (github_api) と git コマンドの 2 経路。どちらも
トークンは実行の直前に github_auth から取得し、その 1 回の呼び出しにだけ渡す。
git にはコマンドラインではなく環境変数経由の credential helper で渡すので、
トークンが ps や進捗ログに現れることはない。

codex の 2 つの出力は性質が違うので扱いを分ける:

* stdout はレビュー本文で、散文なので分量が頭打ちになる。パイプで受けて DB に入れる。
* stderr は全トランスクリプト (実行した各コマンドとその出力を含む) で、PR の差分規模に
  比例して伸びる。ファイルに流したまま gzip し、期限で削除する。アプリはこのファイルを
  読み返さない — 必要な先頭の実行条件ブロックだけ実行直後に取り出して DB に保存する。
"""

from __future__ import annotations

import gzip
import os
import re
import shutil
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import config, findings, github_api, github_auth, reviews
from .errors import ReviewError

ProgressFn = Callable[[str], None]

__all__ = [
    "ReviewError",
    "ParsedPR",
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
class CodexRun:
    """codex の 1 回の実行から取り出した、DB に残す情報。"""
    body: str
    details: str
    codex_version: str | None
    model: str | None
    reasoning_effort: str | None
    session_id: str | None


def parse_pr_url(pr_url: str) -> ParsedPR:
    """PR URL を owner/repo/number に分解し、Organization 制約を検証する。"""
    pr_url = (pr_url or "").strip()
    if not pr_url:
        raise ReviewError("PR の URL を入力してください。")

    m = _PR_URL_RE.match(pr_url)
    if not m:
        raise ReviewError(f"GitHub の PR URL 形式ではありません: {pr_url}")

    owner, repo, number = m.group(1), m.group(2), int(m.group(3))
    if not config.REVIEW_ORG:
        raise ReviewError(
            "REVIEW_ORG が設定されていません。管理者に連絡してください。"
        )
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


# err ログ先頭のバナーから拾う項目。codex の版によって欠けることがあるので、
# どれも「あれば使う」扱いにする。
_BANNER_VERSION_RE = re.compile(r"^OpenAI Codex v(\S+)")
_BANNER_FIELDS = {
    "model": "model",
    "reasoning effort": "reasoning_effort",
    "session id": "session_id",
}


def read_err_header(err_path: Path, max_lines: int = 200) -> tuple[str, dict[str, str]]:
    """err ログの先頭 (最初の 'exec' 行の手前まで) と、そこから拾った項目を返す。

    err ログ全体は PR の差分規模しだいで大きくなるため、必ず逐次読みして打ち切る。
    max_lines は 'exec' が現れない異常なログで読み進みすぎないための保険。
    """
    lines: list[str] = []
    fields: dict[str, str] = {}
    try:
        with err_path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip("\n")
                if line == "exec":
                    break
                if len(lines) >= max_lines:
                    break
                lines.append(line)

                m = _BANNER_VERSION_RE.match(line)
                if m:
                    fields["codex_version"] = m.group(1)
                    continue
                key, sep, value = line.partition(":")
                if sep and key in _BANNER_FIELDS:
                    fields[_BANNER_FIELDS[key]] = value.strip()
    except OSError:
        return "", {}
    return "\n".join(lines), fields


def _tail(path: Path, limit: int = 2000) -> str:
    """ファイル末尾を limit 文字ぶん読む (失敗時の抜粋用)。

    err ログは大きくなりうるので全文は読まない。
    """
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            # UTF-8 は 1 文字最大 4 バイト。余裕を見て読み、末尾を切り出す。
            f.seek(max(0, size - limit * 4))
            data = f.read()
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")[-limit:].strip()


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
) -> tuple[str, str]:
    """base と PR の head を取得し、PR のコミットを checkout する。

    (base_commit, head_commit) を返す。head_commit はレビューしたコミットそのもので、
    投稿時に行コメントの commit_id として使う (その後 PR が更新されても、レビューが
    どの版に対するものかがコメントに残る)。

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
    return base_commit, pr_commit


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


def _run_codex(prompt: str, repo_path: Path, err_path: Path) -> CodexRun:
    """codex を実行し、stdout を本文として返す。stderr は err_path に流す。

    タイムアウト時はプロセスグループごと終了させる。codex は bubblewrap 経由で
    子プロセスを起こすため、親だけ kill すると stdout のパイプが閉じずに待ち続ける。
    """
    timeout = config.REVIEW_TIMEOUT or None

    with err_path.open("wb") as err_f:
        try:
            proc = subprocess.Popen(
                _codex_args(prompt),
                cwd=str(repo_path),
                stdout=subprocess.PIPE,
                stderr=err_f,
                env=_codex_env(),
                # 自身をプロセスグループのリーダーにして、まとめて終了できるようにする。
                start_new_session=True,
            )
        except FileNotFoundError:
            raise ReviewError(f"コマンドが見つかりません: {config.CODEX_BIN}")

        try:
            out, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()
            proc.communicate()
            raise ReviewError(
                f"codex レビューが制限時間 ({timeout} 秒) を超えたため中断しました。"
                "\nREVIEW_TIMEOUT で調整できます。"
            )

    body = out.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        raise ReviewError(
            f"codex レビューが失敗しました (exit={proc.returncode}):\n"
            f"{_tail(err_path)}"
        )

    details, fields = read_err_header(err_path)
    return CodexRun(
        body=body.strip(),
        details=details,
        codex_version=fields.get("codex_version"),
        # 実際に使われた設定を err ログのバナーから取る。こちらが渡した値と
        # 食い違った場合 (codex 側の既定など) も実態の方を残せる。
        model=fields.get("model") or config.REVIEW_MODEL,
        reasoning_effort=fields.get("reasoning_effort")
        or config.REVIEW_REASONING_EFFORT
        or None,
        session_id=fields.get("session_id"),
    )


def _finalize_err_log(tmp_path: Path, pr: ParsedPR, rev: int) -> Path:
    """一時ファイルの err ログを最終名に移し、gzip する。

    rev はレビューを DB に登録して初めて決まるので、実行中は一時名で書いておく。
    """
    final = tmp_path.with_name(f"codex_review_pr{pr.number}_err.log-{rev}.gz")
    try:
        with tmp_path.open("rb") as src, gzip.open(final, "wb") as dst:
            shutil.copyfileobj(src, dst)
        tmp_path.unlink(missing_ok=True)
    except OSError:
        # 生ログの保存に失敗してもレビュー自体は成立している。
        return tmp_path
    return final


def run_review(pr: ParsedPR, progress: ProgressFn | None = None) -> reviews.Review:
    """1 件のレビューを最後まで実行し、DB に登録したレビューを返す。"""
    progress = progress or (lambda _msg: None)

    _check_tools()

    progress(f"PR #{pr.number} の情報を取得します...")
    pull = github_api.get_pull(github_auth.get_provider(), pr.owner, pr.repo, pr.number)
    reviews.put_title(pr.repo, pr.number, pull.title)

    repo_path = _prepare_repo(pr, progress)
    base_commit, head_commit = _checkout_pr(pr, repo_path, pull.base_ref, progress)

    progress("PR のタイトル・本文からプロンプトを組み立てます...")
    prompt = _build_prompt(pull, base_commit)

    output_dir = config.LOGS_PATH / pr.repo
    output_dir.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=output_dir, prefix=f".codex_review_pr{pr.number}_err.", suffix=".tmp"
    )
    os.close(fd)
    tmp_path = Path(tmp_name)

    progress(f"codex レビューを実行します ({_model_label()})...")
    try:
        run = _run_codex(prompt, repo_path, tmp_path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    review = reviews.create(
        repo=pr.repo,
        pr_number=pr.number,
        body=run.body,
        base_ref=pull.base_ref,
        base_commit=base_commit,
        head_commit=head_commit,
        model=run.model,
        reasoning_effort=run.reasoning_effort,
        codex_version=run.codex_version,
        session_id=run.session_id,
        details=run.details,
    )
    reviews.set_stderr_path(review.id, _finalize_err_log(tmp_path, pr, review.rev))

    progress(f"codex レビューが完了しました (rev-{review.rev})。")
    return review


def post_review(
    pr: ParsedPR, review_id: int, progress: ProgressFn | None = None
) -> None:
    """レビュー結果を PR にコメントとして投稿する。

    位置の読み取れた指摘はソース行へのコメントにし、残り (全体の要約や書式の
    読めなかったぶん) をレビュー本体の本文にする。GitHub が位置を解決できないと
    レビューごと 422 で弾かれるため、そのときは本文にまとめて投稿し直す。
    指摘の内容はどちらの経路でも PR に残る。

    REST API でリポジトリを明示して投稿するため、作業ディレクトリの git 状態には
    依存しない (別のレビューが実行中でも安全に投稿できる)。レビュー完了から時間が
    経ってから呼ばれることもあるが、トークンはこの時点で取り直される。
    """
    progress = progress or (lambda _msg: None)
    review = reviews.get(review_id)
    if review is None:
        raise ReviewError(f"レビューが見つかりません: id={review_id}")

    summary, found = findings.extract(review.body, config.WORK_PATH / pr.repo)
    comments = [f.comment() for f in found]

    if comments:
        progress(
            f"PR #{pr.number} にレビューを投稿します "
            f"({len(comments)} 件をソース行へのコメントにします)..."
        )
        posted = _post_with_comments(
            pr,
            f"{reviews.header_line(review)}\n\n{summary}".rstrip(),
            comments,
            review.head_commit,
        )
        if posted:
            progress("投稿が完了しました。")
            return
        progress("行コメントを解決できなかったため、本文にまとめて投稿し直します。")
    else:
        progress(f"PR #{pr.number} にレビューを投稿します...")

    # 本文だけの投稿では commit_id も渡さない。レビュー後に PR が更新されていると
    # 「PR に属さない commit_id」自体が 422 の原因になりうるため。
    github_api.create_review(
        github_auth.get_provider(),
        pr.owner,
        pr.repo,
        pr.number,
        reviews.render_body(review),
    )
    progress("投稿が完了しました。")


def _post_with_comments(
    pr: ParsedPR, body: str, comments: list[dict], commit_id: str | None
) -> bool:
    """行コメント付きで投稿する。GitHub が位置を解決できなければ False を返す。

    422 は差分に無いパスや行を指したときのもので (エラーは "Path could not be
    resolved" など)、投稿はまるごと行われていない。本文だけならまだ通るので、
    呼び出し元で投稿し直す。それ以外の失敗は投稿の失敗として伝える。
    """
    try:
        github_api.create_review(
            github_auth.get_provider(),
            pr.owner,
            pr.repo,
            pr.number,
            body,
            comments=comments,
            commit_id=commit_id,
        )
    except github_api.ApiError as e:
        if e.status != 422:
            raise
        return False
    return True
