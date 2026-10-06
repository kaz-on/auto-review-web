"""トークンの供給。

installation access token は 1 時間で失効するので「使う直前に取得する」設計に
なっている。キャッシュを返す条件と再発行する条件、そして秘密鍵や JWT の扱いを見る。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import pytest

from webreview import config, github_api, github_auth
from webreview.errors import ReviewError
from webreview.github_auth import AppTokenProvider, PatTokenProvider


# --- PAT --------------------------------------------------------------------
def test_pat_はそのまま返り失効しない():
    provider = PatTokenProvider("ghp_xxx")

    assert provider.token() == "ghp_xxx"
    provider.invalidate()
    # 再発行できないので invalidate 後も同じ値を返す。
    assert provider.token() == "ghp_xxx"


# --- installation access token ----------------------------------------------
@pytest.fixture
def api_calls(monkeypatch):
    """github_api.api_request を差し替え、呼び出しを記録する。"""
    calls: list[tuple[str, str, str]] = []
    expires_at = ["2099-01-01T00:00:00Z"]
    counter = [0]

    def fake_api_request(method, path, *, authorization, body=None):
        calls.append((method, path, authorization))
        if path.endswith("/installation"):
            return {"id": 12345}
        counter[0] += 1
        return {"token": f"ghs_{counter[0]}", "expires_at": expires_at[0]}

    monkeypatch.setattr(github_api, "api_request", fake_api_request)
    return calls, expires_at


@pytest.fixture
def fake_jwt(monkeypatch):
    """PyJWT の署名を差し替える (鍵の生成は本筋ではない)。"""
    signed: list[dict] = []

    class FakeJwt:
        @staticmethod
        def encode(payload, key, algorithm):
            signed.append({"payload": payload, "key": key, "algorithm": algorithm})
            return "jwt-token"

    monkeypatch.setitem(__import__("sys").modules, "jwt", FakeJwt)
    return signed


def _provider(**kw) -> AppTokenProvider:
    return AppTokenProvider(
        client_id="Iv1.abc",
        private_key="-----BEGIN RSA PRIVATE KEY-----\n...",
        **kw,
    )


def test_installation_access_token_を発行する(api_calls, fake_jwt):
    calls, _ = api_calls

    token = _provider(installation_id="12345").token()

    assert token == "ghs_1"
    assert calls == [(
        "POST", "/app/installations/12345/access_tokens", "Bearer jwt-token",
    )]


def test_有効期限に余裕があればキャッシュを返す(api_calls, fake_jwt):
    calls, _ = api_calls
    provider = _provider(installation_id="12345")

    assert provider.token() == provider.token() == "ghs_1"
    assert len(calls) == 1


def test_期限が近づいたら再発行する(api_calls, fake_jwt):
    calls, expires_at = api_calls
    # 残り 60 秒 < margin 300 秒。
    expires_at[0] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 60))
    provider = _provider(installation_id="12345", refresh_margin=300)

    assert provider.token() == "ghs_1"
    assert provider.token() == "ghs_2"
    assert len(calls) == 2


def test_invalidate_すると次回に取り直す(api_calls, fake_jwt):
    provider = _provider(installation_id="12345")

    assert provider.token() == "ghs_1"
    provider.invalidate()
    assert provider.token() == "ghs_2"


def test_installation_id_未指定なら_owner_から解決して以後使い回す(
    api_calls, fake_jwt, monkeypatch
):
    calls, _ = api_calls
    monkeypatch.setattr(config, "REVIEW_OWNER", "example-org")
    provider = _provider()

    provider.token()
    provider.invalidate()
    provider.token()

    paths = [path for _m, path, _a in calls]
    # 解決は最初の 1 回だけ。
    assert paths == [
        "/orgs/example-org/installation",
        "/app/installations/12345/access_tokens",
        "/app/installations/12345/access_tokens",
    ]


def _not_found(path: str) -> github_api.ApiError:
    return github_api.ApiError(f"GitHub API がエラーを返しました (GET {path}, HTTP 404)", status=404)


def test_org_で見つからなければ個人アカウントとして引き直す(monkeypatch, fake_jwt):
    """/orgs/{org}/installation は Organization 専用で、個人アカウントには使えない。"""
    monkeypatch.setattr(config, "REVIEW_OWNER", "some-user")
    paths: list[str] = []

    def fake_api_request(method, path, *, authorization, body=None):
        paths.append(path)
        if path == "/orgs/some-user/installation":
            raise _not_found(path)
        if path == "/users/some-user/installation":
            return {"id": 777}
        return {"token": "ghs_1", "expires_at": "2099-01-01T00:00:00Z"}

    monkeypatch.setattr(github_api, "api_request", fake_api_request)

    assert _provider().token() == "ghs_1"
    assert paths == [
        "/orgs/some-user/installation",
        "/users/some-user/installation",
        "/app/installations/777/access_tokens",
    ]


def test_app_が_owner_に入っていなければ分かる形で失敗する(monkeypatch, fake_jwt):
    monkeypatch.setattr(config, "REVIEW_OWNER", "example-org")

    def fake_api_request(method, path, *, authorization, body=None):
        raise _not_found(path)

    monkeypatch.setattr(github_api, "api_request", fake_api_request)

    with pytest.raises(ReviewError, match="インストールされていません"):
        _provider().token()


def test_404_以外のエラーは引き直さずにそのまま返す(monkeypatch, fake_jwt):
    """JWT の不備などを「インストールされていない」と取り違えない。"""
    monkeypatch.setattr(config, "REVIEW_OWNER", "example-org")
    paths: list[str] = []

    def fake_api_request(method, path, *, authorization, body=None):
        paths.append(path)
        raise github_api.ApiError("A JSON web token could not be decoded", status=401)

    monkeypatch.setattr(github_api, "api_request", fake_api_request)

    with pytest.raises(github_api.ApiError, match="JSON web token"):
        _provider().token()
    assert paths == ["/orgs/example-org/installation"]


def test_token_が返らなければ失敗させる(monkeypatch, fake_jwt):
    monkeypatch.setattr(
        github_api, "api_request", lambda *a, **k: {"expires_at": "2099-01-01T00:00:00Z"}
    )

    with pytest.raises(ReviewError, match="installation access token"):
        _provider(installation_id="1").token()


def test_全権限のトークンにならないよう空のボディを送る(monkeypatch, fake_jwt):
    """ボディを省略すると App が持つ全権限・全リポジトリのトークンになる。"""
    bodies: list = []

    def fake_api_request(method, path, *, authorization, body=None):
        bodies.append(body)
        return {"token": "ghs_1", "expires_at": "2099-01-01T00:00:00Z"}

    monkeypatch.setattr(github_api, "api_request", fake_api_request)

    _provider(installation_id="1").token()

    assert bodies == [{}]


def test_同時に呼ばれても発行は一度だけ(api_calls, fake_jwt):
    calls, _ = api_calls
    provider = _provider(installation_id="12345")
    tokens: list[str] = []

    threads = [
        threading.Thread(target=lambda: tokens.append(provider.token()))
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert set(tokens) == {"ghs_1"}
    assert len(calls) == 1


# --- JWT --------------------------------------------------------------------
def test_jwt_は時刻ずれを見込んで署名する(api_calls, fake_jwt):
    now = int(time.time())

    _provider(installation_id="1").token()

    payload = fake_jwt[0]["payload"]
    assert payload["iss"] == "Iv1.abc"
    assert payload["iat"] <= now - 60
    # GitHub の上限は 10 分。
    assert 0 < payload["exp"] - now <= 600
    assert fake_jwt[0]["algorithm"] == "RS256"


def test_秘密鍵が不正なら分かる形で失敗する(monkeypatch):
    class BrokenJwt:
        @staticmethod
        def encode(payload, key, algorithm):
            raise ValueError("Could not deserialize key data")

    monkeypatch.setitem(__import__("sys").modules, "jwt", BrokenJwt)

    with pytest.raises(ReviewError, match="JWT を署名できません"):
        _provider(installation_id="1").token()


# --- 有効期限の解釈 ---------------------------------------------------------
def test_expires_at_を_epoch_秒にする():
    """末尾の Z は UTC。ローカルタイムとして読むと有効期限がずれる。"""
    expected = datetime(2026, 8, 6, 12, 34, 56, tzinfo=timezone.utc).timestamp()

    assert github_auth._parse_expires_at("2026-08-06T12:34:56Z") == expected


@pytest.mark.parametrize("value", [None, "", "not-a-date", 123, "2026-08-06 12:34:56"])
def test_解釈できない_expires_at_は一時間後とみなす(value):
    got = github_auth._parse_expires_at(value)

    assert abs(got - (time.time() + 3600)) < 5


# --- 秘密鍵の読み込み -------------------------------------------------------
def test_秘密鍵はパス指定を優先する(tmp_path, monkeypatch):
    key_file = tmp_path / "key.pem"
    key_file.write_text("FROM FILE")
    monkeypatch.setattr(config, "GITHUB_APP_PRIVATE_KEY_PATH", str(key_file))
    monkeypatch.setattr(config, "GITHUB_APP_PRIVATE_KEY", "FROM ENV")

    assert github_auth._load_private_key() == "FROM FILE"


def test_秘密鍵を読めなければ分かる形で失敗する(tmp_path, monkeypatch):
    monkeypatch.setattr(
        config, "GITHUB_APP_PRIVATE_KEY_PATH", str(tmp_path / "missing.pem")
    )

    with pytest.raises(ReviewError, match="秘密鍵を読めません"):
        github_auth._load_private_key()


def test_環境変数の秘密鍵はリテラルの改行を戻す(monkeypatch):
    """.env には 1 行でしか書けない。"""
    monkeypatch.setattr(config, "GITHUB_APP_PRIVATE_KEY_PATH", "")
    monkeypatch.setattr(
        config, "GITHUB_APP_PRIVATE_KEY", "-----BEGIN-----\\nabc\\n-----END-----"
    )

    assert github_auth._load_private_key() == "-----BEGIN-----\nabc\n-----END-----"


def test_秘密鍵が無ければ設定を促す(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_APP_PRIVATE_KEY_PATH", "")
    monkeypatch.setattr(config, "GITHUB_APP_PRIVATE_KEY", "")

    with pytest.raises(ReviewError, match="GITHUB_APP_PRIVATE_KEY"):
        github_auth._load_private_key()


# --- モードの選択 -----------------------------------------------------------
def test_pat_モードでは_gh_token_を使う(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_AUTH_MODE", "pat")
    monkeypatch.setattr(config, "GH_TOKEN", "ghp_xxx")

    provider = github_auth.build_provider()

    assert isinstance(provider, PatTokenProvider)
    assert provider.token() == "ghp_xxx"


def test_pat_モードで_gh_token_が無ければ失敗する(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_AUTH_MODE", "pat")
    monkeypatch.setattr(config, "GH_TOKEN", "")

    with pytest.raises(ReviewError, match="GH_TOKEN"):
        github_auth.build_provider()


def test_app_モードでは設定から_provider_を組む(monkeypatch, tmp_path):
    key_file = tmp_path / "key.pem"
    key_file.write_text("KEY")
    monkeypatch.setattr(config, "GITHUB_AUTH_MODE", "app")
    monkeypatch.setattr(config, "GITHUB_APP_CLIENT_ID", "Iv1.abc")
    monkeypatch.setattr(config, "GITHUB_APP_PRIVATE_KEY_PATH", str(key_file))
    monkeypatch.setattr(config, "GITHUB_APP_INSTALLATION_ID", "999")
    monkeypatch.setattr(config, "GITHUB_TOKEN_REFRESH_MARGIN", 123)

    provider = github_auth.build_provider()

    assert isinstance(provider, AppTokenProvider)
    assert provider._installation_id == "999"
    assert provider._refresh_margin == 123
    assert provider._private_key == "KEY"


def test_app_モードで_client_id_が無ければ失敗する(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_AUTH_MODE", "app")
    monkeypatch.setattr(config, "GITHUB_APP_CLIENT_ID", "")

    with pytest.raises(ReviewError, match="GITHUB_APP_CLIENT_ID"):
        github_auth.build_provider()


def test_知らないモードは失敗させる(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_AUTH_MODE", "oauth")

    with pytest.raises(ReviewError, match="GITHUB_AUTH_MODE"):
        github_auth.build_provider()


def test_provider_はプロセス全体で一つ(monkeypatch):
    monkeypatch.setattr(config, "GITHUB_AUTH_MODE", "pat")
    monkeypatch.setattr(config, "GH_TOKEN", "ghp_xxx")

    assert github_auth.get_provider() is github_auth.get_provider()
