"""レビューの指摘を、AI エージェントに渡す依頼文にする。

画面の「エージェント用にコピー」ボタンが使う。レビューの指摘をすべて 1 つの文章に
まとめ、エージェントが手元のリポジトリで続きを調べられるだけの文脈 (PR とそのタイトル・
レビューしたコミット・場所・説明) を載せる。どの指摘を渡すか、何をしてほしいか (相談・修正・調査)
は使う人が貼り付けたあとに編集する前提で、ここでは指示を書かない。特定のエージェントに
依存する書き方もしない。

指摘の読み取りは投稿・集計と同じ (findings.parse)。書式を読み取れなかった箇条書きは
場所が分からないので載せない。
"""

from __future__ import annotations

from . import config, findings, forge, reviews
from .paths import Roots
from .reviews import Review

__all__ = ["handoff"]


def handoff(review: Review) -> str | None:
    """指摘をまとめた依頼文を返す。指摘が 1 件も無ければ None。"""
    items = findings.parse(review.body).items
    if not items:
        return None

    roots = config.review_roots(review.owner, review.repo)
    facts = [f"- PR: {forge.get().pr_url(review.owner, review.repo, review.pr_number)}"]
    # タイトルはキャッシュにあるときだけ載せる (取り直しは一覧の表示に任せる)。
    title = reviews.known_title(review.owner, review.repo, review.pr_number)
    if title:
        facts.append(f"- タイトル: {title}")
    if review.head_commit:
        facts.append(f"- レビューしたコミット: {review.head_commit}")

    parts = ["プルリクエストの自動レビューで、次の指摘がありました。", "\n".join(facts)]
    if review.head_commit:
        # 指摘のあとに PR が更新されていると、行番号や内容が手元のコードとずれる。
        # 使う人は末尾の指摘から消していくことが多いので、注意は前置きに置く。
        parts.append(
            "手元のコードがレビューしたコミットと異なる場合は、現在のコードで確かめてください。"
        )
    for number, item in enumerate(items, 1):
        parts.extend(_finding(number, item, roots))
    return "\n\n".join(parts) + "\n"


def _finding(number: int, item: findings.Item, roots: Roots) -> list[str]:
    """1 件ぶんの段落。番号は、使う人が「1 と 3 を直して」のように指せるように付ける。"""
    heading = f"[{item.priority}] {item.title}" if item.priority else item.title
    # 投稿と同じくリポジトリからの相対パスにする。直せないものは codex が
    # 出したまま渡す (エージェントが読み替えられることもある)。
    resolved = item.resolve(roots)
    path = resolved.path if resolved else item.raw_path
    lines = (
        str(item.start_line)
        if item.start_line == item.end_line
        else f"{item.start_line}-{item.end_line}"
    )
    paragraphs = [f"## {number}. {heading}", f"場所: {path}:{lines}"]
    if item.description:
        paragraphs.append(item.description)
    return paragraphs
