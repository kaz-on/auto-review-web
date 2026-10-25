"""GitHub (github.com) の forge 実装。

REST API は github_api、トークンは github_auth が受け持ち、ここでは URL の形と
投稿の手順を決める。どちらのモジュールも呼び出しの時点で属性を引くので、
テストで github_api / github_auth の関数を差し替えればそのまま効く。
"""

from __future__ import annotations

import re
from pathlib import Path
from . import findings, github_api, github_auth, reviews
from .forge import GitCredential, ProgressFn, PullRequest
from .reviews import Review

_WEB_URL = "https://github.com"

_PR_URL_RE = re.compile(
    r"^https://github\.com/([^/]+)/([^/]+)/pull/([0-9]+)(?:/.*)?$"
)
_REMOTE_RE = re.compile(
    r"^(?:git@github\.com:|https://github\.com/)([^/]+)/([^/.]+)(?:\.git)?$"
)


class GitHubForge:
    def parse_pr_url(self, url: str) -> tuple[str, str, int] | None:
        m = _PR_URL_RE.match(url)
        if not m:
            return None
        return m.group(1), m.group(2), int(m.group(3))

    def pr_url(self, owner: str, repo: str, number: int) -> str:
        return f"{_WEB_URL}/{owner}/{repo}/pull/{number}"

    def clone_url(self, owner: str, repo: str) -> str:
        return f"{_WEB_URL}/{owner}/{repo}.git"

    def parse_remote_url(self, url: str) -> tuple[str, str] | None:
        m = _REMOTE_RE.match(url)
        if not m:
            return None
        return m.group(1), m.group(2)

    def git_credential(self, owner: str) -> GitCredential:
        # installation access token でも PAT でも、ユーザー名はこの固定値で通る。
        return GitCredential(
            url=_WEB_URL,
            username="x-access-token",
            password=github_auth.get_provider().token(),
        )

    def get_pull(self, owner: str, repo: str, number: int) -> PullRequest:
        return github_api.get_pull(github_auth.get_provider(), owner, repo, number)

    def post_review(
        self,
        owner: str,
        repo: str,
        number: int,
        review: Review,
        repo_path: Path,
        progress: ProgressFn,
    ) -> None:
        """位置の読み取れた指摘はソース行へのコメントにし、残り (全体の要約や書式の
        読めなかったぶん) をレビュー本体の本文にする。GitHub が位置を解決できないと
        レビューごと 422 で弾かれるため、そのときは本文にまとめて投稿し直す。
        """
        summary, found = findings.extract(review.body, repo_path)
        comments = [f.comment() for f in found]

        if comments:
            progress(
                f"PR #{number} にレビューを投稿します "
                f"({len(comments)} 件をソース行へのコメントにします)..."
            )
            posted = self._post_with_comments(
                owner,
                repo,
                number,
                f"{reviews.header_line(review)}\n\n{summary}".rstrip(),
                comments,
                review.head_commit,
            )
            if posted:
                return
            progress("行コメントを解決できなかったため、本文にまとめて投稿し直します。")
        else:
            progress(f"PR #{number} にレビューを投稿します...")

        github_api.create_review(
            github_auth.get_provider(),
            owner,
            repo,
            number,
            reviews.render_body(review),
            commit_id=review.head_commit,
        )

    def _post_with_comments(
        self,
        owner: str,
        repo: str,
        number: int,
        body: str,
        comments: list[dict],
        commit_id: str | None,
    ) -> bool:
        """行コメント付きで投稿する。GitHub が位置を解決できなければ False を返す。

        422 は差分に無いパスや行を指したときのもので (エラーは "Path could not be
        resolved" など)、投稿はまるごと行われていない。本文だけならまだ通るので、
        呼び出し元で投稿し直す。それ以外の失敗は投稿の失敗として伝える。
        """
        try:
            github_api.create_review(
                github_auth.get_provider(),
                owner,
                repo,
                number,
                body,
                comments=comments,
                commit_id=commit_id,
            )
        except github_api.ApiError as e:
            if e.status != 422:
                raise
            return False
        return True
