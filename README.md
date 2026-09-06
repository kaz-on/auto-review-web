# auto-review-web

LAN 内の PC からブラウザで使える、Codex による PR レビューの実行・投稿・集計ツール。

## できること

- ブラウザから PR の URL を入力してレビューを実行できる。
- レビューの **待機中 / 実行中 / 完了 / 失敗** の状態と進捗ログをリアルタイムに確認できる。
- レビュー完了後に **自動的に PR へ投稿** できる（既定でON。フォームのチェックで切替可）。
- バグだけでなく **書き方・品質の改善** も指摘対象に含められる（既定でON。フォームの
  チェックで切替可。下記「品質改善の指摘」）。
- 指摘は **ソース行へのコメント** として投稿する（位置を読み取れなかったぶんは本文にまとめる）。
- 自動投稿をOFFにした場合や自動投稿に失敗した場合は、ボタンから手動で投稿できる。
- 終了したレビューは履歴から削除して一覧を整理できる（ログファイルは残る）。
- 過去のレビューを一覧・検索できる（リポジトリでの絞り込みと本文検索に対応）。
- エラーが発生した場合は画面にエラーメッセージを表示する。

## 動作要件

- **Docker で起動する場合（推奨）**: Docker / Docker Compose のみ。`git` / `codex`
  はイメージに含まれる。
- **ホストで直接起動する場合**: Python 3.10 以上、および `git` / `codex` が
  導入済みであること。

GitHub へのアクセスは REST API と `git` で行うため、GitHub CLI (`gh`) は不要。

## GitHub 認証

`GITHUB_AUTH_MODE` で 2 方式を選べる。

### `pat`（既定）

`GH_TOKEN` に Personal Access Token を設定する。repo の読み取りと PR コメントの
権限が必要。

### `app`

GitHub App の秘密鍵から installation access token を都度発行する。App には
**Contents = Read / Pull requests = Read and write / Metadata = Read** の権限を与え、
`REVIEW_ORG` の Organization にインストールしておく。

```ini
GITHUB_AUTH_MODE=app
GITHUB_APP_CLIENT_ID=Iv23li...              # App の Client ID（App ID でも可）
GITHUB_APP_PRIVATE_KEY_PATH=/run/secrets/github-app.pem
GITHUB_APP_INSTALLATION_ID=                 # 省略時は REVIEW_ORG から自動解決
```

秘密鍵（`.pem`）はリポジトリ直下の `secrets/` に置く。docker compose がこれを
`/run/secrets` へ read-only でマウントする（`secrets/` は `.gitignore` 済み）。
ファイルではなく PEM 文字列を直接渡したい場合は `GITHUB_APP_PRIVATE_KEY` を使う
（改行は `\n` で書ける）。

installation access token は 1 時間で失効するため、**GitHub にアクセスする直前に
取得し、残り有効期間が 5 分を切ったら再発行する**（`GITHUB_TOKEN_REFRESH_MARGIN`
で調整可）。起動時やジョブ開始時にまとめて取得する方式は、常駐サーバや「完了から
時間が経ってからの手動投稿」で失効するため採っていない。

トークンは実行のたびに subprocess の環境変数として渡され、git には環境変数を読む
credential helper 経由で渡る。ホストやイメージに認証設定は残らず、コマンドライン
や進捗ログにトークンが現れることもない。

## 起動方法A: Docker Compose（推奨）

ホストに `git` / `codex` を入れなくても、Docker だけで起動できる。
Codex CLI はイメージに含まれ、認証は環境変数で渡す
（ホストの認証ディレクトリは共有しない）。

```bash
cp .env.example .env
# .env に GitHub 認証（GH_TOKEN もしくは GITHUB_APP_*）、CODEX_API_KEY、REVIEW_ORG を設定する
docker compose up -d --build
```

起動後、LAN 内の任意の PC のブラウザから `http://<実行PCのIP>:8080/` でアクセスする。

- 認証: 上記「GitHub 認証」の設定と `CODEX_API_KEY`（Codex CLI）。
  いずれも `.env` から環境変数として渡す。
- 永続化: git リポジトリ（`review_repos`）と、DB・codex の生ログ（`review_logs`）は
  named volume。ジョブ履歴・レビュー本文・PR タイトルはすべて DB に入る。
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

認証は Docker の場合と同じ環境変数で渡す（`GITHUB_AUTH_MODE` ほか）。ただし
`run.sh` は `.env` を読まないため、**必須の環境変数（GitHub 認証・`CODEX_API_KEY`・
`REVIEW_ORG`）は自分で export しておく**こと。`REVIEW_ORG` が未設定のまま起動すると、
サーバーは立ち上がるがレビューはすべて拒否される（起動ログに `[warn]` が出る）。

```bash
export REVIEW_ORG=<your-org>
./run.sh
```

初回は自動で仮想環境を作成し、依存（Flask）をインストールする。起動後、
LAN 内の任意の PC のブラウザから `http://<実行PCのIP>:8080/` でアクセスする。

手動で起動する場合:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m webreview
```

## 同時実行の扱い

レビューは共有の作業ディレクトリで git を操作するため、**単一ワーカーの
キュー方式** を採る。

- レビューは投入順に **1 件ずつ直列** に実行される（計算リソースを使いすぎない）。
- 実行中に別の URL を投入するとキューに追加され、「実行中」バナーと各ジョブの
  状態表示でユーザーが状況を認識できる。
- 作業ディレクトリの git 操作も直列化されるため競合しない。

## 設定（環境変数で上書き可能）

| 環境変数 | 既定値 | 説明 |
| --- | --- | --- |
| `GITHUB_AUTH_MODE` | `pat` | GitHub 認証方式（`pat` / `app`） |
| `GH_TOKEN` | （空） | `pat` モードの Personal Access Token |
| `GITHUB_APP_CLIENT_ID` | （空） | `app` モードの Client ID（App ID も可） |
| `GITHUB_APP_PRIVATE_KEY_PATH` | （空） | `app` モードの秘密鍵ファイル |
| `GITHUB_APP_PRIVATE_KEY` | （空） | 秘密鍵を PEM 文字列で渡す場合 |
| `GITHUB_APP_INSTALLATION_ID` | （空） | 省略時は `REVIEW_ORG` から自動解決 |
| `GITHUB_TOKEN_REFRESH_MARGIN` | `300` | トークン再発行の閾値（秒） |
| `GITHUB_API_URL` | `https://api.github.com` | REST API のベース URL |
| `REVIEW_ORG` | （空） | **必須。** 許可する GitHub Organization |
| `REVIEW_MODEL` | `gpt-5.6-terra` | codex のレビューモデル |
| `REVIEW_REASONING_EFFORT` | `medium` | 推論の深さ（`minimal` / `low` / `medium` / `high`）。空でモデル既定 |
| `REVIEW_DESCRIPTION` | （空） | 画面上部に表示する説明文（下記「説明文の書式」） |
| `REVIEW_TIMEOUT` | `3600` | codex 1 回あたりの制限時間（秒）。`0` で無制限 |
| `REVIEW_STDERR_RETENTION_DAYS` | `30` | codex 生ログの保持日数。`0` で削除しない |
| `REVIEW_WORK_PATH` | `data/work` | リポジトリのクローン先 |
| `REVIEW_LOGS_PATH` | `data/review_logs` | codex の生ログのルート |
| `REVIEW_DB_PATH` | `data/review.db` | SQLite データベースの位置 |
| `REVIEW_HOST` | `0.0.0.0` | 待ち受けアドレス |
| `REVIEW_PORT` | `8080` | 待ち受けポート |
| `REVIEW_GIT_BIN` / `REVIEW_CODEX_BIN` | `git` / `codex` | 各コマンドのパス |

`codex` には上記に加えて
`--sandbox read-only --ask-for-approval never --config model_reasoning_summary=none`
を固定で渡す。非対話実行で承認待ちに固まらず、作業ツリーを書き換えず、推論サマリを
レビュー本文に混ぜないため。

## 説明文の書式（`REVIEW_DESCRIPTION`）

`REVIEW_DESCRIPTION` を設定すると、画面上部（Organization / モデル表示の下）に
説明欄が出る。レビューの概要や説明ページへのリンクを載せる用途を想定している。
未設定なら欄そのものが表示されない。

`.env` の値は 1 行でしか書けないため、**リテラルの `\n` を改行として解釈する**。
書式は最小限の Markdown 風で、次の 4 つだけをサポートする。

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

見出し・強調・画像・HTML はサポートしない。設定値は DOM のテキストノードとして
描画されるため、HTML タグを書いてもマークアップとしては解釈されずそのまま表示される
（説明文経由でスクリプトが混入することはない）。

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
  server.py      Flask（REST API + 静的配信）
  static/        フロントエンド（HTML / CSS / JS）
tests/                pytest によるテスト
run.sh                起動スクリプト（ホスト直接起動用）
Dockerfile            codex / アプリを含むイメージ定義
docker-compose.yml    起動定義（volume・環境変数）
secrets/              GitHub App の秘密鍵置き場（.gitignore 済み）
.env.example          docker compose 用の環境変数テンプレート
requirements-dev.txt  テスト用の追加依存
```

## テスト

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

GitHub API・git・codex はいずれも差し替えるので、ネットワークにも本物の `codex`
にも触らない（`codex` は代役のスクリプトを起動して確かめる）。`git` が入っていれば
remote の検証も実行され、無ければその 2 件だけ skip する。

設定 (`config`) は import 時に環境変数から読む定数なので、テストでは
`monkeypatch.setattr(config, ...)` で差し替える。`data/` は `tests/conftest.py` の
`isolate_data_paths` が一時ディレクトリへ逃がすため、テストが実データを触ることはない。

## API

| メソッド / パス | 説明 |
| --- | --- |
| `POST /api/reviews` | `{"url": "...", "auto_post": true, "include_quality": true}` でレビューを投入（どちらも既定 `true`） |
| `GET /api/reviews` | ジョブ一覧と `busy` 状態 |
| `GET /api/reviews/<id>` | ジョブ詳細（レビュー本文を含む） |
| `POST /api/reviews/<id>/post` | 完了したレビューを PR に投稿 |
| `DELETE /api/reviews/<id>` | 終了したジョブを履歴から削除 |
| `GET /api/summary` | レビューの集計一覧（`limit` / `offset` / `repo` / `q`）。本文は含まない |
| `GET /api/summary/<id>` | レビュー本文と詳細情報 |
| `GET /api/config` | Organization / モデル名 / reasoning effort / 説明文 |

## データの持ち方

状態はすべて SQLite（`REVIEW_DB_PATH`）に入る。ディスク上に残るファイルは
git のクローンと codex の生ログ（err）だけ。

codex の 2 つの出力は性質が違うため扱いを分けている。

- **stdout（レビュー本文）** は散文なので分量が頭打ちになる。DB に保存し、
  ファイルには残さない。
- **stderr（全トランスクリプト）** は実行した各コマンドとその出力を含み、PR の
  差分規模に比例して伸びる。ファイルに流したまま gzip し、
  `REVIEW_STDERR_RETENTION_DAYS` で期限削除する。

生ログはアプリからは読み返さない。集計に必要な先頭の実行条件ブロックだけを
実行直後に取り出して DB に保存するため、期限削除しても画面表示は変わらない。

レビュー本文の先頭に出る `codex review (モデル, reasoning=..., rev-N):` は、
保存された本文には含まれない。モデル名などはカラムとして持ち、表示・投稿の
たびに組み立てる。品質改善を含めて実行したレビューには `quality` が入る。

## 品質改善の指摘

codex の既定のレビュー指針は、指摘を実際のバグに絞り、一般的なコード品質の話や
非ブロッキングな指摘（体裁・タイポ・ドキュメント）を明示的に除外している。

フォームの「バグだけでなく、書き方・品質の改善も指摘する」を ON にすると、その
除外だけを打ち消す指示をプロンプトに足す（`review._QUALITY_PROMPT`）。読みやすさ、
不要な複雑さ、命名、責務の分離、重複、API 設計、テストの不足などを、動作として
正しくても指摘の対象にする。

打ち消すのは除外の一点だけで、他の条件（この変更で入ったものに限る・既存コードの
厳密さに合わせる・ファイルと行範囲を必ず付ける）は残す。品質の指摘は既定で `[P3]`
とし、それだけを理由に「変更が正しくない」と判定させない。

この設定はジョブとレビューの両方に記録するので、ジョブ履歴を消したあとでも、
集計タブのレビューがどちらの条件で実行されたかは分かる。

## PR への投稿の形

codex は指摘を「タイトル — パス:行範囲」の箇条書きで出す。投稿時にこれを読み、
位置の分かった指摘はソース行へのコメントに、残り（全体の要約や書式を読み取れな
かったぶん）はレビュー本体の本文にする。パスは codex が動いた作業ツリーの絶対
パスなので、`REVIEW_WORK_PATH/<リポジトリ名>` からの相対パスに直す。

行コメントの位置を GitHub が解決できないと、レビューごと 422（`Path could not be
resolved` など）で弾かれる。そのときは指摘を含む本文をまとめて 1 つのコメントとして
投稿し直すので、投稿自体が失敗することはない。

レビューは実行時の PR の head コミット（`head_commit`）に紐付けて投稿する。

## 確認を挟まない実行

ブラウザからの操作を待たせないため、レビューの実行中は確認を挟まず最後まで進める。

- ログ世代（rev）が既に存在する場合は、新しい rev を自動採番する。
- レビューはジョブ投入をもって実行する。
- PR への投稿は既定では完了後に自動で行う。OFF の場合は「PR に投稿」ボタンで実行する。
- 作業ツリーが dirty でも、毎回 PR のコミットへ `checkout --force` + `clean` して復帰する。
