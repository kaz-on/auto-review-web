# auto-review-web

LAN 内の PC からブラウザで使える、[Codex CLI](https://github.com/openai/codex)（OpenAI）による PR レビューの実行・投稿・集計ツール。

レビューの対象は、GitHub（github.com）のプルリクエスト、GitHub 互換の REST API を持つセルフホストのサービスのプルリクエスト（下記「GitHub 互換のサービス」）、GitLab のマージリクエスト（下記「GitLab」）。1 つのインスタンスで扱えるのはこのうち 1 つのサービスだけ。PR / MR になっていないブランチやコミットはレビューできない。GitHub Enterprise Server は互換のサービスと同じ設定で動く見込みだが、確かめていない。

このアプリケーションは Claude Code を利用して作成されている。

## できること

- ブラウザから PR の URL を入力してレビューを実行できる。
- レビューは投入順に 1 件ずつ実行される。実行中に投入したレビューは待機中としてキューに並ぶ。
- レビューの **待機中 / 実行中 / 完了 / 失敗** の状態と進捗ログをリアルタイムに確認できる。
- レビュー完了後に **自動的に PR へ投稿** できる（既定でON。フォームのチェックで切替可）。
- バグだけでなく **書き方・品質の改善** も指摘対象に含められる（既定でON。フォームのチェックで切替可。下記「品質改善の指摘」）。
- 指摘は **ソース行へのコメント** として投稿する（位置を読み取れなかったぶんは本文にまとめる）。
- 投稿する本文の中のファイルの参照（指摘のパスや `AGENTS.md` への相対リンク）は、レビューしたコミットのファイルへのリンクにする。
- 自動投稿をOFFにした場合や自動投稿に失敗した場合は、ボタンから手動で投稿できる。
- 終了したレビューは履歴から削除して一覧を整理できる（ログファイルは残る）。
- 一覧には直近 20 件を表示し、「さらに表示」で古いものを読み込める。終了から 30 日を過ぎたジョブは履歴から自動で削除する（`REVIEW_JOB_RETENTION_DAYS`）。失敗したジョブと未投稿のレビューは、同じ PR をやり直さない限り残す。どちらの削除でもレビュー本体は消えず、集計タブから見られる。
- 過去のレビューを一覧・検索できる（リポジトリでの絞り込みと本文検索に対応）。
- レビューと指摘の一覧を CSV でダウンロードし、誰の PR にどれだけ指摘があったかなどを別のスクリプトや表計算で集計できる（下記「CSV エクスポート」）。
- エラーが発生した場合は画面にエラーメッセージを表示する。

## セキュリティ上の注意

**このツールには認証の仕組みがない。** 画面と API に届く人は誰でも、レビューの投入・PR への投稿・履歴の削除ができる。PR への投稿は設定した GitHub の認証情報（PAT ならそのアカウント）の名義で行われ、Codex の利用料金も設定した認証情報（API キーまたは access token）に課金される。

- 信頼できる LAN 内だけで使い、インターネットには公開しない。外から使う場合は、認証付きのリバースプロキシや VPN の後ろに置く。
- 既定では実行している PC 自身（`127.0.0.1`）からしか接続できない。LAN 内のほかの PC から使う場合は `REVIEW_HOST=0.0.0.0` にする（docker compose でもホストで直接起動する場合でも同じ）。
- レビューできるのは `REVIEW_OWNER` に書いたオーナーのリポジトリへの PR に限られる（カンマ区切りで複数書ける。「すべて許可」の指定はない）。ただし codex は PR のコードや説明文を読むので、fork から出された PR のように信頼できない人が書いた内容は、レビュー結果に影響しうる（プロンプトインジェクション）。PR が `AGENTS.md` を書き換えた場合も同様（下記「プロジェクト固有のレビュー観点」）。codex は read-only のサンドボックスで動かしており、作業ツリーを書き換えることはない。

## 動作要件

- **Docker で起動する場合（推奨）**: Docker / Docker Compose のみ。`git` / `codex` はイメージに含まれる。
- **ホストで直接起動する場合**: Python 3.10 以上、および `git` / `codex` が導入済みであること。

## PR をホストするサービス

`REVIEW_FORGE` で、レビューする PR / MR のあるサービスを選ぶ。1 つのインスタンスで扱えるのはどちらか一方だけ。

| `REVIEW_FORGE` | 対象 | 設定する環境変数 |
| --- | --- | --- |
| `github`（既定） | github.com と、GitHub 互換の REST API を持つサービス | `GITHUB_*` / `GH_TOKEN`（下記「GitHub 認証」「GitHub 互換のサービス」） |
| `gitlab` | GitLab（CE を含むセルフホスト） | `GITLAB_*`（下記「GitLab」） |

選ばなかったほうの環境変数は使わないので、空のままでよい。

## GitHub 認証

`REVIEW_FORGE=github`（既定）のときに使う。`GITHUB_AUTH_MODE` で 2 方式を選べる。

### `pat`（既定）

`GH_TOKEN` に Personal Access Token を設定する。repo の読み取りと PR コメントの権限が必要。

### `app`

GitHub App の秘密鍵から installation access token を都度発行する。App には **Contents = Read / Pull requests = Read and write / Metadata = Read** の権限を与え、`REVIEW_OWNER` のアカウント（Organization または個人アカウント）にインストールしておく。`REVIEW_OWNER` に複数のオーナーを書いた場合は、そのすべてにインストールする。

```ini
GITHUB_AUTH_MODE=app
GITHUB_APP_CLIENT_ID=Iv23li...              # App の Client ID（App ID でも可）
GITHUB_APP_PRIVATE_KEY_PATH=/run/secrets/github-app.pem
GITHUB_APP_INSTALLATION_ID=                 # 省略時は REVIEW_OWNER から自動解決（オーナーが複数なら省略する）
```

秘密鍵（`.pem`）はリポジトリ直下の `secrets/` に置く。docker compose がこれを `/run/secrets` へ read-only でマウントする（`secrets/` は `.gitignore` 済み）。

installation はオーナーごとに分かれているので、トークンもオーナーごとに発行する。`GITHUB_APP_INSTALLATION_ID` を指定できるのはオーナーが 1 つのときだけで、複数のときは省略してオーナーごとに自動で解決させる。

installation access token は GitHub にアクセスする直前に取得し、失効が近ければ再発行するので、常駐させていても切れない。トークンの受け渡しの仕組みは [docs/design.md](docs/design.md#github-トークンの扱い) を参照。

## GitHub 互換のサービス

`REVIEW_FORGE=github`（既定）のまま、以下を設定する。

GitHub と同じ形の REST API を持つセルフホストのサービスも、設定を合わせればレビューできる。1 つのインスタンスで扱えるのは 1 つのサービスだけで、github.com と混ぜては使えない。

```ini
GITHUB_WEB_URL=http://git.example.local:8888        # PR の URL はこの下の /<owner>/<repo>/pull/<番号>
GITHUB_API_URL=http://git.example.local:8888/api/v3
GITHUB_CLONE_URL={web}/git/{owner}/{repo}.git        # サービスの clone URL の形
GITHUB_GIT_USERNAME=review-bot                       # トークンの持ち主のアカウント名
GITHUB_POST_MODE=issue_comment                       # レビュー API が無ければ issue_comment
GH_TOKEN=...                                         # 読み取りと PR へのコメントができるトークン
```

- **認証**: `pat` モード（`GH_TOKEN`）だけ使える。public のリポジトリでも、PR への投稿にはトークンが要る。
- **git の認証**: github.com はユーザー名が `x-access-token` 固定で通るが、トークンの持ち主のアカウント名でないと通らないサービスがある。そのときは `GITHUB_GIT_USERNAME` にその名前を書く。
- **投稿**: `pulls/{n}/reviews`（レビュー API）を持たないサービスでは `GITHUB_POST_MODE=issue_comment` にする。指摘をソース行へのコメントにはせず、本文全体を 1 件のコメント（`issues/{n}/comments`）として投稿する。
- **必要な API**: `GET /repos/{owner}/{repo}/pulls/{n}` が `base.sha` / `head.sha` / `base.ref` を返すこと。マージしたあとも `base.sha` が PR の元の base のまま変わらないこと（変わると差分が空になる。[docs/design.md](docs/design.md#レビュー対象のコミット)）。
- **git**: SHA を指定した fetch に応じること。プロトコル v2 でしか応じないサービスがあるので、アプリは fetch をプロトコル v2 に固定している。

## GitLab

GitLab（CE を含むセルフホストの GitLab）のマージリクエストをレビューするときは、`REVIEW_FORGE=gitlab` にする。GitHub の設定（`GITHUB_*` / `GH_TOKEN`）は使わない。

```ini
REVIEW_FORGE=gitlab
GITLAB_URL=https://gitlab.example.com     # MR の URL はこの下の /<group>/<project>/-/merge_requests/<番号>
GITLAB_TOKEN=glpat-...
REVIEW_OWNER=my-group                      # グループのパス。サブグループも含む
```

- **トークン**: レビュー対象がすべて 1 つのトップレベルのグループ（とそのサブグループ）にあるなら、そのグループの**グループアクセストークン**を使う。ロールは **Reporter**、スコープは **`api` と `read_repository`**。投稿はトークンのボットの名前で行われる。複数のトップレベルのグループを `REVIEW_OWNER` に並べる場合は、それらすべてに Reporter で参加させた専用ユーザーのパーソナルアクセストークンを使う（アプリが持てるトークンは 1 つだけのため）。
- **トークンの更新**: GitLab のトークンには有効期限がある。更新は「失効させて作り直す」ではなくローテーションで行う（グループアクセストークンを失効させるとボットのユーザーが消え、過去の投稿の名前が「Ghost User」になる）。トークンは起動時に読むので、更新したら再起動する。
- **オーナー**: `REVIEW_OWNER` にはグループのパスを書く。そのグループの下のサブグループも許可する（`my-group` を書けば `my-group/sub/project` も通る）。サブグループだけを許可するときは `my-group/sub` と書く。
- **投稿**: 指摘をソース行へのコメントにはせず、本文全体を MR へのコメント 1 件として投稿する。GitLab は単独の改行を改行として表示しないので、投稿するときだけ改行を明示する（DB の本文は変えない）。
- **MR を作った直後**: GitLab が MR の差分を計算し終えるまではレビュー対象のコミットが分からないので、数秒待って取り直す。それでも取れなければエラーになるので、少し待ってからやり直す。
- **変更の規模**: GitLab の API は追加・削除の行数を返さないので、集計の `pr_additions` / `pr_deletions` は空欄になる。`pr_changed_files` は入る（1000 件を超えると空欄）。

## Codex 認証

次のどちらか一方を設定する（両方は設定しない）。

- `CODEX_API_KEY`: OpenAI の API キー。
- `CODEX_ACCESS_TOKEN`: ChatGPT の Business / Enterprise ワークスペースの管理コンソールで発行する [access token](https://developers.openai.com/codex/enterprise/access-tokens)。

どちらも codex がそのまま読むので、アプリ側の設定はいらない。両方とも未設定だと起動ログに `[warn]` が出る（起動は続く）。

## 起動方法A: Docker Compose（推奨）

ホストに `git` / `codex` を入れなくても、Docker だけで起動できる。Codex CLI はイメージに含まれ、認証は環境変数で渡す（ホストの認証ディレクトリは共有しない）。

```bash
cp .env.example .env
# .env に GitHub 認証（GH_TOKEN もしくは GITHUB_APP_*。GitLab なら REVIEW_FORGE と GITLAB_*）、Codex 認証（CODEX_API_KEY もしくは CODEX_ACCESS_TOKEN）、REVIEW_OWNER を設定する
docker compose up -d --build
```

起動後、ブラウザから `http://localhost:8080/` でアクセスする。LAN 内のほかの PC から使う場合は、`.env` に `REVIEW_HOST=0.0.0.0` を設定して `http://<実行PCのIP>:8080/` でアクセスする（上記「セキュリティ上の注意」）。

- 認証: 上記「GitHub 認証」（GitLab なら「GitLab」）と「Codex 認証」の設定。いずれも `.env` から環境変数として渡す。
- 永続化: git リポジトリ（`review_repos`）と、DB・codex の生ログ（`review_logs`）は named volume。ジョブ履歴・レビュー本文・PR タイトルはすべて DB に入る。
- 停止 / ログ確認:

  ```bash
  docker compose down          # 停止（volume は保持）
  docker compose logs -f       # ログ確認
  docker compose down -v       # volume ごと削除（履歴・クローンも消える）
  ```

- アップデート: codex はビルド時に入れるため、`up --build` ではキャッシュが使われて更新されない。キャッシュを使わずにビルドし直し、コンテナを作り直す（`--pull` でベースイメージも更新する）。volume は保持される。

  ```bash
  docker compose build --pull --no-cache
  docker compose up -d
  ```

## 起動方法B: ホストで直接起動

レビューを実行する PC に以下が導入済みであること。

- Python 3.10 以上
- `git` / `codex`

認証は Docker の場合と同じ環境変数で渡す（`GITHUB_AUTH_MODE` ほか）。`run.sh` はリポジトリ直下に `.env` があれば読んで export するので、設定は docker compose と同じ 1 ファイルで済む。

Tips: ホストの `codex` で `codex login` 済みなら、`CODEX_API_KEY` / `CODEX_ACCESS_TOKEN` を空のままにしておくとその認証が使われる（起動ログの `[warn]` は無視してよい）。ChatGPT アカウントでログインした場合は、そのプランの利用上限がレビューにも適用される。

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

## バックアップ

残しておくべきデータは SQLite の DB（ジョブ履歴・レビュー本文・指摘・PR タイトル）。クローンしたリポジトリは次のレビューで取り直すので要らない。codex の生ログは調査用で、無くても画面と集計には影響しない。`.env` の設定値は控えておくとよいが、トークンや秘密鍵は再発行できるので、バックアップに含めるかは運用に合わせる。

DB は WAL モードで動いているので、ファイルをそのままコピーすると書き込み途中の内容を取りこぼすことがある。SQLite のバックアップ API を使えば、起動したままでも一貫した状態で書き出せる。

Docker Compose の場合（DB はコンテナ内の `/data/review_logs/review.db`）:

```bash
docker compose exec webreview python -c "import sqlite3; d = sqlite3.connect('/tmp/review.db'); sqlite3.connect('/data/review_logs/review.db').backup(d); d.close()"
docker compose cp webreview:/tmp/review.db ./review-backup.db
```

ホストで直接起動している場合（DB は `REVIEW_DB_PATH`、既定 `data/review.db`）:

```bash
python3 -c "import sqlite3; d = sqlite3.connect('review-backup.db'); sqlite3.connect('data/review.db').backup(d); d.close()"
```

戻すときは、アプリを止めてから DB を置き換え、同じ場所にある `review.db-wal` / `review.db-shm` があれば消す。Docker Compose の場合:

```bash
docker compose stop
docker compose run --rm --no-deps -v "$PWD/review-backup.db:/restore.db:ro" webreview \
  sh -c 'rm -f /data/review_logs/review.db-wal /data/review_logs/review.db-shm && cp /restore.db /data/review_logs/review.db'
docker compose start
```

## レビュー対象のオーナー（`REVIEW_OWNER`）

`REVIEW_OWNER` にはレビューを許可するオーナーを書く。GitHub では Organization 名または個人アカウント名、GitLab ではグループのパス。`REVIEW_OWNER=team-a,team-b` のようにカンマ区切りで複数書ける。オーナー名は完全一致で照合する（大文字小文字も区別する）。ただし GitLab では、書いたグループの下のサブグループも許可する（上記「GitLab」）。

認証の仕組みがないこのツールでは、オーナーの制約がレビュー・投稿できる範囲を絞る唯一の関門なので、「すべて許可」の指定は設けていない。空のままならすべて拒否する。

オーナーが違えば同じ名前のリポジトリも別のものとして扱う。画面と集計では `owner/repo` の形で表示する。

オーナーを 1 つしか書けなかった版から更新すると、初回の起動時に DB をオーナー付きの形へ移行する。既存のレビューのオーナーは、そのレビューを実行したジョブから埋める。ジョブが消えていれば `REVIEW_OWNER` の先頭のオーナーで埋めるので、**オーナーを増やす前に、いったん今の設定のまま更新して起動する**。

## 設定（環境変数で上書き可能）

Codex の認証（`CODEX_API_KEY` / `CODEX_ACCESS_TOKEN`）は上記「Codex 認証」を参照。

### サービスの選択

| 環境変数 | 既定値 | 説明 |
| --- | --- | --- |
| `REVIEW_FORGE` | `github` | PR をホストするサービス（`github` = github.com と GitHub 互換のサービス / `gitlab` = GitLab） |

### GitHub（`REVIEW_FORGE=github`）

| 環境変数 | 既定値 | 説明 |
| --- | --- | --- |
| `GITHUB_AUTH_MODE` | `pat` | GitHub 認証方式（`pat` / `app`） |
| `GH_TOKEN` | （空） | `pat` モードの Personal Access Token |
| `GITHUB_APP_CLIENT_ID` | （空） | `app` モードの Client ID（App ID も可） |
| `GITHUB_APP_PRIVATE_KEY_PATH` | （空） | `app` モードの秘密鍵ファイル |
| `GITHUB_APP_INSTALLATION_ID` | （空） | 省略時は `REVIEW_OWNER` から自動解決。オーナーが複数なら指定できない |
| `GITHUB_TOKEN_REFRESH_MARGIN` | `300` | トークン再発行の閾値（秒） |
| `GITHUB_WEB_URL` | `https://github.com` | PR をホストするサービスの URL（上記「GitHub 互換のサービス」）。github.com なら変更不要 |
| `GITHUB_API_URL` | `https://api.github.com` | REST API のベース URL。github.com なら変更不要 |
| `GITHUB_CLONE_URL` | `{web}/{owner}/{repo}.git` | clone URL の形。`{web}` は `GITHUB_WEB_URL` に置き換わる |
| `GITHUB_GIT_USERNAME` | `x-access-token` | git の認証に使うユーザー名 |
| `GITHUB_POST_MODE` | `review` | 投稿の方法（`review` = レビュー API で行コメント付き / `issue_comment` = 本文全体を 1 件のコメント） |

### GitLab（`REVIEW_FORGE=gitlab`）

| 環境変数 | 既定値 | 説明 |
| --- | --- | --- |
| `GITLAB_URL` | （空） | `gitlab` のときの GitLab の URL |
| `GITLAB_TOKEN` | （空） | `gitlab` のときのアクセストークン（上記「GitLab」） |

### レビュー

| 環境変数 | 既定値 | 説明 |
| --- | --- | --- |
| `REVIEW_OWNER` | （空） | **必須。** 許可するリポジトリのオーナー（GitHub は Organization 名または個人アカウント名、GitLab はグループのパス）。カンマ区切りで複数指定できる（上記「レビュー対象のオーナー」） |
| `REVIEW_MODEL` | `gpt-6.1-sol` | codex のレビューモデル |
| `REVIEW_REASONING_EFFORT` | `medium` | 推論の深さ（`minimal` / `low` / `medium` / `high`）。空でモデル既定 |
| `REVIEW_DESCRIPTION` | （空） | 画面上部に表示する説明文（下記「説明文の書式」） |
| `REVIEW_TIMEOUT` | `3600` | codex 1 回あたりの制限時間（秒）。`0` で無制限 |
| `REVIEW_STDERR_RETENTION_DAYS` | `30` | codex 生ログの保持日数。`0` で削除しない |
| `REVIEW_JOB_RETENTION_DAYS` | `30` | 終了したジョブを一覧から自動削除するまでの日数。失敗・未投稿のものは残す。`0` で削除しない |

### 保存先・サーバ・外部コマンド

| 環境変数 | 既定値 | 説明 |
| --- | --- | --- |
| `REVIEW_WORK_PATH` | `data/work` | リポジトリの作業ディレクトリ |
| `REVIEW_LOGS_PATH` | `data/review_logs` | codex の生ログのルート |
| `REVIEW_DB_PATH` | `data/review.db` | SQLite データベースの位置 |
| `REVIEW_HOST` | `127.0.0.1` | 待ち受けアドレス。LAN に公開するなら `0.0.0.0` |
| `REVIEW_PORT` | `8080` | 待ち受けポート |
| `REVIEW_GIT_BIN` / `REVIEW_CODEX_BIN` | `git` / `codex` | 各コマンドのパス |

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

## API

| メソッド / パス | 説明 |
| --- | --- |
| `POST /api/reviews` | `{"url": "...", "auto_post": true, "include_quality": true, "skip_if_reviewed": false}` でレビューを投入（下記「CI からの投入」） |
| `GET /api/reviews` | ジョブ一覧（新しい順に `limit` 件。既定 20、上限 500）と総数 `total`・`busy` 状態 |
| `GET /api/reviews/<id>` | ジョブ詳細（レビュー本文を含む） |
| `POST /api/reviews/<id>/post` | 完了したレビューを PR に投稿 |
| `DELETE /api/reviews/<id>` | 終了したジョブを履歴から削除 |
| `GET /api/summary` | レビューの集計一覧（`limit` / `offset` / `repo` / `q`）。本文は含まない。`repo` は `owner/repo` の形（リポジトリ名だけならオーナーを問わず絞り込む） |
| `GET /api/summary/<id>` | レビュー本文と詳細情報 |
| `GET /api/export/reviews.csv` | レビュー単位の CSV（`repo` / `q` で絞り込み。下記「CSV エクスポート」） |
| `GET /api/export/findings.csv` | 指摘単位の CSV（同上） |
| `GET /api/config` | オーナー / PR の URL の形 / モデル名 / reasoning effort / 説明文 |

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

## CSV エクスポート

集計そのものはアプリでは行わず、CSV をダウンロードして別のスクリプトや表計算で集計する。集計タブの「CSV ダウンロード」から、そのときの絞り込み条件（リポジトリ・本文検索）で、レビュー単位の `reviews.csv` と指摘単位の `findings.csv` を書き出せる。

列の定義や集計するときの注意は [docs/csv-export.md](docs/csv-export.md) を参照。

## 品質改善の指摘

codex の既定のレビュー指針は、指摘を実際のバグに絞り、一般的なコード品質の話や非ブロッキングな指摘（体裁・タイポ・ドキュメント）を除外している。

フォームの「バグだけでなく、書き方・品質の改善も指摘する」を ON にすると、読みやすさ、不要な複雑さ、命名、責務の分離、重複、API 設計、テストの不足なども、動作として正しくても指摘の対象になる。品質の指摘は既定で `[P3]` になる。

プロンプトに何を足しているかは [docs/design.md](docs/design.md#品質改善の指摘) を参照。

## プロジェクト固有のレビュー観点

レビュー対象のリポジトリに `AGENTS.md` を置くと、codex はその内容もレビューの指針として使う。アプリ側の設定はいらない。codex の既定の指針と `AGENTS.md` の扱いは [codex のレビュー用プロンプト](https://github.com/openai/codex/blob/main/codex-rs/prompts/templates/review/rubric.md) を参照。

- ルートの `AGENTS.md` のほか、変更されたファイルのあるディレクトリの `AGENTS.md` も読まれる。食い違う場合は、より近い階層のものが優先される。
- コーディング規約を書いておくと、「バグだけでなく、書き方・品質の改善も指摘する」が OFF でも、規約違反は指摘の対象になる。`AGENTS.md` のルールが根拠になった指摘には、本文にそのファイルと行への参照が付くよう指示されている。
- このアプリがプロンプトに足す指示（品質改善の指摘）は `AGENTS.md` より優先される。
- 読まれるのは PR の head のコミットにある `AGENTS.md` なので、PR の中で `AGENTS.md` を変更すると、その PR 自身のレビューにも反映される。
- `AGENTS.md` は codex がレビュー以外の作業でも読むので、レビューだけに関わる要件は見出しを分けて書くとよい。

### PR ごとの観点

PR のタイトルと説明文（GitLab では MR の説明）も codex に渡している。その PR だけに関わる指示は、PR の説明に書くとよい。たとえば変更の意図、重点的に見てほしい箇所、意図して変えた仕様（指摘しなくてよいこと）などを書いておくと、レビューに反映される。

説明文はレビューを始めるときに取得するので、書き直したらレビューを投入し直す。

## ドキュメント

- [docs/csv-export.md](docs/csv-export.md) — CSV の列の定義と集計するときの注意
- [docs/development.md](docs/development.md) — 構成・テスト・型検査
- [docs/design.md](docs/design.md) — 内部の仕様と設計判断

## ライセンス

[MIT License](LICENSE)
