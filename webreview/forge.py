"""PR をホストするサービス (forge) ごとの違いを受け持つ層。

レビューの実行 (review) と集計 (reviews / export) は、PR の URL の解釈、clone 元、
git の認証、PR の情報の取得、投稿をすべてこの層を通して行い、サービス固有の
API や URL の形を直接は知らない。

使う forge はプロセス全体で 1 つに決まる (1 つのインスタンスで複数のサービスを
混ぜて扱わない)。DB はサービスを区別する列を持たないため。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Protocol

if TYPE_CHECKING:
    from .findings import Roots
    from .reviews import Review

ProgressFn = Callable[[str], None]


@dataclass
class PullRequest:
    base_ref: str
    base_sha: str
    head_sha: str
    title: str
    body: str
    # 集計用。レビューの実行には使わないので、取れなくても止めない。
    author: str = ""
    additions: int | None = None
    deletions: int | None = None
    changed_files: int | None = None


@dataclass(frozen=True)
class GitCredential:
    """git の credential helper に渡す認証情報。

    url は helper を効かせる範囲 (スキームとホスト)。git はこれに前方一致する
    URL へのアクセスにだけ helper を使う。
    """

    url: str
    username: str
    password: str


class Forge(Protocol):
    def parse_pr_url(self, url: str) -> tuple[str, str, int] | None:
        """PR の URL を (owner, repo, number) に分解する。形式が違えば None。"""
        ...

    def pr_url(self, owner: str, repo: str, number: int) -> str:
        """画面・CSV に出す PR の URL。"""
        ...

    def pr_url_pattern(self) -> str:
        """入力欄の例に出す PR の URL の形 (例: https://github.com/<owner>/<repo>/pull/<番号>)。"""
        ...

    def clone_url(self, owner: str, repo: str) -> str:
        """作業ディレクトリの origin にする URL。"""
        ...

    def parse_remote_url(self, url: str) -> tuple[str, str] | None:
        """origin の URL を (owner, repo) に分解する。形式が違えば None。"""
        ...

    def git_credential(self, owner: str) -> GitCredential:
        """git で fetch するための認証情報。呼んだ時点で有効なトークンを返す。"""
        ...

    def get_pull(self, owner: str, repo: str, number: int) -> PullRequest:
        """PR の情報を取得する。レビュー対象のコミットはここで返す SHA で確定する。"""
        ...

    def post_review(
        self,
        owner: str,
        repo: str,
        number: int,
        review: Review,
        roots: Roots,
        progress: ProgressFn,
    ) -> None:
        """レビューを PR に投稿する。

        行コメントに対応するかどうか、対応しても位置を解決できなかったときに
        どう投稿し直すかは forge ごとに決める。どの経路でも指摘の内容は PR に残す。
        roots は codex が動いた作業ツリーの候補で、指摘のパスを相対に直すのに使う。
        """
        ...


_FORGE: Forge | None = None
_FORGE_LOCK = threading.Lock()


def get() -> Forge:
    """プロセス全体で 1 つの forge を返す (初回のみ構築)。"""
    global _FORGE
    with _FORGE_LOCK:
        if _FORGE is None:
            # 実装側が github_api を import し、github_api はこのモジュールの
            # PullRequest を使うので、循環しないよう呼ばれた時点で読む。
            from .github_forge import GitHubForge

            _FORGE = GitHubForge()
        return _FORGE
