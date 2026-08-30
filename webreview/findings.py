"""codex のレビュー本文から、PR のソース行に付けられる指摘を取り出す。

codex の出力は自由記述なので、書式が読み取れたものだけを行コメントに回し、
読み取れなかったぶんは本文にそのまま残す。どちらに転んでも指摘の内容は投稿される。

拾うのは次の形の箇条書き:

    - [P1] 指摘のタイトル — /work/my-repo/order_service.py:16-18
      指摘の説明。次の箇条書きまでが 1 件ぶん。

パスは codex が動いた作業ツリーの絶対パスで出るため、リポジトリのルート
(REVIEW_WORK_PATH/<repo>) からの相対パスに直す。直せないパスは GitHub 側でも
解決できない (422 になる) ので、行コメントにはせず本文に残す。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

__all__ = ["Finding", "extract"]

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


def extract(body: str, repo_path: Path) -> tuple[str, list[Finding]]:
    """本文を「行コメントにできなかった残り」と「指摘」に分ける。

    残りの本文は元の並びのまま返すので、全体の要約や書式の読めなかった箇条書きは
    レビュー本体のコメントとして投稿される。
    """
    lines = body.splitlines()
    kept: list[str] = []
    findings: list[Finding] = []

    for block in _blocks(lines):
        finding = _parse(block, repo_path)
        if finding is None:
            kept.extend(block)
        else:
            findings.append(finding)

    return _tidy(kept, dropped=bool(findings)), findings


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


def _parse(block: list[str], repo_path: Path) -> Finding | None:
    """1 ブロックを指摘として解釈する。行コメントにできなければ None。"""
    m = _HEADING_RE.match(block[0])
    if not m:
        return None

    path = _relative_path(m.group("path"), repo_path)
    if path is None:
        return None

    start = int(m.group("start"))
    end = int(m.group("end") or start)
    if start < 1 or end < 1:
        return None
    if start > end:
        start, end = end, start

    return Finding(
        path=path,
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


def _relative_path(raw: str, repo_path: Path) -> str | None:
    """作業ツリーのパスをリポジトリルートからの相対パスに直す。

    実際にファイルを見に行かず文字列だけで解決する。投稿はレビューから時間が
    経ってから行われることがあり、そのときクローンが残っている保証はない。
    """
    text = raw.strip().strip("`")
    if not text:
        return None

    path = os.path.normpath(text)
    if os.path.isabs(path):
        path = os.path.relpath(path, os.path.normpath(str(repo_path)))
    # ルートの外を指すパスは PR の差分に対応しない。
    if path.startswith("..") or os.path.isabs(path) or path == ".":
        return None
    return path


def _tidy(lines: list[str], dropped: bool) -> str:
    """残った本文を整える。

    指摘を抜き出したあとには "Full review comments:" のような、中身の無くなった
    見出し行だけが残ることがある。行コメントに回したときだけ、末尾のそれを落とす。
    """
    text = "\n".join(lines).strip()
    if dropped and text.endswith((":", "：")):
        text = text.rsplit("\n", 1)[0].strip() if "\n" in text else ""
    return text
