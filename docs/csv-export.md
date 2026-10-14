# CSV エクスポート

レビューと指摘の一覧を CSV で書き出す機能の詳細。使い方の全体は [README](../README.md) を参照。

集計そのものはアプリでは行わず、CSV をダウンロードして別のスクリプトや表計算で集計する。集計タブの「CSV ダウンロード」から、そのときの絞り込み条件（リポジトリ・本文検索）で書き出せる。件数の上限は無く、古い順に並ぶ。

- 文字コードは BOM 付きの UTF-8 で、Excel でそのまま開ける。pandas なら `pd.read_csv(path, encoding="utf-8-sig")` で読める。
- 日時（`created_at`）は UTC の ISO 8601（`2026-10-12T03:04:05Z`）。
- ファイル名には書き出した日時が UTC で付く（`reviews-20261012-030405Z.csv`）。
- 真偽値（`is_latest_rev` / `include_quality`）は `1` / `0`。値が無いところは空欄。

## `reviews.csv`（レビュー 1 件 = 1 行）

| 列 | 内容 |
| --- | --- |
| `review_id` / `repo` / `pr_number` / `pr_url` / `pr_title` | レビューと PR。タイトルは集計タブと同じキャッシュから引く（未取得なら空欄） |
| `pr_author` | PR の作成者（GitHub のログイン名） |
| `rev` / `is_latest_rev` | 同じ PR の何回目のレビューか / その PR の最新のレビューか |
| `created_at` | レビューした日時 |
| `model` / `reasoning_effort` / `include_quality` | 実行時の設定 |
| `pr_additions` / `pr_deletions` / `pr_changed_files` | レビューした差分の規模 |
| `findings` | 指摘の件数 |
| `findings_p0` 〜 `findings_p3` / `findings_other` | 優先度ごとの内訳（優先度が無い・それ以外の表記は `other`） |
| `unparsed_items` | 指摘の書式を読み取れなかった箇条書きの数（下記） |

## `findings.csv`（指摘 1 件 = 1 行）

`review_id` / `repo` / `pr_number` / `pr_author` / `rev` / `is_latest_rev` / `created_at` / `include_quality` / `priority` / `title` / `path` / `start_line` / `end_line` / `description`。

`path` はリポジトリのルートからの相対パス。`description` は複数行になることがある（CSV の規則どおりに引用符で囲む）。指摘の無いレビューは行を出さないので、0 件のレビューも数えたいときは `reviews.csv` を使う。

## 指摘の数え方

PR への投稿と同じく、`- [P1] タイトル — パス:行` の形の箇条書きを 1 件の指摘として数える（[PR への投稿の形](design.md#pr-への投稿の形)）。投稿では位置を解決できない指摘を本文に残すが、集計ではそれも指摘として数える。

書式を読み取れなかった箇条書きは `unparsed_items` に出す。要約の中の箇条書きなど指摘でないものも含むが、多いレビューは codex の出力がぶれて指摘を取りこぼしている可能性がある。

## 集計するときの注意

- **同じ PR に複数のレビューがある。** やり直したレビューも 1 行ずつ出すので、PR 単位で数えるなら `is_latest_rev == 1` で絞るなど、どれを使うかを決める。
- **実行条件で指摘の数の基準が変わる。** 品質改善を含めた実行（`include_quality == 1`）は指摘が大きく増える。モデルや reasoning effort も同様。人や期間で比べるときはこれらで分けて集計する。
- **指摘の数は変更の規模にも比例する。** 必要なら `pr_additions + pr_deletions` などで割った値も見る。
- **作成者や規模が空欄のレビューがある。** 記録を始める前のレビューのうち、head コミットを記録していない古いものや、変更ファイルが多すぎて規模を求められなかったものは空欄のまま。規模で割るときは空欄を除く。
- **PR のタイトルや指摘の文面は信頼できない入力として扱う。** fork からの PR のタイトルや codex の出力がそのまま入る。`=` などで始まる値は Excel が数式として解釈しうるので、見慣れない値のセルは不用意に編集・実行しない。
