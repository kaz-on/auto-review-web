"""GitLab (CE / セルフホスト) の forge 実装。

github_forge との主な違い:

* オーナーはグループのパスで、サブグループを含めて "group/sub" のように "/" を
  含みうる。PR (MR) の URL は <group>/<sub>/<project>/-/merge_requests/<iid>。
* REVIEW_OWNER のグループには、その下のサブグループも含める。グループアクセス
  トークンの効く範囲と揃えるため。
* レビュー対象のコミットは MR の diff_refs から取る。MR を作った直後は diff_refs が
  まだ計算されておらず null になるので、少し待って取り直す。
* 投稿は MR へのコメント (note) 1 件だけ。行コメント (discussions) は位置の指定が
  複雑なうえ 1 件ずつ投稿するので一部だけ失敗しうるため、まだ扱わない。
* GitLab の Markdown は単独の改行を改行として表示しないので、投稿するときだけ
  指摘の見出しの直後の改行を明示する (findings.break_headings)。
  ファイルへのリンク (links) の行範囲は "#L40-45"。
"""

from __future__ import annotations

import re
import time
from functools import partial
from urllib.parse import quote, urlsplit

from . import config, findings, gitlab_api, links, reviews
from .errors import ReviewError
from .findings import Roots
from .forge import GitCredential, ProgressFn, PullRequest
from .reviews import Review

# MR を作った直後に diff_refs が null だったときに取り直す間隔 (秒)。
_DIFF_REFS_RETRY_DELAYS = (1, 2, 4)

# テストで待たずに済むよう差し替えられるようにしておく。
_sleep = time.sleep


class GitLabForge:
    name = "GitLab"

    def __init__(self) -> None:
        web = config.GITLAB_URL
        if not re.match(r"^https?://[^/]+", web):
            raise ReviewError(f"GITLAB_URL の値が不正です: {web or '(未設定)'}")
        self._web = web
        self._pr_url_re = re.compile(
            rf"^{re.escape(web)}/(?P<path>[^?#]+?)/-/merge_requests/(?P<iid>[0-9]+)"
            r"(?:[/?#].*)?$"
        )
        self._remote_re = re.compile(
            rf"^{re.escape(web)}/(?P<owner>.+)/(?P<repo>[^/]+?)(?:\.git)?$"
        )
        parts = urlsplit(web)
        self._credential_url = f"{parts.scheme}://{parts.netloc}"

    def owner_allowed(self, owner: str, allowed: str) -> bool:
        return owner == allowed or owner.startswith(f"{allowed}/")

    def auth_summary(self) -> str:
        _token()
        return "GitLab のトークン: 設定済み"

    def parse_pr_url(self, url: str) -> tuple[str, str, int] | None:
        m = self._pr_url_re.match(url)
        if not m:
            return None
        owner, sep, repo = m.group("path").rpartition("/")
        if not sep or not owner:
            return None
        return owner, repo, int(m.group("iid"))

    def pr_url(self, owner: str, repo: str, number: int) -> str:
        return f"{self._web}/{owner}/{repo}/-/merge_requests/{number}"

    def pr_url_pattern(self) -> str:
        return f"{self._web}/<group>/<project>/-/merge_requests/<番号>"

    def blob_url(
        self, owner: str, repo: str, sha: str, path: str,
        start: int | None = None, end: int | None = None,
    ) -> str:
        url = f"{self._web}/{owner}/{repo}/-/blob/{sha}/{quote(path)}"
        if start is None:
            return url
        # GitLab の行範囲は "#L<開始>-<終了>" (GitHub と違い終了側に L を付けない)。
        return f"{url}#L{start}" if end in (None, start) else f"{url}#L{start}-{end}"

    def clone_url(self, owner: str, repo: str) -> str:
        return f"{self._web}/{owner}/{repo}.git"

    def parse_remote_url(self, url: str) -> tuple[str, str] | None:
        m = self._remote_re.match(url)
        if not m:
            return None
        return m.group("owner"), m.group("repo")

    def git_credential(self, owner: str) -> GitCredential:
        # GitLab のトークンはユーザー名を問わない。慣例の oauth2 にしておく。
        return GitCredential(url=self._credential_url, username="oauth2", password=_token())

    def get_pull(self, owner: str, repo: str, number: int) -> PullRequest:
        """MR の情報を取得する。

        比較の起点は diff_refs.base_sha (target ブランチとの merge-base)。GitLab が
        MR の差分として表示するのと同じ範囲になる。マージ先が進んでも、マージして
        ブランチを消しても、diff_refs は MR の差分を作ったときのまま変わらない。
        """
        data = gitlab_api.get_merge_request(_token(), owner, repo, number)
        for delay in _DIFF_REFS_RETRY_DELAYS:
            if data.get("diff_refs"):
                break
            _sleep(delay)
            data = gitlab_api.get_merge_request(_token(), owner, repo, number)

        label = f"{owner}/{repo}!{number}"
        diff_refs = data.get("diff_refs") or {}
        base_ref = data.get("target_branch") or ""
        if not base_ref:
            raise ReviewError(f"MR の target ブランチを取得できませんでした: {label}")
        base_sha = diff_refs.get("base_sha") or ""
        head_sha = diff_refs.get("head_sha") or ""
        for kind, sha in (("base", base_sha), ("head", head_sha)):
            if not sha:
                raise ReviewError(
                    f"MR の {kind} コミットを取得できませんでした: {label}"
                    "\nMR を作った直後なら、少し待ってからやり直してください。"
                )
        changes = data.get("changes_count")
        return PullRequest(
            base_ref=base_ref,
            base_sha=base_sha,
            head_sha=head_sha,
            title=data.get("title") or "",
            body=data.get("description") or "",
            author=(data.get("author") or {}).get("username") or "",
            # 行数は返らない。変更ファイル数は文字列で、多いと "1000+" になる。
            changed_files=int(changes) if isinstance(changes, str) and changes.isdigit() else None,
        )

    def post_review(
        self,
        owner: str,
        repo: str,
        number: int,
        review: Review,
        roots: Roots,
        progress: ProgressFn,
    ) -> None:
        """ヘッダ付きの本文全体を MR へのコメント 1 件として投稿する。"""
        blob = (
            partial(self.blob_url, owner, repo, review.head_commit)
            if review.head_commit
            else None
        )
        # 見出しのパスはリンクにすると形が変わるので、改行を先に付ける。
        body = findings.break_headings(reviews.render_body(review))
        body = links.linkify(body, roots, blob)
        progress(f"MR !{number} にコメントとして投稿します...")
        gitlab_api.create_note(_token(), owner, repo, number, body)


def _token() -> str:
    if not config.GITLAB_TOKEN:
        raise ReviewError("GITLAB_TOKEN が設定されていません。")
    return config.GITLAB_TOKEN
