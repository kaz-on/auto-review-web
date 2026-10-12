"""レビュー本文から行コメントを取り出す部分。

codex の出力は自由記述なので、読めた指摘だけを行コメントに回し、読めなかった
ぶんは本文に残す — その振り分けが要。書式が崩れても内容を落とさないことを見る。
"""

from __future__ import annotations

from pathlib import Path

from webreview import findings

REPO = Path("/work/my-repo")

SAMPLE = """\
注文合計と期限判定に、通常の入力で誤った結果を返す不具合があります。

Full review comments:

- [P1] 数量分の価格を加算する — /work/my-repo/order_service.py:16-16
  数量1の商品が無料になります。
  小計は `price * quantity` で計算してください。

- [P2] 読み込み後にファイルを確実に閉じる — /work/my-repo/store/orders.py:57-58
  `with open(...) as file:` を使用してください。
"""


def test_サンプルの書式から指摘を取り出す():
    remaining, found = findings.extract(SAMPLE, REPO)

    assert [(f.path, f.start_line, f.end_line, f.priority) for f in found] == [
        ("order_service.py", 16, 16, "P1"),
        ("store/orders.py", 57, 58, "P2"),
    ]
    assert found[0].title == "数量分の価格を加算する"
    assert found[0].description == (
        "数量1の商品が無料になります。\n小計は `price * quantity` で計算してください。"
    )
    # 全体の要約は本文として残す。中身が無くなった見出し行は落とす。
    assert remaining == "注文合計と期限判定に、通常の入力で誤った結果を返す不具合があります。"


def test_単一行の指摘には_start_line_を付けない():
    """同じ行を start_line と line に入れると GitHub が 422 を返す。"""
    _, found = findings.extract(
        "- [P1] 指摘 — /work/my-repo/a.py:16-16\n  説明\n", REPO
    )

    assert found[0].comment() == {
        "path": "a.py",
        "line": 16,
        "side": "RIGHT",
        "body": "**[P1] 指摘**\n\n説明",
    }


def test_複数行の指摘は範囲として送る():
    _, found = findings.extract("- [P2] 指摘 — /work/my-repo/a.py:57-58\n", REPO)

    assert found[0].comment() == {
        "path": "a.py",
        "line": 58,
        "side": "RIGHT",
        "body": "**[P2] 指摘**",
        "start_line": 57,
        "start_side": "RIGHT",
    }


def test_行範囲が省略されていればその_1_行を指す():
    _, found = findings.extract("- 指摘 — /work/my-repo/a.py:42\n", REPO)

    assert (found[0].start_line, found[0].end_line) == (42, 42)
    # 優先度が無ければタイトルだけを太字にする。
    assert found[0].comment()["body"] == "**指摘**"


def test_行範囲が逆順でも投稿できる形にする():
    _, found = findings.extract("- 指摘 — /work/my-repo/a.py:58-57\n", REPO)

    assert (found[0].start_line, found[0].end_line) == (57, 58)


def test_タイトルにダッシュが含まれても位置だけを切り出す():
    _, found = findings.extract(
        "- [P1] タイムアウト — 再試行の間隔を空ける — /work/my-repo/a.py:3\n", REPO
    )

    assert found[0].title == "タイムアウト — 再試行の間隔を空ける"
    assert found[0].path == "a.py"


# --- 行コメントにできないもの ------------------------------------------------
def test_作業ツリーの外を指すパスは本文に残す():
    """PR の差分に対応しないので、投稿しても 422 になる。"""
    body = "- [P1] 指摘 — /etc/passwd:1\n  説明\n"

    remaining, found = findings.extract(body, REPO)

    assert found == []
    assert remaining == body.strip()


def test_位置の書式が読めない箇条書きは本文に残す():
    body = "要約\n\n- [P1] 全体的にテストが足りません\n- [P2] 命名を揃えてください\n"

    remaining, found = findings.extract(body, REPO)

    assert found == []
    assert remaining == body.strip()


def test_読めた指摘と読めない指摘が混ざっていても取りこぼさない():
    body = (
        "要約\n\n"
        "- [P1] 位置のある指摘 — /work/my-repo/a.py:10\n  説明\n\n"
        "- [P2] 位置のない指摘\n  こちらは本文に残す。\n"
    )

    remaining, found = findings.extract(body, REPO)

    assert [f.path for f in found] == ["a.py"]
    assert "位置のない指摘" in remaining
    assert "位置のある指摘" not in remaining


def test_指摘が無ければ本文をそのまま返す():
    """末尾がコロンで終わる本文を、指摘の抜き出しと無関係に削らない。"""
    body = "レビューの結果は次のとおり:"

    remaining, found = findings.extract(body, REPO)

    assert (remaining, found) == (body, [])


def test_空の本文でも壊れない():
    assert findings.extract("", REPO) == ("", [])


# --- パスの解決 --------------------------------------------------------------
def test_相対パスはそのまま使う():
    """codex がリポジトリ基準で出力することもある。"""
    _, found = findings.extract("- 指摘 — ./src/a.py:1\n", REPO)

    assert found[0].path == "src/a.py"


def test_リポジトリのルートより上は指せない():
    _, found = findings.extract("- 指摘 — ../other/a.py:1\n", REPO)

    assert found == []


def test_余分な区切りを含むパスも解決する():
    _, found = findings.extract("- 指摘 — /work/my-repo/./src//a.py:1\n", REPO)

    assert found[0].path == "src/a.py"


# --- 集計用の読み取り -------------------------------------------------------
def test_集計用の読み取りはパスを解決せずに指摘を数える():
    parsed = findings.parse(SAMPLE)

    assert [(i.raw_path, i.priority) for i in parsed.items] == [
        ("/work/my-repo/order_service.py", "P1"),
        ("/work/my-repo/store/orders.py", "P2"),
    ]
    assert parsed.unparsed == 0


def test_リポジトリの外を指す指摘も集計では数える():
    """行コメントにできるかどうかは投稿の都合で、指摘であることは変わらない。"""
    body = "- [P1] 外を指す — /elsewhere/a.py:1\n"

    assert len(findings.parse(body).items) == 1
    assert findings.extract(body, REPO)[1] == []


def test_書式を読めない箇条書きの数を返す():
    body = (
        "要約\n\n"
        "- [P1] 読める — /work/my-repo/a.py:1\n"
        "- [P2] 位置が無い指摘\n"
        "- もう一つ\n"
    )

    parsed = findings.parse(body)

    assert len(parsed.items) == 1
    # 先頭の導入部分は箇条書きではないので数えない。
    assert parsed.unparsed == 2


def test_読み取った指摘は行コメントの形に直せる():
    item = findings.parse(SAMPLE).items[1]

    finding = item.resolve(REPO)

    assert (finding.path, finding.start_line, finding.end_line) == ("store/orders.py", 57, 58)
    assert finding.title == item.title
