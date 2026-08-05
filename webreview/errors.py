"""ユーザーに提示する想定の例外。

review / github_api / github_auth が相互に import せずに済むよう、共通の例外だけを
ここに切り出している。message はそのまま画面に表示される。
"""

from __future__ import annotations


class ReviewError(Exception):
    """ユーザーに提示する想定のエラー。message はそのまま画面に表示される。"""
