# 開発

使い方は [README](../README.md)、内部の仕様と設計判断は [design.md](design.md) を参照。

## 構成

```
webreview/
  config.py      設定（環境変数で上書き可能）
  errors.py      画面に出す想定の共通例外
  db.py          SQLite の接続管理とスキーマ
  forge.py       PR をホストするサービス（forge）ごとの違いを受け持つ層の定義
  github_forge.py GitHub（互換のサービスを含む）の forge 実装（URL の形・git の認証・投稿の手順）
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

## フロントエンドの型検査

`static/app.js` は素の JavaScript のまま配信する（ビルドは無い）。型は JSDoc のコメントで書き、先頭の `// @ts-check` と `tsconfig.json` で TypeScript に検査だけさせる。実行に Node は要らず、検査したいときだけ使う。

```bash
npx -p typescript@5 tsc --noEmit
```

`-p typescript@5` は省略しないこと。npm には TypeScript と無関係の `tsc` というパッケージがあり、TypeScript が入っていない環境で `npx tsc` とだけ打つとそちらが実行されて失敗する。

VS Code はこの設定を自動で読むので、エディタ上では何も入れなくても検査が効く。

API レスポンスの型は `static/types.d.ts` にまとめてある。これはサーバ側（`server.py` / `store.py` / `reviews.py` が返す辞書）を手で写したもので自動では同期しない。**API のフィールドを変えたら types.d.ts も直すこと。**
