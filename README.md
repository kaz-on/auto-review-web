# auto-review-web

LAN 内の PC からブラウザで使える、[Codex CLI](https://github.com/openai/codex)（OpenAI）による PR レビューの実行・投稿・集計ツール。

Codex の実行には OpenAI の API キーが必要で、レビューのたびに API 料金がかかる。

レビューの対象は GitHub（github.com）のプルリクエストだけ。GitLab などほかのサービスや GitHub Enterprise Server のリポジトリ、PR になっていないブランチやコミットはレビューできない。

このアプリケーションは Claude Code を利用して作成されている。

## できること

- ブラウザから PR の URL を入力してレビューを実行できる。
- レビューの **待機中 / 実行中 / 完了 / 失敗** の状態と進捗ログをリアルタイムに確認できる。
- レビュー完了後に **自動的に PR へ投稿** できる（既定でON。フォームのチェックで切替可）。
- バグだけでなく **書き方・品質の改善** も指摘対象に含められる（既定でON。フォームのチェックで切替可。下記「品質改善の指摘」）。
- 指摘は **ソース行へのコメント** として投稿する（位置を読み取れなかったぶんは本文にまとめる）。
- 自動投稿をOFFにした場合や自動投稿に失敗した場合は、ボタンから手動で投稿できる。
- 終了したレビューは履歴から削除して一覧を整理できる（ログファイルは残る）。
- 一覧には直近 20 件を表示し、「さらに表示」で古いものを読み込める。終了から 30 日を過ぎたジョブは履歴から自動で削除する（`REVIEW_JOB_RETENTION_DAYS`）。失敗したジョブと未投稿のレビューは、同じ PR をやり直さない限り残す。どちらの削除でもレビュー本体は消えず、集計タブから見られる。
- 過去のレビューを一覧・検索できる（リポジトリでの絞り込みと本文検索に対応）。
- レビューと指摘の一覧を CSV でダウンロードし、誰の PR にどれだけ指摘があったかなどを別のスクリプトや表計算で集計できる（下記「CSV エクスポート」）。
- エラーが発生した場合は画面にエラーメッセージを表示する。

## セキュリティ上の注意

**このツールには認証の仕組みがない。** 画面と API に届く人は誰でも、レビューの投入・PR への投稿・履歴の削除ができる。PR への投稿は設定した GitHub の認証情報（PAT ならそのアカウント）の名義で行われ、Codex の API 料金も設定したキーに課金される。

- 信頼できる LAN 内だけで使い、インターネットには公開しない。外から使う場合は、認証付きのリバースプロキシや VPN の後ろに置く。
- 既定では実行している PC 自身（`127.0.0.1`）からしか接続できない。LAN 内のほかの PC から使う場合は `REVIEW_HOST=0.0.0.0` にする（docker compose でもホストで直接起動する場合でも同じ）。
- レビューできるのは `REVIEW_OWNER` のリポジトリへの PR に限られる。ただし codex は PR のコードや説明文を読むので、fork から出された PR のように信頼できない人が書いた内容は、レビュー結果に影響しうる（プロンプトインジェクション）。codex は read-only のサンドボックスで動かしており、作業ツリーを書き換えることはない。

## 動作要件

- **Docker で起動する場合（推奨）**: Docker / Docker Compose のみ。`git` / `codex` はイメージに含まれる。
- **ホストで直接起動する場合**: Python 3.10 以上、および `git` / `codex` が導入済みであること。

## GitHub 認証

`GITHUB_AUTH_MODE` で 2 方式を選べる。

### `pat`（既定）

`GH_TOKEN` に Personal Access Token を設定する。repo の読み取りと PR コメントの権限が必要。

### `app`

GitHub App の秘密鍵から installation access token を都度発行する。App には **Contents = Read / Pull requests = Read and write / Metadata = Read** の権限を与え、`REVIEW_OWNER` のアカウント（Organization または個人アカウント）にインストールしておく。

```ini
GITHUB_AUTH_MODE=app
GITHUB_APP_CLIENT_ID=Iv23li...              # App の Client ID（App ID でも可）
GITHUB_APP_PRIVATE_KEY_PATH=/run/secrets/github-app.pem
GITHUB_APP_INSTALLATION_ID=                 # 省略時は REVIEW_OWNER から自動解決
```

秘密鍵（`.pem`）はリポジトリ直下の `secrets/` に置く。docker compose がこれを `/run/secrets` へ read-only でマウントする（`secrets/` は `.gitignore` 済み）。ファイルではなく PEM 文字列を直接渡したい場合は `GITHUB_APP_PRIVATE_KEY` を使う（改行は `\n` で書ける）。

installation access token は 1 時間で失効するため、**GitHub にアクセスする直前に取得し、残り有効期間が 5 分を切ったら再発行する**（`GITHUB_TOKEN_REFRESH_MARGIN` で調整可）。起動時やジョブ開始時にまとめて取得する方式は、常駐サーバや「完了から時間が経ってからの手動投稿」で失効するため採っていない。

トークンは実行のたびに subprocess の環境変数として渡され、git には環境変数を読む credential helper 経由で渡る。ホストやイメージに認証設定は残らず、コマンドラインや進捗ログにトークンが現れることもない。

## 起動方法A: Docker Compose（推奨）

ホストに `git` / `codex` を入れなくても、Docker だけで起動できる。Codex CLI はイメージに含まれ、認証は環境変数で渡す（ホストの認証ディレクトリは共有しない）。

```bash
cp .env.example .env
# .env に GitHub 認証（GH_TOKEN もしくは GITHUB_APP_*）、CODEX_API_KEY、REVIEW_OWNER を設定する
docker compose up -d --build
```

起動後、ブラウザから `http://localhost:8080/` でアクセスする。LAN 内のほかの PC から使う場合は、`.env` に `REVIEW_HOST=0.0.0.0` を設定して `http://<実行PCのIP>:8080/` でアクセスする（上記「セキュリティ上の注意」）。

- 認証: 上記「GitHub 認証」の設定と `CODEX_API_KEY`（Codex CLI）。いずれも `.env` から環境変数として渡す。
- 永続化: git リポジトリ（`review_repos`）と、DB・codex の生ログ（`review_logs`）は named volume。ジョブ履歴・レビュー本文・PR タイトルはすべて DB に入る。
- 停止 / ログ確認:

  ```bash
  docker compose down          # 停止（volume は保持）
  docker compose logs -f       # ログ確認
  docker compose down -v       # volume ごと削除（履歴・クローンも消える）
  ```

## 起動方法B: ホストで直接起動

レビューを実行する PC に以下が導入済みであること。

- Python 3.10 以上
- `git` / `codex`

認証は Docker の場合と同じ環境変数で渡す（`GITHUB_AUTH_MODE` ほか）。`run.sh` はリポジトリ直下に `.env` があれば読んで export するので、設定は docker compose と同じ 1 ファイルで済む。

```bash
cp .env.example .env   # 初回のみ。REVIEW_OWNER などを埋める
./run.sh
```

`.env` が無ければ読み込みを飛ばすだけなので、環境変数を自分で export して起動してもよい。両方ある場合は `.env` の値が勝つ。

```bash
export REVIEW_OWNER=<your-org-or-user>
./run.sh
```

`REVIEW_OWNER` が未設定のまま起動すると、サーバーは立ち上がるがレビューはすべて拒否される（起動ログに `[warn]` が出る）。

初回は自動で仮想環境を作成し、依存（Flask）をインストールする。起動後、ブラウザから `http://localhost:8080/` でアクセスする。LAN 内のほかの PC から使う場合は `REVIEW_HOST=0.0.0.0` を設定する（Docker の場合と同じ）。

手動で起動する場合:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m webreview
```

## 同時実行の扱い

レビューは共有の作業ディレクトリで git を操作するため、**単一ワーカーのキュー方式** を採る。

- レビューは投入順に **1 件ずつ直列** に実行される（計算リソースを使いすぎない）。
- 実行中に別の URL を投入するとキューに追加され、「実行中」バナーと各ジョブの状態表示でユーザーが状況を認識できる。
- 作業ディレクトリの git 操作も直列化されるため競合しない。

## 設定（環境変数で上書き可能）

| 環境変数 | 既定値 | 説明 |
| --- | --- | --- |
| `GITHUB_AUTH_MODE` | `pat` | GitHub 認証方式（`pat` / `app`） |
| `GH_TOKEN` | （空） | `pat` モードの Personal Access Token |
| `GITHUB_APP_CLIENT_ID` | （空） | `app` モードの Client ID（App ID も可） |
| `GITHUB_APP_PRIVATE_KEY_PATH` | （空） | `app` モードの秘密鍵ファイル |
| `GITHUB_APP_PRIVATE_KEY` | （空） | 秘密鍵を PEM 文字列で渡す場合 |
| `GITHUB_APP_INSTALLATION_ID` | （空） | 省略時は `REVIEW_OWNER` から自動解決 |
| `GITHUB_TOKEN_REFRESH_MARGIN` | `300` | トークン再発行の閾値（秒） |
| `GITHUB_API_URL` | `https://api.github.com` | REST API のベース URL。通常は変更不要（PR や clone の URL は github.com 固定のため、変えても GitHub Enterprise Server には対応しない） |
| `REVIEW_OWNER` | （空） | **必須。** 許可するリポジトリのオーナー（Organization 名または個人アカウント名） |
| `REVIEW_MODEL` | `gpt-6.1-sol` | codex のレビューモデル |
| `REVIEW_REASONING_EFFORT` | `medium` | 推論の深さ（`minimal` / `low` / `medium` / `high`）。空でモデル既定 |
| `REVIEW_DESCRIPTION` | （空） | 画面上部に表示する説明文（下記「説明文の書式」） |
| `REVIEW_TIMEOUT` | `3600` | codex 1 回あたりの制限時間（秒）。`0` で無制限 |
| `REVIEW_STDERR_RETENTION_DAYS` | `30` | codex 生ログの保持日数。`0` で削除しない |
| `REVIEW_JOB_RETENTION_DAYS` | `30` | 終了したジョブを一覧から自動削除するまでの日数。失敗・未投稿のものは残す。`0` で削除しない |
| `REVIEW_WORK_PATH` | `data/work` | リポジトリの作業ディレクトリ |
| `REVIEW_LOGS_PATH` | `data/review_logs` | codex の生ログのルート |
| `REVIEW_DB_PATH` | `data/review.db` | SQLite データベースの位置 |
| `REVIEW_HOST` | `127.0.0.1` | 待ち受けアドレス。LAN に公開するなら `0.0.0.0` |
| `REVIEW_PORT` | `8080` | 待ち受けポート |
| `REVIEW_GIT_BIN` / `REVIEW_CODEX_BIN` | `git` / `codex` | 各コマンドのパス |

`codex` には上記に加えて `--sandbox read-only --ask-for-approval never --config model_reasoning_summary=none` を固定で渡す。非対話実行で承認待ちに固まらず、作業ツリーを書き換えず、推論サマリをレビュー本文に混ぜないため。

## 説明文の書式（`REVIEW_DESCRIPTION`）

`REVIEW_DESCRIPTION` を設定すると、画面上部（オーナー / モデル表示の下）に説明欄が出る。レビューの概要や説明ページへのリンクを載せる用途を想定している。未設定なら欄そのものが表示されない。

`.env` の値は 1 行でしか書けないため、**リテラルの `\n` を改行として解釈する**。書式は最小限の Markdown 風で、次の 4 つだけをサポートする。

| 書き方 | 表示 |
| --- | --- |
| `\n\n`（空行） | 段落の区切り |
| `\n`（単独の改行） | 段落内の改行 |
| 行頭の `- ` | 箇条書き（連続する行がすべて `- ` で始まる場合） |
| `[表示文字列](https://…)` / 裸の `https://…` | リンク（別タブで開く） |

例:

```ini
REVIEW_DESCRIPTION=このシステムは Codex による PR レビューを実行します。\n\n- 使い方: [説明ページ](https://example.com/codex-review)\n- 不具合の報告: [#codex-review](https://example.com/chat)
```

見出し・強調・画像・HTML はサポートしない。設定値は DOM のテキストノードとして描画されるため、HTML タグを書いてもマークアップとしては解釈されずそのまま表示される（説明文経由でスクリプトが混入することはない）。

## 構成

```
webreview/
  config.py      設定（環境変数で上書き可能）
  errors.py      画面に出す想定の共通例外
  db.py          SQLite の接続管理とスキーマ
  github_auth.py GitHub のトークン供給（PAT / GitHub App）
  github_api.py  GitHub REST API の薄いラッパ
  findings.py    レビュー本文から行コメントにできる指摘を取り出す
  review.py      レビューの実行と投稿（codex / git の駆動）
  store.py       ジョブ状態の管理
  reviews.py     レビュー結果の永続化・集計・PR タイトルキャッシュ
  worker.py      単一ワーカーによる直列実行キュー
  export.py      レビューと指摘の CSV 書き出し
  server.py      Flask（REST API + 静的配信）
  static/        フロントエンド（HTML / CSS / JS）
    types.d.ts   API レスポンスの型（型検査専用。配信はしない）
tests/                pytest によるテスト
run.sh                起動スクリプト（ホスト直接起動用）
Dockerfile            codex / アプリを含むイメージ定義
docker-compose.yml    起動定義（volume・環境変数）
secrets/              GitHub App の秘密鍵置き場（.gitignore 済み）
.env.example          環境変数のテンプレート（docker compose / run.sh 共通）
requirements-dev.txt  テスト用の追加依存
tsconfig.json         フロントエンドの型検査設定（ビルドはしない）
```

## テスト

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

GitHub API・git・codex はいずれも差し替えるので、ネットワークにも本物の `codex` にも触らない（`codex` は代役のスクリプトを起動して確かめる）。`git` が入っていれば remote の検証も実行され、無ければその 2 件だけ skip する。

設定 (`config`) は import 時に環境変数から読む定数なので、テストでは `monkeypatch.setattr(config, ...)` で差し替える。`data/` は `tests/conftest.py` の `isolate_data_paths` が一時ディレクトリへ逃がすため、テストが実データを触ることはない。

### フロントエンドの型検査

`static/app.js` は素の JavaScript のまま配信する（ビルドは無い）。型は JSDoc のコメントで書き、先頭の `// @ts-check` と `tsconfig.json` で TypeScript に検査だけさせる。実行に Node は要らず、検査したいときだけ使う。

```bash
npx -p typescript@5 tsc --noEmit
```

`-p typescript@5` は省略しないこと。npm には TypeScript と無関係の `tsc` というパッケージがあり、TypeScript が入っていない環境で `npx tsc` とだけ打つとそちらが実行されて失敗する。

VS Code はこの設定を自動で読むので、エディタ上では何も入れなくても検査が効く。

API レスポンスの型は `static/types.d.ts` にまとめてある。これはサーバ側（`server.py` / `store.py` / `reviews.py` が返す辞書）を手で写したもので自動では同期しない。**API のフィールドを変えたら types.d.ts も直すこと。**

## API

| メソッド / パス | 説明 |
| --- | --- |
| `POST /api/reviews` | `{"url": "...", "auto_post": true, "include_quality": true, "skip_if_reviewed": false}` でレビューを投入（下記「CI からの投入」） |
| `GET /api/reviews` | ジョブ一覧（新しい順に `limit` 件。既定 20、上限 500）と総数 `total`・`busy` 状態 |
| `GET /api/reviews/<id>` | ジョブ詳細（レビュー本文を含む） |
| `POST /api/reviews/<id>/post` | 完了したレビューを PR に投稿 |
| `DELETE /api/reviews/<id>` | 終了したジョブを履歴から削除 |
| `GET /api/summary` | レビューの集計一覧（`limit` / `offset` / `repo` / `q`）。本文は含まない |
| `GET /api/summary/<id>` | レビュー本文と詳細情報 |
| `GET /api/export/reviews.csv` | レビュー単位の CSV（`repo` / `q` で絞り込み。下記「CSV エクスポート」） |
| `GET /api/export/findings.csv` | 指摘単位の CSV（同上） |
| `GET /api/config` | オーナー / モデル名 / reasoning effort / 説明文 |

### CI からの投入

`POST /api/reviews` は画面と同じ API なので、GitHub Actions の self-hosted runner などからも投入できる。`auto_post` と `include_quality` は既定で `true`。

`"skip_if_reviewed": true` を付けると、同じ PR を **一度でもレビューしていれば投入しない**。PR の更新 (`synchronize`) のたびに呼んでも、レビューは最初の 1 回だけになる。

- 完了したレビューがあるか (`reviews` を見るので、ジョブ履歴を消しても残る)、同じ PR のジョブが待機中・実行中なら見送る。失敗したジョブは数えないので、次の投入でやり直せる。
- 見送った場合は `200` で `{"skipped": true, "job": <待機中・実行中のジョブ or null>, "review_id": <最新のレビュー or null>}` を返す。投入した場合は `201` で `"skipped": false` とジョブを返す。
- 判定と投入は同じトランザクションで行うので、同じ PR に対して同時に投入されても二重には積まれない。
- 指定しない場合 (画面からの投入) は従来どおり毎回レビューする。

```bash
curl -fsS -X POST "http://<実行PCのIP>:8080/api/reviews" \
  -H 'Content-Type: application/json' \
  -d "{\"url\": \"${PR_URL}\", \"skip_if_reviewed\": true}"
```

## データの持ち方

状態はすべて SQLite（`REVIEW_DB_PATH`）に入る。ディスク上に残るファイルは git の作業ディレクトリと codex の生ログ（err）だけ。

codex の 2 つの出力は性質が違うため扱いを分けている。

- **stdout（レビュー本文）** は散文なので分量が頭打ちになる。DB に保存し、ファイルには残さない。
- **stderr（全トランスクリプト）** は実行した各コマンドとその出力を含み、PR の差分規模に比例して伸びる。ファイルに流したまま gzip し、`REVIEW_STDERR_RETENTION_DAYS` で期限削除する。

生ログはアプリからは読み返さない。集計に必要な先頭の実行条件ブロックだけを実行直後に取り出して DB に保存するため、期限削除しても画面表示は変わらない。

レビュー本文の先頭に出る `codex review (モデル, reasoning=..., rev-N):` は、保存された本文には含まれない。モデル名などはカラムとして持ち、表示・投稿のたびに組み立てる。品質改善を含めて実行したレビューには `quality` が入る。

集計用に、PR の作成者と変更の規模（追加行数・削除行数・変更ファイル数）もレビューごとに記録する。どちらもレビューの開始時に取得する PR の情報に含まれているので、API の呼び出しは増えない。規模はレビューした head コミット時点のもの。

## CSV エクスポート

集計そのものはアプリでは行わず、CSV をダウンロードして別のスクリプトや表計算で集計する。集計タブの「CSV ダウンロード」から、そのときの絞り込み条件（リポジトリ・本文検索）で書き出せる。件数の上限は無く、古い順に並ぶ。

- 文字コードは BOM 付きの UTF-8 で、Excel でそのまま開ける。pandas なら `pd.read_csv(path, encoding="utf-8-sig")` で読める。
- 日時（`created_at`）は UTC の ISO 8601（`2026-10-12T03:04:05Z`）。
- ファイル名には書き出した日時が UTC で付く（`reviews-20261012-030405Z.csv`）。
- 真偽値（`is_latest_rev` / `include_quality`）は `1` / `0`。値が無いところは空欄。

### `reviews.csv`（レビュー 1 件 = 1 行）

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

### `findings.csv`（指摘 1 件 = 1 行）

`review_id` / `repo` / `pr_number` / `pr_author` / `rev` / `is_latest_rev` / `created_at` / `include_quality` / `priority` / `title` / `path` / `start_line` / `end_line` / `description`。

`path` はリポジトリのルートからの相対パス。`description` は複数行になることがある（CSV の規則どおりに引用符で囲む）。指摘の無いレビューは行を出さないので、0 件のレビューも数えたいときは `reviews.csv` を使う。

### 指摘の数え方

PR への投稿と同じく、`- [P1] タイトル — パス:行` の形の箇条書きを 1 件の指摘として数える（「PR への投稿の形」）。投稿では位置を解決できない指摘を本文に残すが、集計ではそれも指摘として数える。

書式を読み取れなかった箇条書きは `unparsed_items` に出す。要約の中の箇条書きなど指摘でないものも含むが、多いレビューは codex の出力がぶれて指摘を取りこぼしている可能性がある。

### 集計するときの注意

- **同じ PR に複数のレビューがある。** やり直したレビューも 1 行ずつ出すので、PR 単位で数えるなら `is_latest_rev == 1` で絞るなど、どれを使うかを決める。
- **実行条件で指摘の数の基準が変わる。** 品質改善を含めた実行（`include_quality == 1`）は指摘が大きく増える。モデルや reasoning effort も同様。人や期間で比べるときはこれらで分けて集計する。
- **指摘の数は変更の規模にも比例する。** 必要なら `pr_additions + pr_deletions` などで割った値も見る。
- **作成者や規模が空欄のレビューがある。** 記録を始める前のレビューのうち、head コミットを記録していない古いものや、変更ファイルが多すぎて規模を求められなかったものは空欄のまま。規模で割るときは空欄を除く。
- **PR のタイトルや指摘の文面は信頼できない入力として扱う。** fork からの PR のタイトルや codex の出力がそのまま入る。`=` などで始まる値は Excel が数式として解釈しうるので、見慣れない値のセルは不用意に編集・実行しない。

## 品質改善の指摘

codex の既定のレビュー指針は、指摘を実際のバグに絞り、一般的なコード品質の話や非ブロッキングな指摘（体裁・タイポ・ドキュメント）を明示的に除外している。

フォームの「バグだけでなく、書き方・品質の改善も指摘する」を ON にすると、その除外だけを打ち消す指示をプロンプトに足す（`review._QUALITY_PROMPT`）。読みやすさ、不要な複雑さ、命名、責務の分離、重複、API 設計、テストの不足などを、動作として正しくても指摘の対象にする。

打ち消すのは除外の一点だけで、他の条件（この変更で入ったものに限る・既存コードの厳密さに合わせる・ファイルと行範囲を必ず付ける）は残す。品質の指摘は既定で `[P3]` とし、それだけを理由に「変更が正しくない」と判定させない。

この設定はジョブとレビューの両方に記録するので、ジョブ履歴を消したあとでも、集計タブのレビューがどちらの条件で実行されたかは分かる。

## レビュー対象のコミット

base と head のコミットは、どちらも PR の API（`GET /repos/{owner}/{repo}/pulls/{n}`）が返す SHA をそのまま使い、ブランチ名から git で引き直さない。ブランチから取ると、PR がマージされたあとに base ブランチの先端が PR 自身を含んでしまい、`git diff <base>...HEAD` の merge-base が head 側に一致して差分が空になる。head も API の SHA に固定することで、PR の情報を取ってから fetch するまでの間に push があってもレビュー対象が一意に決まる。SHA が取れない応答は、取り違えたコミットをレビューするより止める方を選んでエラーにする。

GitHub は ref から到達できる SHA なら fetch させるので、fork からの PR でも、ブランチが消えたあとでも同じ手順で取得できる。これは head だけの話ではない — feature ブランチから feature ブランチへ出した PR は、どちらもマージされて削除されると base のブランチも残らないが、コミットはマージ先から到達できるので SHA で要求すれば取れる。SHA 指定の fetch は既定では ref を作らず、オブジェクトが GC の対象になりうるので、refspec で `refs/review/<PR 番号>/{base,head}` に直接受ける。

必要なコミットは実行時に fetch するため、作業ディレクトリは clone せず `git init` + `git remote add` で空から作る。初回に全ブランチ・全履歴を落とさずに済み、この段階では GitHub のトークンも要らない。途中で失敗したディレクトリは消す（残すと次回は「既にある」と見なされ、origin の無いまま使い続けてしまう）。submodule は checkout のあとの `submodule update --init --recursive` で揃える。

## PR への投稿の形

codex は指摘を「タイトル — パス:行範囲」の箇条書きで出す。投稿時にこれを読み、位置の分かった指摘はソース行へのコメントに、残り（全体の要約や書式を読み取れなかったぶん）はレビュー本体の本文にする。パスは codex が動いた作業ツリーの絶対パスなので、`REVIEW_WORK_PATH/<リポジトリ名>` からの相対パスに直す。

行コメントの位置を GitHub が解決できないと、レビューごと 422（`Path could not be resolved` など）で弾かれる。そのときは指摘を含む本文をまとめて 1 つのコメントとして投稿し直すので、投稿自体が失敗することはない。

レビューは実行時の PR の head コミット（`head_commit`）に紐付けて投稿する。

## 確認を挟まない実行

ブラウザからの操作を待たせないため、レビューの実行中は確認を挟まず最後まで進める。

- ログ世代（rev）が既に存在する場合は、新しい rev を自動採番する。
- レビューはジョブ投入をもって実行する。
- PR への投稿は既定では完了後に自動で行う。OFF の場合は「PR に投稿」ボタンで実行する。
- 作業ツリーが dirty でも、毎回 PR のコミットへ `checkout --force` + `clean` して復帰する。

## ライセンス

[MIT License](LICENSE)
