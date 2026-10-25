"""GitHub の forge 実装。github.com と、GitHub 互換の REST API を持つサービスに使う。

REST API は github_api、トークンは github_auth が受け持ち、ここでは URL の形と
投稿の手順を決める。どちらのモジュールも呼び出しの時点で属性を引くので、
テストで github_api / github_auth の関数を差し替えればそのまま効く。

互換のサービスとの違いは設定で吸収する (config の GITHUB_WEB_URL ほか)。
clone URL の形、git の認証に通るユーザー名、レビュー API の有無がサービスごとに違う。
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from . import config, findings, github_api, github_auth, reviews
from .errors import ReviewError
from .findings import Roots
from .forge import GitCredential, ProgressFn, PullRequest
from .reviews import Review

_POST_MODES = ("review", "issue_comment")


class GitHubForge:
    name = "GitHub"

    def __init__(self) -> None:
        web = config.GITHUB_WEB_URL
        if not re.match(r"^https?://[^/]+", web):
            raise ReviewError(f"GITHUB_WEB_URL の値が不正です: {web}")
        template = config.GITHUB_CLONE_URL.replace("{web}", web)
        if "{owner}" not in template or "{repo}" not in template:
            raise ReviewError(
                "GITHUB_CLONE_URL には {owner} と {repo} を含めてください: "
                f"{config.GITHUB_CLONE_URL}"
            )
        if config.GITHUB_POST_MODE not in _POST_MODES:
            raise ReviewError(
                f"GITHUB_POST_MODE の値が不正です: {config.GITHUB_POST_MODE} "
                "(review または issue_comment を指定してください)"
            )

        self._web = web
        self._clone_template = template
        self._post_mode = config.GITHUB_POST_MODE
        # ホスト名の前方一致で通さないよう、ホストの直後に "/" を求める。
        self._pr_url_re = re.compile(
            rf"^{re.escape(web)}/([^/]+)/([^/]+)/pull/([0-9]+)(?:/.*)?$"
        )
        self._remote_re = _template_regex(template)
        # credential helper はスキームとホストで効かせる (clone URL のパスは含めない)。
        parts = urlsplit(template.replace("{owner}", "o").replace("{repo}", "r"))
        self._credential_url = f"{parts.scheme}://{parts.netloc}"

    def owner_allowed(self, owner: str, allowed: str) -> bool:
        return owner == allowed

    def auth_summary(self) -> str:
        for owner in config.REVIEW_OWNERS or ("",):
            github_auth.get_provider(owner)
        return f"GitHub 認証モード: {config.GITHUB_AUTH_MODE}"

    def parse_pr_url(self, url: str) -> tuple[str, str, int] | None:
        m = self._pr_url_re.match(url)
        if not m:
            return None
        return m.group(1), m.group(2), int(m.group(3))

    def pr_url(self, owner: str, repo: str, number: int) -> str:
        return f"{self._web}/{owner}/{repo}/pull/{number}"

    def pr_url_pattern(self) -> str:
        return f"{self._web}/<owner>/<repo>/pull/<番号>"

    def clone_url(self, owner: str, repo: str) -> str:
        return self._clone_template.replace("{owner}", owner).replace("{repo}", repo)

    def parse_remote_url(self, url: str) -> tuple[str, str] | None:
        m = self._remote_re.match(url)
        if not m:
            return None
        return m.group("owner"), m.group("repo")

    def git_credential(self, owner: str) -> GitCredential:
        return GitCredential(
            url=self._credential_url,
            username=config.GITHUB_GIT_USERNAME,
            password=github_auth.get_provider(owner).token(),
        )

    def get_pull(self, owner: str, repo: str, number: int) -> PullRequest:
        return github_api.get_pull(github_auth.get_provider(owner), owner, repo, number)

    def post_review(
        self,
        owner: str,
        repo: str,
        number: int,
        review: Review,
        roots: Roots,
        progress: ProgressFn,
    ) -> None:
        """位置の読み取れた指摘はソース行へのコメントにし、残り (全体の要約や書式の
        読めなかったぶん) をレビュー本体の本文にする。GitHub が位置を解決できないと
        レビューごと 422 で弾かれるため、そのときは本文にまとめて投稿し直す。

        レビュー API の無いサービス (GITHUB_POST_MODE=issue_comment) には、本文全体を
        1 件のコメントとして投稿する。行コメントが弾かれたときと同じ形になる。
        """
        if self._post_mode == "issue_comment":
            progress(f"PR #{number} にコメントとして投稿します...")
            github_api.create_issue_comment(
                github_auth.get_provider(owner),
                owner,
                repo,
                number,
                reviews.render_body(review),
            )
            return

        summary, found = findings.extract(review.body, roots)
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
            github_auth.get_provider(owner),
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
                github_auth.get_provider(owner),
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


def _template_regex(template: str) -> re.Pattern[str]:
    """clone URL の形から、origin の URL を owner / repo に分解する正規表現を作る。

    末尾の ".git" は省略されていても同じリポジトリとみなす。
    """
    body = template.removesuffix(".git")
    pattern = ""
    for part in re.split(r"(\{owner\}|\{repo\})", body):
        if part == "{owner}":
            pattern += "(?P<owner>[^/]+)"
        elif part == "{repo}":
            pattern += "(?P<repo>[^/]+?)"
        else:
            pattern += re.escape(part)
    return re.compile(rf"^{pattern}(?:\.git)?$")
