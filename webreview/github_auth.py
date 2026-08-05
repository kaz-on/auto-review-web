"""GitHub のアクセストークンを供給するモジュール。

2 つのモードを持つ (``GITHUB_AUTH_MODE`` で選択):

* ``pat`` -- ``GH_TOKEN`` の Personal Access Token をそのまま使う。失効しない。
* ``app`` -- GitHub App の秘密鍵から JWT を作り、installation access token を
  発行する。

installation access token は 1 時間で失効するので「使う直前に取得する」設計に
する。``token()`` は残り有効期間が ``GITHUB_TOKEN_REFRESH_MARGIN`` 以上あれば
キャッシュを返し、下回っていれば再発行する。起動時やジョブ開始時にまとめて
取得する方式だと、常駐サーバや「レビュー完了から時間が経ってからの手動投稿」で
失効するため採用しない。

ワーカースレッド (レビュー実行) と投稿スレッドから同時に呼ばれるため、発行は
ロックで保護する。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config, github_api
from .errors import ReviewError

# JWT の有効期間。GitHub の上限は 10 分。時刻ずれ対策で iat は 60 秒過去に置く。
_JWT_LIFETIME = 9 * 60
_JWT_CLOCK_SKEW = 60

# expires_at を解釈できなかった場合に仮定する有効期間 (GitHub の仕様値)。
_DEFAULT_TOKEN_LIFETIME = 3600


class PatTokenProvider:
    """PAT をそのまま返す。失効しないのでキャッシュも更新も不要。"""

    def __init__(self, token: str):
        self._token = token

    def token(self) -> str:
        return self._token

    def invalidate(self) -> None:
        # PAT は再発行できないので何もしない。
        pass


class AppTokenProvider:
    """GitHub App の installation access token を必要に応じて発行する。"""

    def __init__(
        self,
        client_id: str,
        private_key: str,
        installation_id: str = "",
        refresh_margin: int = 300,
    ):
        self._client_id = client_id
        self._private_key = private_key
        self._installation_id = installation_id
        self._refresh_margin = refresh_margin
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at = 0.0

    def token(self) -> str:
        with self._lock:
            if self._token is not None and time.time() < self._expires_at - self._refresh_margin:
                return self._token
            self._token, self._expires_at = self._issue_token()
            return self._token

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._expires_at = 0.0

    # --- 内部 ---------------------------------------------------------------
    def _issue_token(self) -> tuple[str, float]:
        jwt_token = self._build_jwt()
        installation_id = self._resolve_installation_id(jwt_token)
        data = github_api.api_request(
            "POST",
            f"/app/installations/{installation_id}/access_tokens",
            authorization=f"Bearer {jwt_token}",
            # ボディを省略すると App が持つ全権限・全リポジトリのトークンになる。
            # 対象は App のインストール時に絞る運用とする。
            body={},
        )
        if not isinstance(data, dict) or not data.get("token"):
            raise ReviewError("installation access token を取得できませんでした。")
        return data["token"], _parse_expires_at(data.get("expires_at"))

    def _resolve_installation_id(self, jwt_token: str) -> str:
        """未指定なら REVIEW_ORG の installation を引いて以後キャッシュする。"""
        if self._installation_id:
            return self._installation_id

        data = github_api.api_request(
            "GET",
            f"/orgs/{config.REVIEW_ORG}/installation",
            authorization=f"Bearer {jwt_token}",
        )
        installation_id = str((data or {}).get("id") or "")
        if not installation_id:
            raise ReviewError(
                f"GitHub App が Organization ({config.REVIEW_ORG}) に "
                "インストールされていません。"
            )
        self._installation_id = installation_id
        return installation_id

    def _build_jwt(self) -> str:
        try:
            import jwt  # PyJWT[crypto]
        except ImportError:
            raise ReviewError(
                "GitHub App 認証には PyJWT が必要です。"
                "`pip install -r requirements.txt` を実行してください。"
            ) from None

        now = int(time.time())
        payload: dict[str, Any] = {
            "iat": now - _JWT_CLOCK_SKEW,
            "exp": now + _JWT_LIFETIME,
            "iss": self._client_id,
        }
        try:
            return jwt.encode(payload, self._private_key, algorithm="RS256")
        except Exception as e:  # 鍵の形式不正など
            raise ReviewError(f"GitHub App の秘密鍵で JWT を署名できません: {e}") from None


def _parse_expires_at(value: Any) -> float:
    """"2026-08-06T12:34:56Z" 形式を epoch 秒にする。解釈できなければ 1 時間後。"""
    if isinstance(value, str) and value:
        try:
            return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=timezone.utc
            ).timestamp()
        except ValueError:
            pass
    return time.time() + _DEFAULT_TOKEN_LIFETIME


def _load_private_key() -> str:
    """秘密鍵をパスまたは環境変数から読む。パス指定を優先する。"""
    if config.GITHUB_APP_PRIVATE_KEY_PATH:
        try:
            return Path(config.GITHUB_APP_PRIVATE_KEY_PATH).read_text(encoding="utf-8")
        except OSError as e:
            raise ReviewError(
                f"GitHub App の秘密鍵を読めません "
                f"({config.GITHUB_APP_PRIVATE_KEY_PATH}): {e}"
            ) from None

    if config.GITHUB_APP_PRIVATE_KEY:
        # .env に 1 行で書けるよう、リテラルの \n を改行に戻す。
        return config.GITHUB_APP_PRIVATE_KEY.replace("\\n", "\n")

    raise ReviewError(
        "GITHUB_APP_PRIVATE_KEY_PATH または GITHUB_APP_PRIVATE_KEY を設定してください。"
    )


def build_provider() -> PatTokenProvider | AppTokenProvider:
    mode = config.GITHUB_AUTH_MODE
    if mode == "pat":
        if not config.GH_TOKEN:
            raise ReviewError("GH_TOKEN が設定されていません。")
        return PatTokenProvider(config.GH_TOKEN)

    if mode == "app":
        if not config.GITHUB_APP_CLIENT_ID:
            raise ReviewError("GITHUB_APP_CLIENT_ID が設定されていません。")
        return AppTokenProvider(
            client_id=config.GITHUB_APP_CLIENT_ID,
            private_key=_load_private_key(),
            installation_id=config.GITHUB_APP_INSTALLATION_ID,
            refresh_margin=config.GITHUB_TOKEN_REFRESH_MARGIN,
        )

    raise ReviewError(
        f"GITHUB_AUTH_MODE の値が不正です: {mode} (pat または app を指定してください)"
    )


_PROVIDER: PatTokenProvider | AppTokenProvider | None = None
_PROVIDER_LOCK = threading.Lock()


def get_provider() -> PatTokenProvider | AppTokenProvider:
    """プロセス全体で 1 つの TokenProvider を返す (初回のみ構築)。"""
    global _PROVIDER
    with _PROVIDER_LOCK:
        if _PROVIDER is None:
            _PROVIDER = build_provider()
        return _PROVIDER
