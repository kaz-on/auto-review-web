"""GitHub REST API の薄いラッパ。

GitHub CLI (gh) を使わずに済ませるための最小限の実装。追加依存を増やさないよう
標準ライブラリの urllib で組む。

低レベルの :func:`api_request` は Authorization ヘッダの値を直接受け取るだけで、
トークンの取得方法 (PAT / GitHub App) は関知しない。高レベル関数は TokenProvider
を受け取り、401 が返ったらキャッシュを捨てて 1 回だけ再試行する
(installation access token の失効やクロックずれへの保険)。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Protocol

from . import config
from .errors import ReviewError
from .forge import PullRequest

# 明示しないと GitHub 側の既定バージョンが使われ、将来の既定変更で挙動が変わるため固定する。
_API_VERSION = "2022-11-28"
_TIMEOUT = 30


class TokenProvider(Protocol):
    """使う直前にトークンを供給するオブジェクト (github_auth を参照)。"""

    def token(self) -> str: ...

    def invalidate(self) -> None: ...


class ApiError(ReviewError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def api_request(
    method: str,
    path: str,
    *,
    authorization: str,
    body: dict[str, Any] | None = None,
) -> Any:
    """GitHub API を呼び出して JSON を返す。path は "/repos/..." 形式。"""
    url = path if path.startswith("http") else f"{config.GITHUB_API_URL}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", _API_VERSION)
    req.add_header("Authorization", authorization)
    req.add_header("User-Agent", "auto-review")
    if data is not None:
        req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise ApiError(_format_http_error(method, url, e), status=e.code) from None
    except urllib.error.URLError as e:
        raise ApiError(f"GitHub API に接続できません ({method} {url}): {e.reason}") from None

    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ApiError(f"GitHub API の応答を解釈できません ({method} {url})") from None


def _format_http_error(method: str, url: str, e: urllib.error.HTTPError) -> str:
    detail = ""
    try:
        payload = json.loads(e.read().decode("utf-8"))
        detail = payload.get("message", "")
        for err in payload.get("errors", []) or []:
            if isinstance(err, dict) and err.get("message"):
                detail += f"\n- {err['message']}"
    except Exception:  # noqa: BLE001 - 応答が JSON でない場合は素通し
        pass
    return f"GitHub API がエラーを返しました ({method} {url}, HTTP {e.code}): {detail}".rstrip(": ")


def _request_with_token(
    provider: TokenProvider,
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
) -> Any:
    """トークン認証で API を呼ぶ。401 のときだけトークンを取り直して再試行する。"""
    try:
        return api_request(
            method, path, authorization=f"Bearer {provider.token()}", body=body
        )
    except ApiError as e:
        if e.status != 401:
            raise
        provider.invalidate()
        return api_request(
            method, path, authorization=f"Bearer {provider.token()}", body=body
        )


# --- 高レベル API ------------------------------------------------------------

def get_pull(provider: TokenProvider, owner: str, repo: str, number: int) -> PullRequest:
    """PR の base/head コミット・base ブランチ・タイトル・本文・作成者・変更の規模を
    まとめて取得する。

    レビュー対象のコミットはここで返す SHA で確定させる。ブランチ名から git で
    引き直すと、PR がマージされたあとに base ブランチの先端が PR 自身を含んでしまう。
    SHA が欠けた応答は、取り違えたコミットをレビューするより止める方が安全なので
    エラーにする。
    """
    data = _request_with_token(provider, "GET", f"/repos/{owner}/{repo}/pulls/{number}")
    if not isinstance(data, dict):
        raise ApiError(f"PR の情報を取得できませんでした: {owner}/{repo}#{number}")
    base = data.get("base") or {}
    head = data.get("head") or {}
    base_ref = base.get("ref") or ""
    if not base_ref:
        raise ApiError(f"PR の base ブランチを取得できませんでした: {owner}/{repo}#{number}")
    base_sha = base.get("sha") or ""
    head_sha = head.get("sha") or ""
    for label, sha in (("base", base_sha), ("head", head_sha)):
        if not sha:
            raise ApiError(
                f"PR の {label} コミットを取得できませんでした: {owner}/{repo}#{number}"
            )
    return PullRequest(
        base_ref=base_ref,
        base_sha=base_sha,
        head_sha=head_sha,
        title=data.get("title") or "",
        # body は null になりうる。
        body=data.get("body") or "",
        author=(data.get("user") or {}).get("login") or "",
        # 変更の規模は head_sha 時点のもの (= レビューする差分の規模)。
        additions=_int_or_none(data.get("additions")),
        deletions=_int_or_none(data.get("deletions")),
        changed_files=_int_or_none(data.get("changed_files")),
    )


def _int_or_none(value: Any) -> int | None:
    # bool は int の派生だが、件数としては意味を成さない。
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def create_review(
    provider: TokenProvider,
    owner: str,
    repo: str,
    number: int,
    body: str,
    *,
    comments: list[dict[str, Any]] | None = None,
    commit_id: str | None = None,
) -> None:
    """PR にコメントレビューを投稿する (gh pr review --comment 相当)。

    comments を渡すとソース行へのコメントが付く。要素の形は「レビューコメントの
    作成」API と同じ (path / line / side / body、複数行なら start_line も)。
    GitHub が位置を解決できないときは 422 になり、レビュー全体が投稿されない。

    commit_id はレビュー対象のコミット。省略すると PR の最新コミットが使われる。
    """
    payload: dict[str, Any] = {"event": "COMMENT", "body": body}
    if commit_id:
        payload["commit_id"] = commit_id
    if comments:
        payload["comments"] = comments
    _request_with_token(
        provider, "POST", f"/repos/{owner}/{repo}/pulls/{number}/reviews", payload
    )


def create_issue_comment(
    provider: TokenProvider,
    owner: str,
    repo: str,
    number: int,
    body: str,
) -> None:
    """PR にコメントを 1 件投稿する (PR は issue でもあるので issues の API で書ける)。

    レビュー API (pulls/{n}/reviews) を持たない GitHub 互換のサービス向け。
    行コメントは付けられない。
    """
    _request_with_token(
        provider, "POST", f"/repos/{owner}/{repo}/issues/{number}/comments",
        {"body": body},
    )
