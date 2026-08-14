"""ファイル保持だった頃のデータを SQLite に取り込む。

対象は 3 つ:

* ``review_logs/<repo>/codex_review_pr<num>_out.log-<rev>`` … レビュー本文
* ``jobs.json`` … ジョブ履歴
* ``pr_titles.json`` … PR タイトルのキャッシュ

本文の先頭にはレビュー実行時の設定を示すヘッダ行が入っていることがあるが、この
書式は運用中に改訂されている (``reasoning=`` は後から加わり、ごく初期のログには
行そのものが無い)。そのためヘッダは**取り込みの入力として当てにしない**:

* rev  … ファイル名から取る (常に存在する)
* model / reasoning effort … err ログのバナーから取る (codex が実際に使った値)
* ヘッダ行 … 見つかれば本文から取り除くだけ。取れる情報があれば補助的に使う

これは新規レビューが DB に入るときの経路 (review.py) と同じ優先順位で、
ヘッダ行はどちらの経路でも本文には残らない。

再実行しても同じ結果になる (UNIQUE 制約に当たったぶんは飛ばす)。
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import config, db, review, reviews

_OUT_RE = re.compile(r"^codex_review_pr(\d+)_out\.log-(\d+)$")

# 運用中に改訂されたヘッダ行。既知の形は以下の 2 つで、どちらも rev-<n> を含む。
#   codex review (gpt-5.6-terra, reasoning=medium, rev-0):
#   codex review (gpt-5.6-terra, rev-0):
# 想定外の形が来ても本文を壊さないよう、除去は「行全体が一致した場合」に限る。
_HEADER_RE = re.compile(r"^codex review \(([^)\n]*)\):[ \t]*\n+")
_REV_IN_HEADER_RE = re.compile(r"\brev-(\d+)\b")


@dataclass
class Report:
    """取り込み結果。件数の内訳を出して目視で確認できるようにする。"""
    reviews_imported: int = 0
    reviews_skipped: int = 0
    header_stripped: int = 0
    header_absent: int = 0
    header_without_reasoning: int = 0
    meta_from_err: int = 0
    meta_missing: int = 0
    jobs_imported: int = 0
    titles_imported: int = 0
    notes: list[str] = field(default_factory=list)

    def lines(self) -> list[str]:
        out = [
            f"レビュー本文: {self.reviews_imported} 件取り込み "
            f"({self.reviews_skipped} 件は取り込み済みのため省略)",
            f"  ヘッダ行: 除去 {self.header_stripped} / 元から無し {self.header_absent} "
            f"(うち reasoning 無しの旧書式 {self.header_without_reasoning})",
            f"  model・reasoning: err ログから {self.meta_from_err} 件 / "
            f"不明 {self.meta_missing} 件",
            f"ジョブ履歴: {self.jobs_imported} 件",
            f"PR タイトル: {self.titles_imported} 件",
        ]
        return out + [f"  ! {n}" for n in self.notes]


def strip_header(text: str) -> tuple[str, str | None]:
    """本文先頭のヘッダ行を取り除く。(本文, ヘッダ中身) を返す。

    ヘッダが無ければ (元の本文, None)。
    """
    m = _HEADER_RE.match(text)
    if not m:
        return text, None
    return text[m.end():], m.group(1)


def _meta_from_header(inner: str) -> tuple[str | None, str | None]:
    """ヘッダ行の括弧内から model と reasoning effort を推定する。

    書式が改訂されているため、確実に言えるのは「先頭が model」ということだけ。
    reasoning は ``reasoning=<値>`` があれば拾い、無ければ旧書式とみなす。
    """
    parts = [p.strip() for p in inner.split(",") if p.strip()]
    model = parts[0] if parts else None
    if model and _REV_IN_HEADER_RE.fullmatch(model):
        # 括弧内が rev だけ、という形は想定していないが念のため。
        model = None
    effort = None
    for part in parts:
        if part.startswith("reasoning="):
            effort = part.split("=", 1)[1].strip()
    return model, effort


def _import_reviews(root: Path, rep: Report) -> dict[tuple[str, int, int], int]:
    """ログファイルから reviews を作る。(repo, pr, rev) -> review_id を返す。"""
    index: dict[tuple[str, int, int], int] = {}
    if not root.exists():
        return index

    out_files = [p for p in root.rglob("*") if p.is_file() and _OUT_RE.match(p.name)]
    # 取り込み順が created_at の順序と一致するように古い順で処理する。
    out_files.sort(key=lambda p: p.stat().st_mtime)

    for out_file in out_files:
        m = _OUT_RE.match(out_file.name)
        assert m
        pr_number = int(m.group(1))
        rev = int(m.group(2))
        repo = out_file.parent.name

        try:
            raw = out_file.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            rep.notes.append(f"読めませんでした: {out_file} ({e})")
            continue

        body, header_inner = strip_header(raw)
        if header_inner is None:
            rep.header_absent += 1
        else:
            rep.header_stripped += 1
            if "reasoning=" not in header_inner:
                rep.header_without_reasoning += 1

        # err ログ: 平文と gzip の両方を探す。details と実行時設定はこちらが優先。
        err_file = out_file.with_name(
            out_file.name.replace("_out.log-", "_err.log-")
        )
        if not err_file.exists() and err_file.with_suffix(
            err_file.suffix + ".gz"
        ).exists():
            err_file = err_file.with_suffix(err_file.suffix + ".gz")

        details, fields = ("", {})
        if err_file.exists() and err_file.suffix != ".gz":
            details, fields = review.read_err_header(err_file)

        model = fields.get("model")
        effort = fields.get("reasoning_effort")
        if model or effort:
            rep.meta_from_err += 1
        elif header_inner is not None:
            # err ログが無い/読めない場合だけヘッダ行を補助的に使う。
            model, effort = _meta_from_header(header_inner)
        if not model:
            rep.meta_missing += 1

        try:
            created = reviews.create(
                repo=repo,
                pr_number=pr_number,
                rev=rev,
                body=body.strip(),
                model=model,
                reasoning_effort=effort or None,
                codex_version=fields.get("codex_version"),
                session_id=fields.get("session_id"),
                details=details or None,
                stderr_path=str(err_file) if err_file.exists() else None,
                # 過去分に実行日時の記録は無いので mtime で代用する。
                created_at=out_file.stat().st_mtime,
            )
        except sqlite3.IntegrityError:
            rep.reviews_skipped += 1
            row = db.connect().execute(
                "SELECT id FROM reviews WHERE repo = ? AND pr_number = ? AND rev = ?",
                (repo, pr_number, rev),
            ).fetchone()
            if row:
                index[(repo, pr_number, rev)] = row["id"]
            continue

        index[(repo, pr_number, rev)] = created.id
        rep.reviews_imported += 1

    return index


def _jobs_file() -> Path | None:
    """旧 jobs.json を探す (Docker とホスト直接起動で置き場所が違う)。"""
    candidates = [
        Path(os.environ["REVIEW_JOBS_FILE"]) if os.environ.get("REVIEW_JOBS_FILE")
        else None,
        config.LOGS_PATH / "jobs.json",
        config.BASE_DIR / "data" / "jobs.json",
    ]
    for path in candidates:
        if path and path.exists():
            return path
    return None


def _import_jobs(index: dict[tuple[str, int, int], int], rep: Report) -> None:
    path = _jobs_file()
    if path is None:
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        rep.notes.append(f"jobs.json を読めませんでした: {path} ({e})")
        return

    for item in raw:
        job_id = item.get("id")
        if not job_id:
            continue
        repo = item.get("repo") or ""
        number = item.get("number") or 0
        rev = item.get("rev")
        review_id = index.get((repo, number, rev)) if rev is not None else None

        status = item.get("status") or "failed"
        if status in ("queued", "running"):
            # 過去に中断されたまま残っていたぶん。
            status = "failed"

        try:
            with db.write() as conn:
                conn.execute(
                    "INSERT INTO jobs (id, url, owner, repo, number, status, "
                    "created_at, started_at, finished_at, error, auto_post, "
                    "posted, posting, post_error, review_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)",
                    (
                        job_id, item.get("url") or "", item.get("owner") or "",
                        repo, number, status,
                        item.get("created_at") or time.time(),
                        item.get("started_at"), item.get("finished_at"),
                        item.get("error"), int(bool(item.get("auto_post", True))),
                        int(bool(item.get("posted"))), item.get("post_error"),
                        review_id,
                    ),
                )
                for seq, line in enumerate(item.get("progress") or []):
                    conn.execute(
                        "INSERT INTO job_progress (job_id, seq, at, line) "
                        "VALUES (?, ?, ?, ?)",
                        (job_id, seq, item.get("created_at") or 0.0, line),
                    )
        except sqlite3.IntegrityError:
            continue

        # ジョブが覚えていた PR タイトルもキャッシュに移す。
        if item.get("pr_title"):
            reviews.put_title(repo, number, item["pr_title"])
        rep.jobs_imported += 1


def _import_titles(rep: Report) -> None:
    path = config.LOGS_PATH / "pr_titles.json"
    if not path.exists():
        return
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        rep.notes.append(f"pr_titles.json を読めませんでした: {path} ({e})")
        return

    for key, title in raw.items():
        repo, _, num = key.rpartition("/")
        if not repo or not num.isdigit() or not title:
            continue
        reviews.put_title(repo, int(num), title)
        rep.titles_imported += 1


def run() -> Report:
    """ファイル上の旧データを DB に取り込む。再実行しても安全。"""
    rep = Report()
    index = _import_reviews(config.LOGS_PATH, rep)
    _import_jobs(index, rep)
    _import_titles(rep)
    return rep


def needed() -> bool:
    """DB がまだ空か (= 初回起動で取り込みが要るか)。"""
    return db.connect().execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 0


def main() -> None:
    config.ensure_dirs()
    db.init()
    rep = run()
    for line in rep.lines():
        print(line)


if __name__ == "__main__":
    main()
