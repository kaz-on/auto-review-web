"""投稿する本文のファイルの参照を blob へのリンクにする変換。"""

from __future__ import annotations

from pathlib import Path

import pytest

from webreview.links import linkify

ROOT = Path("/work/org/repo")
LEGACY = Path("/work/repo")
ROOTS = (ROOT, LEGACY)


def blob(path: str, start: int | None, end: int | None) -> str:
    """forge の blob_url の代わり。行範囲の書式は forge ごとに違うので、ここでは素朴に。"""
    lines = "" if start is None else f"#{start}-{end}"
    return f"https://host/blob/SHA/{path}{lines}"


def test_指摘の見出しの絶対パスを行範囲付きのリンクにする():
    text = "- [P1] 合計を誤る — /work/org/repo/src/a.py:40-45"

    assert linkify(text, ROOTS, blob) == (
        "- [P1] 合計を誤る — [src/a.py:40-45](https://host/blob/SHA/src/a.py#40-45)"
    )


def test_行が_1_つなら開始と終了を同じにする():
    assert linkify("/work/org/repo/a.py:7", ROOTS, blob) == (
        "[a.py:7](https://host/blob/SHA/a.py#7-7)"
    )


def test_行の無いパスはファイルへのリンクにして文末の句読点は外に出す():
    assert linkify("詳しくは /work/org/repo/README.md.", ROOTS, blob) == (
        "詳しくは [README.md](https://host/blob/SHA/README.md)."
    )


def test_日本語の句読点でパスを区切る():
    assert linkify("/work/org/repo/a.py、/work/org/repo/b.py。", ROOTS, blob) == (
        "[a.py](https://host/blob/SHA/a.py)、[b.py](https://host/blob/SHA/b.py)。"
    )


def test_オーナーを固定していた頃の作業ツリーのパスも直す():
    assert linkify("/work/repo/a.py:3", ROOTS, blob) == (
        "[a.py:3](https://host/blob/SHA/a.py#3-3)"
    )


def test_オーナーとリポジトリが同名でも今の作業ツリーとして解決する():
    """WORK/<repo> が WORK/<owner>/<repo> の先頭に一致してしまう場合。"""
    roots = (Path("/work/foo/foo"), Path("/work/foo"))

    assert linkify("/work/foo/foo/a.py:1", roots, blob) == (
        "[a.py:1](https://host/blob/SHA/a.py#1-1)"
    )


def test_作業ツリーの外の絶対パスは触らない():
    text = "/etc/passwd:1 と /work/org/other/a.py:2"

    assert linkify(text, ROOTS, blob) == text


def test_ルートの外へ出るパスは触らない():
    text = "/work/org/repo/../secret.txt:1"

    assert linkify(text, ROOTS, blob) == text


def test_相対リンクを行範囲付きの_blob_へのリンクにする():
    text = "[CR-004](AGENTS.md#L14-L15) に反します。"

    assert linkify(text, ROOTS, blob) == (
        "[CR-004](https://host/blob/SHA/AGENTS.md#14-15) に反します。"
    )


@pytest.mark.parametrize("fragment, expected", [
    ("L3", "#3-3"),
    ("L3-5", "#3-5"),
    ("L5-L3", "#3-5"),
])
def test_相対リンクの行の書き方の違いを吸収する(fragment, expected):
    assert linkify(f"[r](src/AGENTS.md#{fragment})", ROOTS, blob) == (
        f"[r](https://host/blob/SHA/src/AGENTS.md{expected})"
    )


def test_行でないアンカーはそのまま付け直す():
    assert linkify("[設計](docs/design.md#データの持ち方)", ROOTS, blob) == (
        "[設計](https://host/blob/SHA/docs/design.md#データの持ち方)"
    )


def test_作業ツリーの絶対パスを指すリンクも直す():
    assert linkify("[a.py](/work/org/repo/a.py#L2)", ROOTS, blob) == (
        "[a.py](https://host/blob/SHA/a.py#2-2)"
    )


def test_リンクの表示文字列の絶対パスは相対に直すだけ():
    assert linkify("[/work/org/repo/a.py:2](a.py#L2)", ROOTS, blob) == (
        "[a.py:2](https://host/blob/SHA/a.py#2-2)"
    )


@pytest.mark.parametrize("link", [
    "[外部](https://example.com/a.py)",
    "[メール](mailto:a@example.com)",
    "[見出し](#要約)",
    "[プロトコル相対](//example.com/a)",
    "[外へ](../other/a.py)",
    "[クエリ](a.py?plain=1)",
    # 作業ツリーの外の絶対パスは何を指すか分からない。
    "[ルート](/AGENTS.md)",
])
def test_リポジトリのファイルを指さないリンクは触らない(link):
    assert linkify(link, ROOTS, blob) == link


def test_表示文字列にインラインコードを含むリンクも変換する():
    assert linkify("[`AGENTS.md`](AGENTS.md#L14-L15)", ROOTS, blob) == (
        "[`AGENTS.md`](https://host/blob/SHA/AGENTS.md#14-15)"
    )


def test_表示文字列にインラインコードを含む絶対パスのリンクも壊さない():
    assert linkify("[`/work/org/repo/a.py`](/work/org/repo/a.py#L2)", ROOTS, blob) == (
        "[`a.py`](https://host/blob/SHA/a.py#2-2)"
    )


def test_リンクの前後のインラインコードはそれぞれ扱う():
    text = "`/work/org/repo/a.py:1` と [b](b.py#L2) と `[c](c.py)`"

    assert linkify(text, ROOTS, blob) == (
        "`a.py:1` と [b](https://host/blob/SHA/b.py#2-2) と `[c](c.py)`"
    )


def test_エンコードされたリンク先はデコードしてから組み立てる():
    """blob_url がエンコードし直すので、そのまま渡すと %20 が %2520 になる。"""
    assert linkify("[設計](docs/design%20notes.md#L2)", ROOTS, blob) == (
        "[設計](https://host/blob/SHA/docs/design notes.md#2-2)"
    )


def test_エンコードしてリポジトリの外を指すリンク先も触らない():
    link = "[外へ](%2e%2e/other/a.py)"

    assert linkify(link, ROOTS, blob) == link


def test_アンカー付きの絶対パスはアンカーをパスに含めない():
    assert linkify("/work/org/repo/a.py#L2", ROOTS, blob) == (
        "[a.py](https://host/blob/SHA/a.py)#L2"
    )


def test_インラインコードの中は相対パスに直すだけ():
    text = "`/work/org/repo/a.py:3` と `[x](a.py)`"

    assert linkify(text, ROOTS, blob) == "`a.py:3` と `[x](a.py)`"


def test_コードブロックの中は相対パスに直すだけ():
    text = "```\n/work/org/repo/a.py:3\n[x](a.py)\n```\n/work/org/repo/a.py:3"

    assert linkify(text, ROOTS, blob) == (
        "```\na.py:3\n[x](a.py)\n```\n[a.py:3](https://host/blob/SHA/a.py#3-3)"
    )


def test_レビューしたコミットが分からなければ相対パスに直すだけ():
    text = "- [P1] t — /work/org/repo/a.py:3\n[CR-004](AGENTS.md#L14-L15)"

    assert linkify(text, ROOTS, None) == "- [P1] t — a.py:3\n[CR-004](AGENTS.md#L14-L15)"


def test_ルートを_1_つだけ渡してもよい():
    assert linkify("/work/org/repo/a.py:3", ROOT, None) == "a.py:3"


# --- Windows で動かしたとき ---------------------------------------------------
WIN_ROOT = Path(r"C:\work\org\repo")
WIN_ROOTS = (WIN_ROOT, Path(r"C:\work\repo"))


def test_windows_の作業ツリーのパスを_スラッシュ区切りのリンクにする():
    text = r"- [P1] 合計を誤る — C:\work\org\repo\src\a.py:40-45"

    assert linkify(text, WIN_ROOTS, blob) == (
        "- [P1] 合計を誤る — [src/a.py:40-45](https://host/blob/SHA/src/a.py#40-45)"
    )


@pytest.mark.parametrize("path", [
    "C:/work/org/repo/src/a.py",
    r"c:\Work\org\repo\src\a.py",
    r"C:\work\org\repo/src\a.py",
])
def test_windows_のパスは区切りと大文字小文字の違いを吸収する(path):
    assert linkify(f"{path}:3", WIN_ROOTS, blob) == (
        "[src/a.py:3](https://host/blob/SHA/src/a.py#3-3)"
    )


def test_windows_でもオーナーを固定していた頃の作業ツリーのパスを直す():
    assert linkify(r"C:\work\repo\a.py の説明。", WIN_ROOTS, blob) == (
        "[a.py](https://host/blob/SHA/a.py) の説明。"
    )


@pytest.mark.parametrize("text", [
    r"C:\work\other\a.py:3",
    r"C:\work\org\repo\..\other\a.py:3",
    r"D:\work\org\repo\a.py:3",
])
def test_windows_でも作業ツリーの外のパスは触らない(text):
    assert linkify(text, WIN_ROOTS, blob) == text


@pytest.mark.parametrize("target", [
    r"C:\work\org\repo\src\a.py#L3",
    "C:/work/org/repo/src/a.py#L3",
    "/C:/work/org/repo/src/a.py#L3",
    "C:%5Cwork%5Corg%5Crepo%5Csrc%5Ca.py#L3",
])
def test_windows_の作業ツリーのパスを指すリンクも直す(target):
    assert linkify(f"[a]({target})", WIN_ROOTS, blob) == (
        "[a](https://host/blob/SHA/src/a.py#3-3)"
    )


def test_windows_の作業ツリーの外を指すリンクは触らない():
    link = r"[外](C:\other\a.py)"

    assert linkify(link, WIN_ROOTS, blob) == link


def test_windows_のパスもコードの中は相対パスに直すだけ():
    assert linkify(r"`C:\work\org\repo\src\a.py:3`", WIN_ROOTS, blob) == "`src/a.py:3`"
