"""レビューの実行と投稿。codex と git を駆動する。

ブラウザからの操作を待たせないため、途中で確認を挟まず最後まで進める:

* ログの世代 (rev) が既に存在する場合は、新しい rev を自動採番する。
* レビューはジョブ投入をもって実行する (投入=実行の意思とみなす)。
* PR への投稿は Web UI 上の「投稿」操作で明示的に行う (post_review)。
* 行コメントを GitHub が解決できなくても、本文だけの投稿に落として先へ進む。

git の作業ツリーは毎回 PR のコミットへ checkout --force + clean するため、前回実行の
残骸があっても復帰できる。単一ワーカーで直列実行される前提なので、共有の
作業ディレクトリに対する git 操作が競合することはない。

PR をホストするサービスへのアクセスは、REST API と git コマンドの 2 経路。どちらも
forge 層を通し、トークンは実行の直前に取得してその 1 回の呼び出しにだけ渡す。
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

from . import config, forge, reviews
from .errors import ReviewError

from .forge import ProgressFn, PullRequest

__all__ = [
    "ReviewError",
    "ParsedPR",
    "parse_pr_url",
    "run_review",
    "post_review",
]

# git の認証は「実行時の環境変数から認証情報を読む credential helper」で行う。
# こうするとイメージやホストに認証設定を残さず、submodule の子プロセスにも継承される。
_CREDENTIAL_HELPER = (
    '!f() { if test "$1" = get; then echo "username=$REVIEW_GIT_USERNAME"; '
    'echo "password=$REVIEW_GIT_TOKEN"; fi; }; f'
)


@dataclass
class ParsedPR:
    owner: str
    repo: str
    number: int

    @property
    def url(self) -> str:
        return forge.get().pr_url(self.owner, self.repo, self.number)


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
    """PR URL を owner/repo/number に分解し、オーナーの制約を検証する。"""
    pr_url = (pr_url or "").strip()
    if not pr_url:
        raise ReviewError("PR の URL を入力してください。")

    parsed = forge.get().parse_pr_url(pr_url)
    if parsed is None:
        raise ReviewError(f"GitHub の PR URL 形式ではありません: {pr_url}")

    owner, repo, number = parsed
    # オーナーとリポジトリ名はそのまま作業ディレクトリのパスになる。
    if {owner, repo} & {".", ".."}:
        raise ReviewError(f"GitHub の PR URL 形式ではありません: {pr_url}")
    if not config.REVIEW_OWNERS:
        raise ReviewError(
            "REVIEW_OWNER が設定されていません。管理者に連絡してください。"
        )
    if owner not in config.REVIEW_OWNERS:
        raise ReviewError(
            f"リポジトリのオーナー ({owner}) が "
            f"{', '.join(config.REVIEW_OWNERS)} のいずれでもありません。"
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


def _git_env(pr: ParsedPR) -> dict[str, str]:
    """PR のリポジトリへアクセスする git コマンド用の環境変数。

    トークンはこの関数を呼んだ時点で取得するため、常に有効期限に余裕があるものが
    使われる。GIT_CONFIG_* で渡した設定は submodule などの子プロセスにも継承される。
    """
    credential = forge.get().git_credential(pr.owner)
    key = f"credential.{credential.url}.helper"
    env = dict(os.environ)
    env.update({
        "REVIEW_GIT_USERNAME": credential.username,
        "REVIEW_GIT_TOKEN": credential.password,
        # 既存の helper (ホストの gh 設定など) を空値で打ち消してから自前を差し込む。
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": key,
        "GIT_CONFIG_VALUE_0": "",
        "GIT_CONFIG_KEY_1": key,
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
        "REVIEW_GIT_TOKEN",
        # 廃止済みだが、古い設定で環境に残っていても codex には渡さない。
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
    repo_path = config.repo_work_path(pr.owner, pr.repo)
    repo_path.parent.mkdir(parents=True, exist_ok=True)
    repo_url = forge.get().clone_url(pr.owner, pr.repo)

    if not repo_path.exists():
        progress(f"リポジトリを初期化します: {repo_url}")
        # レビュー実行時に必要なコミットだけ fetch するので、clone ではなく
        # init + remote add で足りる。途中で失敗したら消しておく — 中途半端に
        # 残すと、次回以降は「既にある」と見なされて origin の無いディレクトリを
        # 使い続けてしまい、手で消すまで復帰できない。
        try:
            _run([config.GIT_BIN, "init", str(repo_path)], progress=progress)
            _run([config.GIT_BIN, "remote", "add", "origin", repo_url], cwd=repo_path)
        except BaseException:
            shutil.rmtree(repo_path, ignore_errors=True)
            raise
    else:
        progress(f"リポジトリは既に存在します: {repo_path}")

    if not (repo_path / ".git").is_dir():
        raise ReviewError(f"git リポジトリではありません: {repo_path}")

    # remote が PR のリポジトリと一致するか検証。
    remote_url = _run([config.GIT_BIN, "remote", "get-url", "origin"], cwd=repo_path)
    parsed = forge.get().parse_remote_url(remote_url)
    if parsed is None:
        raise ReviewError(f"未対応の remote URL 形式です: {remote_url}")
    remote_repo = "/".join(parsed)
    if remote_repo != f"{pr.owner}/{pr.repo}":
        raise ReviewError(
            f"remote リポジトリ ({remote_repo}) が PR ({pr.owner}/{pr.repo}) と一致しません。"
        )

    return repo_path


def _checkout_pr(
    pr: ParsedPR, repo_path: Path, pull: PullRequest, progress: ProgressFn
) -> None:
    """PR の base と head のコミットを取得し、head を checkout する。

    どちらのコミットも API が返した SHA をそのまま使い、ブランチ名から引き直さない。
    base をブランチの先端から取ると、PR がマージされたあとは先端が PR 自身を含んで
    しまい、`git diff <base>...HEAD` の merge-base が head 側に一致して差分が空になる。
    head も API の SHA に固定することで、PR の情報 (タイトル・本文・base) を取ってから
    fetch するまでの間に push があっても、レビュー対象が一意に決まる。

    GitHub は SHA 指定の fetch に対応しているので、fork からの PR でも、head の
    ブランチが消えたあとでも同じ手順で取得できる。SHA 指定の fetch は既定では ref を
    作らず、オブジェクトが GC の対象になりうる。refspec で直接 ref に受けることで、
    取得した時点から codex が `git diff` を終えるまで確実に残す。
    detached HEAD になるが `git diff <base>...HEAD` には支障ない。
    """
    git_env = _git_env(pr)

    progress(f"PR #{pr.number} の base と head のコミットを取得します...")
    _run(
        [config.GIT_BIN, "fetch", "origin",
         f"+{pull.base_sha}:refs/review/{pr.number}/base",
         f"+{pull.head_sha}:refs/review/{pr.number}/head"],
        cwd=repo_path, progress=progress, env=git_env,
    )

    # 前回実行の残骸があっても復帰できるよう、作業ツリーを強制的に合わせる。
    _run([config.GIT_BIN, "checkout", "--force", "--detach", pull.head_sha],
         cwd=repo_path, progress=progress)
    _run([config.GIT_BIN, "clean", "-ffd"], cwd=repo_path, progress=progress)
    _run(
        [config.GIT_BIN, "submodule", "update", "--init", "--recursive", "--force"],
        cwd=repo_path, progress=progress, env=git_env,
    )


# codex の既定のレビュー指針は「一般的なコード品質の指摘」を明示的に除外している。
# 品質改善を含めるレビューでは、その除外だけを打ち消し、他の条件 (この変更で入った
# ものに限る・既存コードの厳密さに合わせる) は残す。ファイルと行範囲を必ず付けさせる
# のは、findings が行コメントに変換できる書式に揃えるため。
_QUALITY_PROMPT = """\
For this review, do not limit findings to correctness bugs. This instruction \
overrides the default review guidelines where they exclude general code quality \
concerns and non-blocking issues.

Also report discrete, actionable improvements that the author would likely make \
before merging, including:

- readability and clarity problems
- unnecessary complexity
- misleading or weak naming
- poor separation of responsibilities
- duplication introduced by the change
- maintainability problems
- API or interface design problems
- brittle or unnecessarily implementation-coupled tests
- missing tests for important behavior
- code that is substantially harder to understand than a simpler equivalent

Treat these as valid findings even when the current behavior is technically correct.

Every other part of the review guidelines still applies: report only issues \
introduced by this change, not pre-existing ones; match the rigor level of the \
existing codebase; and anchor each finding to a specific file and line range, as \
with any other finding. Do not report purely subjective formatting preferences or \
trivial style nits. Only report an improvement when it is concrete, localized, \
actionable, and would materially improve the changed code.

Use [P3] for non-blocking quality improvements unless their impact warrants a \
higher priority. Quality findings must not by themselves make the overall \
correctness verdict negative.
"""


def _build_prompt(pull: PullRequest, include_quality: bool = False) -> str:
    base_ref = pull.base_ref
    # base ブランチの先端ではなく PR の base コミットと比べる。三点ドットなので、
    # そこから枝分かれした地点 (merge-base) からの差分になる。
    base_commit = pull.base_sha
    # 指示はすべて先に置き、PR のタイトル・本文 (作者が書いた文章) は末尾に回す。
    instructions = (
        f"Review the code changes against the target branch '{base_ref}'. "
        f"The merge target commit for this comparison is {base_commit}. "
        f"Run `git diff {base_commit}...HEAD` to inspect the changes relative to {base_ref}.\n"
        "Provide prioritized, actionable findings. Write review comments in Japanese.\n"
    )
    if include_quality:
        instructions += "\n" + _QUALITY_PROMPT
    return (
        f"{instructions}\n"
        f"Pull Request Title:\n{pull.title}\n\n"
        f"Pull Request Body:\n<PR_BODY>\n{pull.body}\n</PR_BODY>\n"
    )


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


def run_review(
    pr: ParsedPR,
    progress: ProgressFn | None = None,
    include_quality: bool = False,
) -> reviews.Review:
    """1 件のレビューを最後まで実行し、DB に登録したレビューを返す。

    include_quality を立てると、バグ以外に書き方・品質の改善もレビュー対象に含める
    (プロンプトの違いは _QUALITY_PROMPT を参照)。どちらで実行したかはレビューに
    記録するので、あとから集計で見分けられる。
    """
    progress = progress or (lambda _msg: None)

    _check_tools()

    progress(f"PR #{pr.number} の情報を取得します...")
    pull = forge.get().get_pull(pr.owner, pr.repo, pr.number)
    reviews.put_title(pr.owner, pr.repo, pr.number, pull.title)

    repo_path = _prepare_repo(pr, progress)
    _checkout_pr(pr, repo_path, pull, progress)

    progress("PR のタイトル・本文からプロンプトを組み立てます...")
    if include_quality:
        progress("バグに加えて、書き方・品質の改善も指摘対象に含めます。")
    prompt = _build_prompt(pull, include_quality)

    output_dir = config.repo_logs_path(pr.owner, pr.repo)
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
        owner=pr.owner,
        repo=pr.repo,
        pr_number=pr.number,
        body=run.body,
        base_ref=pull.base_ref,
        base_commit=pull.base_sha,
        head_commit=pull.head_sha,
        model=run.model,
        reasoning_effort=run.reasoning_effort,
        codex_version=run.codex_version,
        session_id=run.session_id,
        details=run.details,
        include_quality=include_quality,
        pr_author=pull.author,
        pr_additions=pull.additions,
        pr_deletions=pull.deletions,
        pr_changed_files=pull.changed_files,
    )
    reviews.set_stderr_path(review.id, _finalize_err_log(tmp_path, pr, review.rev))

    progress(f"codex レビューが完了しました (rev-{review.rev})。")
    return review


def post_review(
    pr: ParsedPR, review_id: int, progress: ProgressFn | None = None
) -> None:
    """レビュー結果を PR に投稿する。

    投稿の形 (行コメントにするか、位置を解決できなかったときにどう投稿し直すか)
    は forge が決める。どの経路でも指摘の内容は PR に残る。

    REST API でリポジトリを明示して投稿するため、作業ディレクトリの git 状態には
    依存しない (別のレビューが実行中でも安全に投稿できる)。レビュー完了から時間が
    経ってから呼ばれることもあるが、トークンはこの時点で取り直される。
    """
    progress = progress or (lambda _msg: None)
    review = reviews.get(review_id)
    if review is None:
        raise ReviewError(f"レビューが見つかりません: id={review_id}")

    # オーナーを 1 つに固定していた頃のレビューは、その頃の作業ツリーのパスで出ている。
    roots = (
        config.repo_work_path(pr.owner, pr.repo),
        config.legacy_repo_work_path(pr.repo),
    )
    forge.get().post_review(pr.owner, pr.repo, pr.number, review, roots, progress)
    progress("投稿が完了しました。")
