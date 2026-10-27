"""codex のレビュー本文から、PR のソース行に付けられる指摘を取り出す。

codex の出力は自由記述なので、書式が読み取れたものだけを行コメントに回し、
読み取れなかったぶんは本文にそのまま残す。どちらに転んでも指摘の内容は投稿される。

拾うのは次の形の箇条書き:

    - [P1] 指摘のタイトル — /work/my-repo/order_service.py:16-18
      指摘の説明。次の箇条書きまでが 1 件ぶん。

パスは codex が動いた作業ツリーの絶対パスで出るため、リポジトリのルート
(REVIEW_WORK_PATH/<owner>/<repo>) からの相対パスに直す。オーナーを 1 つに固定して
いた頃のレビューは REVIEW_WORK_PATH/<repo> で動いているので、ルートの候補は複数
受け取り、長いものから試して最初に直せたもので解決する (paths)。直せないパスは
GitHub 側でも解決できない (422 になる) ので、行コメントにはせず本文に残す。

集計 (export) は同じ読み取り方 (parse) で指摘を数える。パスの解決は投稿のための
もので、指摘であるかどうかには関わらない。

この箇条書きは codex の出力そのものではなく、codex CLI が JSON の指摘から組み立てた
もの (codex-rs/protocol/src/review_format.rs)。見出しの直後に説明を改行 1 つで続ける
ので、単独の改行を空白として表示するサービスでは 1 行につながる (break_headings)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .paths import Roots, inside, is_windows_absolute, normalize_roots, relative_to_roots

__all__ = ["Finding", "Item", "Parsed", "break_headings", "extract", "parse"]

# 箇条書きの先頭行。優先度と行範囲は省略されることがある。
# タイトルは貪欲に取り、行末の "パス:行" が成立する最後の区切りで切る
# (タイトル自体にダッシュが含まれていても壊れないようにするため)。
_HEADING_RE = re.compile(
    r"^-\s+(?:\[(?P<priority>[^\]]+)\]\s*)?"
    r"(?P<title>.*\S)"
    r"\s+[—–-]\s+"
    r"(?P<path>\S+):(?P<start>\d+)(?:-(?P<end>\d+))?\s*$"
)

# 箇条書きの開始行。書式が読めなくても、ここで区切って本文に残す。
_ITEM_RE = re.compile(r"^-\s+\S")


@dataclass
class Item:
    """書式を読み取れた 1 件の指摘。パスは codex が出したまま (作業ツリーの絶対パス)。"""

    raw_path: str
    start_line: int
    end_line: int
    title: str
    description: str
    priority: str | None = None

    def resolve(self, roots: Roots) -> Finding | None:
        """行コメントにできる形に直す。パスがリポジトリの外を指せば None。"""
        path = _relative_path(self.raw_path, roots)
        if path is None:
            return None
        return Finding(
            path=path,
            start_line=self.start_line,
            end_line=self.end_line,
            title=self.title,
            description=self.description,
            priority=self.priority,
        )


@dataclass
class Parsed:
    items: list[Item]
    # 箇条書きなのに指摘の書式を読み取れなかった数。codex の出力がぶれて指摘を
    # 取りこぼしていないかの目安 (要約の箇条書きなど、指摘でないものも数える)。
    unparsed: int


@dataclass
class Finding:
    """1 件の指摘。行コメントとして投稿できる形まで解決済み。"""

    path: str
    start_line: int
    end_line: int
    title: str
    description: str
    priority: str | None = None

    def comment(self) -> dict[str, Any]:
        """レビュー投稿 API の comments 要素にする。

        side を省略すると GitHub は RIGHT を既定にするが、変更後の行に付けたい
        意図を明示しておく。start_line は複数行にまたがるときだけ送る
        (同じ行を指すと 422 になる)。
        """
        comment: dict[str, Any] = {
            "path": self.path,
            "line": self.end_line,
            "side": "RIGHT",
            "body": self.body(),
        }
        if self.start_line < self.end_line:
            comment["start_line"] = self.start_line
            comment["start_side"] = "RIGHT"
        return comment

    def body(self) -> str:
        """行コメントの本文。見出しを太字にし、説明を続ける。"""
        heading = f"[{self.priority}] {self.title}" if self.priority else self.title
        if not self.description:
            return f"**{heading}**"
        return f"**{heading}**\n\n{self.description}"


def parse(body: str) -> Parsed:
    """本文から書式を読み取れた指摘を取り出す。パスは解決しない。"""
    items: list[Item] = []
    unparsed = 0
    for block in _blocks(body.splitlines()):
        item = _read(block)
        if item is not None:
            items.append(item)
        elif _ITEM_RE.match(block[0]):
            unparsed += 1
    return Parsed(items=items, unparsed=unparsed)


def extract(body: str, roots: Roots) -> tuple[str, list[Finding]]:
    """本文を「行コメントにできなかった残り」と「指摘」に分ける。

    残りの本文は元の並びのまま返すので、全体の要約や書式の読めなかった箇条書きは
    レビュー本体のコメントとして投稿される。
    """
    kept: list[str] = []
    findings: list[Finding] = []

    for block in _blocks(body.splitlines()):
        item = _read(block)
        finding = item.resolve(roots) if item is not None else None
        if finding is None:
            kept.extend(block)
        else:
            findings.append(finding)

    return _tidy(kept, dropped=bool(findings)), findings


def break_headings(body: str) -> str:
    """指摘の見出しの行末に Markdown の改行 (空白 2 つ) を付け、説明と分けて表示させる。

    GitLab や、設定によっては GitHub 互換のサービス (Gitea など) は単独の改行を
    空白として表示する。github.com はもともと改行として表示するので見た目は変わらない。
    説明は codex が Markdown として書いたものなので、その中には手を加えない。
    """
    lines = body.split("\n")
    for i, line in enumerate(lines[:-1]):
        if _HEADING_RE.match(line) and lines[i + 1].strip() and not line.endswith("  "):
            lines[i] = line + "  "
    return "\n".join(lines)


def _blocks(lines: list[str]) -> list[list[str]]:
    """箇条書きの開始行で本文を区切る。先頭の導入部分も 1 ブロックとして扱う。"""
    blocks: list[list[str]] = []
    current: list[str] = []
    for line in lines:
        if _ITEM_RE.match(line) and current:
            blocks.append(current)
            current = []
        current.append(line)
    if current:
        blocks.append(current)
    return blocks


def _read(block: list[str]) -> Item | None:
    """1 ブロックを指摘として解釈する。書式が読めなければ None。"""
    m = _HEADING_RE.match(block[0])
    if not m:
        return None

    start = int(m.group("start"))
    end = int(m.group("end") or start)
    if start < 1 or end < 1:
        return None
    if start > end:
        start, end = end, start

    return Item(
        raw_path=m.group("path"),
        start_line=start,
        end_line=end,
        title=m.group("title").strip(),
        description=_description(block[1:]),
        priority=(m.group("priority") or "").strip() or None,
    )


def _description(lines: list[str]) -> str:
    """見出しに続く説明行を、箇条書きのぶんの字下げを外して返す。"""
    body = list(lines)
    while body and not body[0].strip():
        body.pop(0)
    while body and not body[-1].strip():
        body.pop()
    if not body:
        return ""

    indent = min(
        (len(line) - len(line.lstrip()) for line in body if line.strip()),
        default=0,
    )
    return "\n".join(line[indent:] if line.strip() else "" for line in body)


def _relative_path(raw: str, roots: Roots) -> str | None:
    """作業ツリーのパスをリポジトリルートからの相対パスに直す。

    実際にファイルを見に行かず文字列だけで解決する。投稿はレビューから時間が
    経ってから行われることがあり、そのときクローンが残っている保証はない。
    """
    text = raw.strip().strip("`")
    if not text:
        return None

    normalized = normalize_roots(roots)
    if is_windows_absolute(text) or text.startswith("/"):
        return relative_to_roots(text, normalized)
    if any(is_windows_absolute(root) for root in normalized):
        # Windows で動いたレビューなら "\" は区切り。
        text = text.replace("\\", "/")
    return inside(text)


def _tidy(lines: list[str], dropped: bool) -> str:
    """残った本文を整える。

    指摘を抜き出したあとには "Full review comments:" のような、中身の無くなった
    見出し行だけが残ることがある。行コメントに回したときだけ、末尾のそれを落とす。
    """
    text = "\n".join(lines).strip()
    if dropped and text.endswith((":", "：")):
        text = text.rsplit("\n", 1)[0].strip() if "\n" in text else ""
    return text
