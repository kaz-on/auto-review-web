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

import re
from collections.abc import Callable
from urllib.parse import unquote

from .paths import Roots, inside, is_windows_absolute, normalize_roots, relative_to_roots

# (リポジトリからの相対パス, 開始行, 終了行) から blob の URL を作る。行が無ければ None。
BlobUrl = Callable[[str, "int | None", "int | None"], str]

# コードブロックの開始・終了の行。引用の中 ("> ```sh") のものも含める。
_FENCE_RE = re.compile(r"^(?:\s*>)*\s*(```|~~~)")
_CODE_SPAN_RE = re.compile(r"(`+)(.+?)\1")
# パスに使える文字。空白や引用符、日本語の句読点で区切る。"#" はリンクの
# アンカー (a.py#L3) の区切りとして扱い、パスには含めない。括弧はディレクトリ名に
# 使われる ("src/[id]/(group)/a.py") のでパスに含め、対応の取れない閉じ括弧
# ("(/work/o/r/a.py)" の ")") で切る (_balanced_length)。
_PATH_CHARS = r"[^\s:`'\"<>#、。，．）（]+"
_LINE_RANGE = r"(?::(?P<start>\d+)(?:-(?P<end>\d+))?)?"
# Markdown のリンク。CommonMark と同じく、表示文字列の中の対応の取れた角括弧と、
# リンク先の中の対応の取れた丸括弧を許す (どちらも 1 段の入れ子まで)。リンク先は
# "<...>" で囲んだ形 (空白や括弧をそのまま書ける) も読む。
_LINK_RE = re.compile(
    r"\[(?P<text>(?:[^\[\]\n]|\[[^\[\]\n]*\])*)\]"
    r"\((?:<(?P<angled>[^<>\n]*)>|(?P<target>(?:[^()\s]|\([^()\s]*\))+))\)"
)
_CLOSING = {")": "(", "]": "["}
# 本文に出すパスの中で、Markdown の書式として読まれてしまう文字。
_MARKDOWN_SPECIAL_RE = re.compile(r"([\\`*_\[\]<>~|])")
# "/C:/work/..." のように、Windows のパスを URL のパスの形で書いたもの。
_SLASHED_DRIVE_RE = re.compile(r"^/[A-Za-z]:[\\/]")
_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_LINE_ANCHOR_RE = re.compile(r"^L(?P<start>\d+)(?:-L?(?P<end>\d+))?$")


def linkify(text: str, roots: Roots, blob_url: BlobUrl | None) -> str:
    """text の中のファイルの参照を blob へのリンクにする。

    roots は codex が動いた作業ツリーの候補。blob_url が None (レビューしたコミットが
    分からない古いレビュー) なら、絶対パスを相対パスに直すだけにする。
    """
    rewriter = _Rewriter(roots, blob_url)
    lines = text.split("\n")
    in_fence = False
    for i, line in enumerate(lines):
        if _FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        lines[i] = rewriter.code(line) if in_fence else rewriter.line(line)
    return "\n".join(lines)


class _Rewriter:
    def __init__(self, roots: Roots, blob_url: BlobUrl | None):
        # 長いルートから試す (正規表現の選択肢も前から一致を試す)。
        self._roots = normalize_roots(roots)
        self._blob_url = blob_url
        alternatives = "|".join(_root_pattern(root) for root in self._roots)
        self._abs_re = (
            re.compile(rf"(?:{alternatives})(?P<path>{_PATH_CHARS}){_LINE_RANGE}")
            if self._roots
            else None
        )

    # --- 行の種類ごと ---------------------------------------------------------
    def code(self, text: str) -> str:
        """コードの中。リンクにできないので、絶対パスを相対パスに直すだけ。"""
        out: list[str] = []
        pos = 0
        while (path := self._find_absolute(text, pos)) is not None:
            out.append(text[pos:path.start()] + path.label + path.trailing)
            pos = path.end()
        out.append(text[pos:])
        return "".join(out)

    def line(self, text: str) -> str:
        """コードブロックの外の 1 行。

        Markdown のリンクとインラインコードは、先に始まるほうを 1 つの単位として
        扱う。リンクの表示文字列にインラインコードが入ること (``[`a.py`](a.py)``)
        も、インラインコードの中にリンクの形が入ること (`` `[x](a.py)` ``) もあり、
        どちらかで先に切り分けると、もう一方が分断されて読めなくなる。

        作業ツリーの絶対パスも同じように 1 つの単位として扱う。パスの中の
        "[id](group)" がリンクの形に見えても、先に始まるパスのほうを取る。
        """
        out: list[str] = []
        pos = 0
        while True:
            link = _LINK_RE.search(text, pos)
            span = _CODE_SPAN_RE.search(text, pos)
            path = self._find_absolute(text, pos)
            m = min(
                (x for x in (link, span, path) if x),
                key=lambda x: x.start(),
                default=None,
            )
            if m is None:
                break
            out.append(text[pos:m.start()])
            if isinstance(m, _AbsolutePath):
                out.append(self._path_link(m))
            elif m is link:
                out.append(self._link(m))
            else:
                out.append(m.group(1) + self.code(m.group(2)) + m.group(1))
            pos = m.end()
        out.append(text[pos:])
        return "".join(out)

    # --- 置き換え ---------------------------------------------------------------
    def _find_absolute(self, text: str, pos: int) -> _AbsolutePath | None:
        """text[pos:] で最初に現れる、作業ツリーの中を指す絶対パス。"""
        if self._abs_re is None:
            return None
        while (m := self._abs_re.search(text, pos)) is not None:
            parsed = _parse_absolute(m)
            if parsed is not None:
                return parsed
            pos = m.end()
        return None

    def _path_link(self, path: _AbsolutePath) -> str:
        """作業ツリーの絶対パス → [相対パス:行](blob の URL)。"""
        # パスの "_" や "[" が強調やリンクの書式として読まれないようにする。
        label = _MARKDOWN_SPECIAL_RE.sub(r"\\\1", path.label)
        if self._blob_url is None:
            return label + path.trailing
        url = _markdown_url(self._blob_url(path.relative, path.first, path.last))
        # 文末のピリオドなどはリンクの外に出す。
        return f"[{label}]({url}){path.trailing}"

    def _link(self, m: re.Match[str]) -> str:
        """Markdown のリンク。リポジトリ内のファイルを指していれば blob の URL にする。"""
        angled = m.group("angled")
        target = angled if angled is not None else m.group("target")
        # リンクの中にリンクは置けないので、表示文字列の絶対パスは相対に直すだけ。
        label = self.code(m.group("text"))
        url = self._target(target)
        if url is None:
            return f"[{label}]({target})" if angled is None else f"[{label}](<{target}>)"
        return f"[{label}]({_markdown_url(url)})"

    def _target(self, target: str) -> str | None:
        if self._blob_url is None or "?" in target:
            return None
        path, _, fragment = target.partition("#")
        # リンク先は URL なので、空白や日本語はエンコードされている。リポジトリの
        # パスとして扱うためにデコードする (URL にするときに blob_url がエンコードし直す)。
        path = unquote(path)
        if _SLASHED_DRIVE_RE.match(path):
            path = path[1:]
        if is_windows_absolute(path) or (path.startswith("/") and not path.startswith("//")):
            # 作業ツリーの絶対パスだけを扱う。それ以外の "/..." は何を指すか分からない。
            # "C:" は URL のスキームの形でもあるので、スキームより先に見る。
            relative = relative_to_roots(path, self._roots)
        elif not path or target.startswith("//") or _SCHEME_RE.match(path):
            return None
        else:
            relative = inside(path)
        if relative is None:
            return None
        anchor = _LINE_ANCHOR_RE.match(fragment)
        if anchor:
            start, end = _lines(anchor.group("start"), anchor.group("end"))
            return self._blob_url(relative, start, end)
        url = self._blob_url(relative, None, None)
        return f"{url}#{fragment}" if fragment else url


def _root_pattern(root: str) -> str:
    """ルートとその直後の区切りに一致する正規表現。

    Windows のルートは区切りの "\\" と "/" を区別せず、大文字小文字も区別しない。
    """
    if not is_windows_absolute(root):
        return re.escape(root.rstrip("/")) + "/"
    parts = re.split(r"[\\/]", root.rstrip("\\/"))
    return "(?i:" + r"[\\/]".join(map(re.escape, parts)) + r"[\\/])"


class _AbsolutePath:
    """本文の中の、作業ツリーの絶対パスの 1 か所。

    start() と end() は本文の中の範囲 (後ろに出す文の区切りも含む)。label は
    表示する "相対パス:行範囲"、trailing はパスの末尾から分けた ". ," などの文の区切り。
    """

    def __init__(
        self, start: int, end: int, relative: str, label: str, trailing: str,
        first: int | None, last: int | None,
    ):
        self._start = start
        self._end = end
        self.relative = relative
        self.label = label
        self.trailing = trailing
        self.first = first
        self.last = last

    def start(self) -> int:
        return self._start

    def end(self) -> int:
        return self._end


def _parse_absolute(m: re.Match[str]) -> _AbsolutePath | None:
    """絶対パスの一致を読む。作業ツリーの外を指せば None。

    パスに対応の取れない閉じ括弧があれば、その手前までをパスとみなす
    ("(/work/o/r/a.py)" の ")" は文の括弧)。行範囲が無いときだけ、パスの末尾の
    ". ," などを文の区切りとみなして分ける。
    """
    raw = m.group("path")
    length = _balanced_length(raw)
    if length < len(raw):
        # 閉じ括弧から後ろは本文に残す (行範囲もその後ろにあるので使わない)。
        raw = raw[:length]
        lines, first, last = "", None, None
        end = m.start("path") + length
    else:
        lines = m.group(0)[m.end("path") - m.start():]
        first, last = _lines(m.group("start"), m.group("end"))
        end = m.end()
    path = raw
    if is_windows_absolute(m.group(0)):
        # Windows のルートの下なら "\" は区切り。
        path = path.replace("\\", "/")
    trailing = ""
    if not lines:
        path, trailing = _split_trailing(path)
    relative = inside(path)
    if relative is None:
        return None
    return _AbsolutePath(m.start(), end, relative, relative + lines, trailing, first, last)


def _balanced_length(path: str) -> int:
    """path の先頭から、対応の取れない閉じ括弧の手前までの長さ。"""
    stack: list[str] = []
    for i, char in enumerate(path):
        if char in "([":
            stack.append(char)
        elif char in _CLOSING:
            if not stack or stack[-1] != _CLOSING[char]:
                return i
            stack.pop()
    return len(path)


def _markdown_url(url: str) -> str:
    """Markdown のリンク先に置く URL。括弧はリンクの終わりと読まれないよう符号化する。"""
    return url.replace("(", "%28").replace(")", "%29")


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
