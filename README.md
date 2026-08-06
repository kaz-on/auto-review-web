# auto_review Web システム

既存の `auto_review.sh`（レビュー実行・投稿）と `list_review.sh`（集計）の運用を、
LAN 内の PC からブラウザで使える軽量な Web アプリに置き換えたもの。

## できること

- ブラウザから PR の URL を入力してレビューを実行できる。
- レビューの **待機中 / 実行中 / 完了 / 失敗** の状態と進捗ログをリアルタイムに確認できる。
- レビュー完了後に **自動的に PR へ投稿** できる（既定でON。フォームのチェックで切替可）。
- 自動投稿をOFFにした場合や自動投稿に失敗した場合は、ボタンから手動で投稿できる。
- 終了したレビューは履歴から削除して一覧を整理できる（ログファイルは残る）。
- 過去のレビューログの集計をブラウザで閲覧できる。
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
# .env に GitHub 認証（GH_TOKEN もしくは GITHUB_APP_*）と CODEX_API_KEY を設定する
docker compose up -d --build
```

起動後、LAN 内の任意の PC のブラウザから `http://<実行PCのIP>:8080/` でアクセスする。

- 認証: 上記「GitHub 認証」の設定と `CODEX_API_KEY`（Codex CLI）。
  いずれも `.env` から環境変数として渡す。
- 永続化: git リポジトリ（`review_repos`）とログ（`review_logs`）は named volume。
  ジョブ履歴・PR タイトルキャッシュもログ volume 内に保存される。
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

認証は Docker の場合と同じ環境変数で渡す（`GITHUB_AUTH_MODE` ほか）。

```bash
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

元のレビュースクリプトは同時実行に対応していないため、本システムは
**単一ワーカーのキュー方式** を採る。

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
| `REVIEW_ORG` | `your-org` | 許可する GitHub Organization |
| `REVIEW_MODEL` | `gpt-5.6-terra` | codex のレビューモデル |
| `REVIEW_REASONING_EFFORT` | `medium` | 推論の深さ（`minimal` / `low` / `medium` / `high`）。空でモデル既定 |
| `REVIEW_WORK_PATH` | `data/work` | リポジトリのクローン先 |
| `REVIEW_LOGS_PATH` | `data/review_logs` | レビューログのルート |
| `REVIEW_JOBS_FILE` | `data/jobs.json` | ジョブ履歴の保存先 |
| `REVIEW_HOST` | `0.0.0.0` | 待ち受けアドレス |
| `REVIEW_PORT` | `8080` | 待ち受けポート |
| `REVIEW_GIT_BIN` / `REVIEW_CODEX_BIN` | `git` / `codex` | 各コマンドのパス |

`codex` には上記に加えて
`--sandbox read-only --ask-for-approval never --config model_reasoning_summary=none`
を固定で渡す。非対話実行で承認待ちに固まらず、作業ツリーを書き換えず、推論サマリを
レビュー本文に混ぜないため。

## 構成

```
webreview/
  config.py      設定（環境変数で上書き可能）
  errors.py      画面に出す想定の共通例外
  github_auth.py GitHub のトークン供給（PAT / GitHub App）
  github_api.py  GitHub REST API の薄いラッパ
  review.py      auto_review.sh の非対話ポート（レビュー実行・投稿）
  store.py       ジョブ状態の管理と JSON 永続化（スレッドセーフ）
  worker.py      単一ワーカーによる直列実行キュー
  aggregate.py   list_review.sh のポート（レビューログ集計）
  server.py      Flask（REST API + 静的配信）
  static/        フロントエンド（HTML / CSS / JS）
run.sh                起動スクリプト（ホスト直接起動用）
Dockerfile            codex / アプリを含むイメージ定義
docker-compose.yml    起動定義（volume・環境変数）
secrets/              GitHub App の秘密鍵置き場（.gitignore 済み）
.env.example          docker compose 用の環境変数テンプレート
```

## API

| メソッド / パス | 説明 |
| --- | --- |
| `POST /api/reviews` | `{"url": "...", "auto_post": true}` でレビューを投入（`auto_post` 既定 `true`） |
| `GET /api/reviews` | ジョブ一覧と `busy` 状態 |
| `GET /api/reviews/<id>` | ジョブ詳細（レビュー本文を含む） |
| `POST /api/reviews/<id>/post` | 完了したレビューを PR に投稿 |
| `DELETE /api/reviews/<id>` | 終了したジョブを履歴から削除 |
| `GET /api/summary` | レビューログの集計 |
| `GET /api/config` | Organization / モデル名 |

## 元スクリプトとの差分（対話部分の置き換え）

Web からは人手の確認（y/N）を挟めないため、次のように自動化した。

- ログ世代（rev）が既に存在する場合 → 確認せず新しい rev を自動採番。
- codex レビューの実行確認 → ジョブ投入をもって実行。
- PR への投稿確認 → 既定では完了後に自動投稿。OFF の場合は「PR に投稿」ボタンで実行。
- 作業ツリーが dirty でも、毎回 PR のコミットへ `checkout --force` + `clean` して復帰。

なお `auto_review.sh` / `list_review.sh` は従来どおりリポジトリにコミットせず
配置しておく運用（`.gitignore` 済み）。
