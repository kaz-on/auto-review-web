"""codex が出した作業ツリーのパスを、リポジトリのルートからの相対パスに直す。

アプリを Windows で動かすと、作業ツリーは "C:\\work\\o\\r" のような Windows の
パスになる。動かしている OS (os.path) ではなく、ルートの文字列の形で POSIX と
Windows を見分け、ntpath / PureWindowsPath で文字列として扱う。こうすると
Windows のパスの扱いも Linux 上のテストで確かめられる。

相対パスはどちらの場合も "/" 区切りで返す。行コメントの位置や blob の URL に
そのまま使うため。
"""

from __future__ import annotations

import ntpath
import posixpath
import re
from collections.abc import Sequence
from pathlib import Path, PureWindowsPath
from typing import Union

# 作業ツリーのルート。候補が複数あるときは長いものから試す。
Roots = Union[Path, Sequence[Path]]

# ドライブ文字から始まるパス ("C:\..." "C:/...") か、UNC パス ("\\server\share")。
_WINDOWS_ABS_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


def is_windows_absolute(path: str) -> bool:
    return bool(_WINDOWS_ABS_RE.match(path))


def normalize_roots(roots: Roots) -> list[str]:
    """ルートの候補を正規化した文字列にし、長いものから並べる。

    オーナーを固定していた頃のルート (WORK/<repo>) は、オーナーとリポジトリが同名だと
    今のルート (WORK/<owner>/<repo>) の先頭に一致するので、長いほうを先に試す。
    """
    items = [roots] if isinstance(roots, (Path, str)) else roots
    normalized = {
        ntpath.normpath(text) if is_windows_absolute(text) else posixpath.normpath(text)
        for text in map(str, items)
    }
    return sorted(normalized, key=len, reverse=True)


def relative_to_roots(path: str, roots: Sequence[str]) -> str | None:
    """作業ツリーの絶対パスを相対パスにする。どのルートの下でもなければ None。

    roots は normalize_roots で整えたもの。Windows のルートには区切りが "\\" でも
    "/" でも、大文字小文字が違っても一致させる (ドライブ文字が "c:" で出るなど)。
    """
    for root in roots:
        if is_windows_absolute(root):
            if not is_windows_absolute(path):
                continue
            try:
                relative = PureWindowsPath(ntpath.normpath(path)).relative_to(root)
            except ValueError:
                continue
            result = inside(relative.as_posix())
        else:
            normalized = posixpath.normpath(path)
            if not normalized.startswith(root.rstrip("/") + "/"):
                continue
            result = inside(normalized[len(root.rstrip("/")) + 1:])
        if result is not None:
            return result
    return None


def inside(path: str) -> str | None:
    """"/" 区切りの相対パスに整える。ルートの外を指せば None。"""
    normalized = posixpath.normpath(path)
    if normalized in (".", "..") or normalized.startswith(("../", "/")):
        return None
    return normalized
