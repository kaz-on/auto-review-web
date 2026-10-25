"""投稿する本文の中のファイルの参照を、レビューしたコミットの blob へのリンクにする。

codex の本文には 2 種類のファイルの参照が出てくる。

* 作業ツリーの絶対パス (指摘の見出しの "/work/o/r/a.py:40-45" や、説明の中の言及)
* リポジトリのルートからの相対リンク ("[CR-004](AGENTS.md#L14-L15)" のように、
  AGENTS.md のルールを根拠にした指摘に付く)

どちらもそのままでは PR の画面で役に立たない。絶対パスはリンクにならず、相対
リンクは GitHub では PR のページからの相対になって切れ、GitLab と互換のサービスでは
デフォルトブランチを指す (PR で足したファイルは 404、行はその後の push でずれる)。
そこで、どちらもレビューした head のコミットを指す blob の URL に置き換える。

置き換えは投稿の直前にだけ行い、DB の本文は変えない。リンク先のファイルが実在する
かは確かめない (無ければ 404 で分かる)。コードブロックとインラインコードの中は
リンクにできないので、絶対パスを相対パスに直すだけにする。
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Sequence
from pathlib import Path
from urllib.parse import unquote

# (リポジトリからの相対パス, 開始行, 終了行) から blob の URL を作る。行が無ければ None。
BlobUrl = Callable[[str, "int | None", "int | None"], str]

_FENCE_RE = re.compile(r"^\s*(```|~~~)")
_CODE_SPAN_RE = re.compile(r"(`+)(.+?)\1")
# パスに使える文字。空白や括弧、引用符、日本語の句読点で区切る。"#" はリンクの
# アンカー (a.py#L3) の区切りとして扱い、パスには含めない。
_PATH_CHARS = r"[^\s:()\[\]`'\"<>#、。，．）（]+"
_LINE_RANGE = r"(?::(?P<start>\d+)(?:-(?P<end>\d+))?)?"
_LINK_RE = re.compile(r"\[(?P<text>[^\]\n]*)\]\((?P<target>[^)\s]+)\)")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_LINE_ANCHOR_RE = re.compile(r"^L(?P<start>\d+)(?:-L?(?P<end>\d+))?$")


def linkify(text: str, roots: Path | Sequence[Path], blob_url: BlobUrl | None) -> str:
    """text の中のファイルの参照を blob へのリンクにする。

    roots は codex が動いた作業ツリーの候補。blob_url が None (レビューしたコミットが
    分からない古いレビュー) なら、絶対パスを相対パスに直すだけにする。
    """
    rewriter = _Rewriter([roots] if isinstance(roots, Path) else roots, blob_url)
    lines = text.split("\n")
    in_fence = False
    for i, line in enumerate(lines):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        lines[i] = rewriter.code(line) if in_fence else rewriter.line(line)
    return "\n".join(lines)


class _Rewriter:
    def __init__(self, roots: Sequence[Path], blob_url: BlobUrl | None):
        # 長いルートから試す。オーナーを固定していた頃のルート (WORK/<repo>) は、
        # オーナーとリポジトリが同名だと今のルート (WORK/<owner>/<repo>) の先頭に一致する。
        self._roots = sorted(
            {posixpath.normpath(str(root)) for root in roots}, key=len, reverse=True
        )
        self._blob_url = blob_url
        alternatives = "|".join(re.escape(root) for root in self._roots)
        self._abs_re = (
            re.compile(rf"(?:{alternatives})/(?P<path>{_PATH_CHARS}){_LINE_RANGE}")
            if self._roots
            else None
        )

    # --- 行の種類ごと ---------------------------------------------------------
    def code(self, text: str) -> str:
        """コードの中。リンクにできないので、絶対パスを相対パスに直すだけ。"""
        if self._abs_re is None:
            return text
        return self._abs_re.sub(self._relative_text, text)

    def line(self, text: str) -> str:
        """コードブロックの外の 1 行。

        Markdown のリンクとインラインコードは、先に始まるほうを 1 つの単位として
        扱う。リンクの表示文字列にインラインコードが入ること (``[`a.py`](a.py)``)
        も、インラインコードの中にリンクの形が入ること (`` `[x](a.py)` ``) もあり、
        どちらかで先に切り分けると、もう一方が分断されて読めなくなる。
        """
        out: list[str] = []
        pos = 0
        while True:
            link = _LINK_RE.search(text, pos)
            span = _CODE_SPAN_RE.search(text, pos)
            m = min((x for x in (link, span) if x), key=lambda x: x.start(), default=None)
            if m is None:
                break
            out.append(self._bare_paths(text[pos:m.start()]))
            if m is link:
                out.append(self._link(m.group("text"), m.group("target")))
            else:
                out.append(m.group(1) + self.code(m.group(2)) + m.group(1))
            pos = m.end()
        out.append(self._bare_paths(text[pos:]))
        return "".join(out)

    # --- 置き換え ---------------------------------------------------------------
    def _bare_paths(self, text: str) -> str:
        if self._abs_re is None:
            return text
        return self._abs_re.sub(self._path_link, text)

    def _path_link(self, m: re.Match[str]) -> str:
        """作業ツリーの絶対パス → [相対パス:行](blob の URL)。"""
        parsed = _parse_absolute(m)
        if parsed is None:
            return m.group(0)
        relative, label, trailing = parsed
        if self._blob_url is None:
            return label + trailing
        start, end = _lines(m.group("start"), m.group("end"))
        # 文末のピリオドなどはリンクの外に出す。
        return f"[{label}]({self._blob_url(relative, start, end)}){trailing}"

    def _relative_text(self, m: re.Match[str]) -> str:
        """絶対パスの部分だけを相対パスに直した文字列。"""
        parsed = _parse_absolute(m)
        if parsed is None:
            return m.group(0)
        _relative, label, trailing = parsed
        return label + trailing

    def _link(self, label: str, target: str) -> str:
        """Markdown のリンク。リポジトリ内のファイルを指していれば blob の URL にする。"""
        # リンクの中にリンクは置けないので、表示文字列の絶対パスは相対に直すだけ。
        label = self.code(label)
        url = self._target(target)
        return f"[{label}]({url or target})"

    def _target(self, target: str) -> str | None:
        if self._blob_url is None or "?" in target:
            return None
        path, _, fragment = target.partition("#")
        if not path or target.startswith("//") or _SCHEME_RE.match(path):
            return None
        # リンク先は URL なので、空白や日本語はエンコードされている。リポジトリの
        # パスとして扱うためにデコードする (URL にするときに blob_url がエンコードし直す)。
        path = unquote(path)
        if path.startswith("/"):
            # 作業ツリーの絶対パスだけを扱う。それ以外の "/..." は何を指すか分からない。
            relative = self._under_root(path)
        else:
            relative = _inside(path)
        if relative is None:
            return None
        anchor = _LINE_ANCHOR_RE.match(fragment)
        if anchor:
            start, end = _lines(anchor.group("start"), anchor.group("end"))
            return self._blob_url(relative, start, end)
        url = self._blob_url(relative, None, None)
        return f"{url}#{fragment}" if fragment else url

    def _under_root(self, path: str) -> str | None:
        normalized = posixpath.normpath(path)
        for root in self._roots:
            if normalized.startswith(root + "/"):
                return _inside(normalized[len(root) + 1:])
        return None


def _inside(path: str) -> str | None:
    """リポジトリのルートからの相対パスに整える。ルートの外を指せば None。"""
    normalized = posixpath.normpath(path)
    if normalized in (".", "..") or normalized.startswith(("../", "/")):
        return None
    return normalized


def _parse_absolute(m: re.Match[str]) -> tuple[str, str, str] | None:
    """絶対パスの一致を (相対パス, 表示する文字列, 後ろに出す文の区切り) にする。

    表示する文字列は "相対パス:行範囲"。行範囲が無いときだけ、パスの末尾の
    ". ," などを文の区切りとみなして分ける。
    """
    path = m.group("path")
    lines = m.group(0)[m.end("path") - m.start():]
    trailing = ""
    if not lines:
        path, trailing = _split_trailing(path)
    relative = _inside(path)
    if relative is None:
        return None
    return relative, relative + lines, trailing


def _split_trailing(path: str) -> tuple[str, str]:
    """パスの末尾に付いた文の区切り (". ," など) を分ける。"""
    stripped = path.rstrip(".,;!?")
    return stripped, path[len(stripped):]


def _lines(start: str | None, end: str | None) -> tuple[int | None, int | None]:
    if start is None:
        return None, None
    first = int(start)
    last = int(end) if end is not None else first
    return min(first, last), max(first, last)
