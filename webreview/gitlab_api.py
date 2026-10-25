"""GitLab REST API (v4) の薄いラッパ。

github_api と同じく標準ライブラリの urllib で組む。トークンは PAT / グループ・
プロジェクトアクセストークンのどれでもよく、失効しない前提で毎回そのまま渡す
(GitHub App のような再発行はない)。

プロジェクトは "group/sub/project" のパスで指定する。API ではパスの "/" を
エンコードしないと別のエンドポイントとして解釈され 404 になる。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from . import config
from .errors import ReviewError

_TIMEOUT = 30


class ApiError(ReviewError):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def project_path(owner: str, repo: str) -> str:
    """API の URL に埋めるプロジェクトの指定 ("group%2Fsub%2Fproject")。"""
    return urllib.parse.quote(f"{owner}/{repo}", safe="")


def api_request(
    method: str,
    path: str,
    *,
    token: str,
    body: dict[str, Any] | None = None,
) -> Any:
    """GitLab API を呼び出して JSON を返す。path は "/projects/..." 形式。"""
    url = f"{config.GITLAB_URL}/api/v4{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/json")
    req.add_header("PRIVATE-TOKEN", token)
    req.add_header("User-Agent", "auto-review")
    if data is not None:
        req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise ApiError(_format_http_error(method, url, e), status=e.code) from None
    except urllib.error.URLError as e:
        raise ApiError(f"GitLab API に接続できません ({method} {url}): {e.reason}") from None

    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ApiError(f"GitLab API の応答を解釈できません ({method} {url})") from None


def _format_http_error(method: str, url: str, e: urllib.error.HTTPError) -> str:
    # GitLab のエラーは {"message": "..."} か {"error": "..."}。message は文字列の
    # ほか、項目ごとのエラーの辞書や配列になることもある。
    detail = ""
    try:
        payload = json.loads(e.read().decode("utf-8"))
        value = payload.get("message") or payload.get("error") or ""
        detail = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    except Exception:  # noqa: BLE001 - 応答が JSON でない場合は素通し
        pass
    return f"GitLab API がエラーを返しました ({method} {url}, HTTP {e.code}): {detail}".rstrip(": ")


def get_merge_request(token: str, owner: str, repo: str, iid: int) -> dict[str, Any]:
    """MR の情報をそのまま返す (解釈は gitlab_forge が行う)。"""
    data = api_request(
        "GET", f"/projects/{project_path(owner, repo)}/merge_requests/{iid}", token=token
    )
    if not isinstance(data, dict):
        raise ApiError(f"MR の情報を取得できませんでした: {owner}/{repo}!{iid}")
    return data


def create_note(token: str, owner: str, repo: str, iid: int, body: str) -> None:
    """MR にコメントを 1 件投稿する。"""
    api_request(
        "POST",
        f"/projects/{project_path(owner, repo)}/merge_requests/{iid}/notes",
        token=token,
        body={"body": body},
    )
