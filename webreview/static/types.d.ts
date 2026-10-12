/**
 * フロントエンドが受け取る API レスポンスの形。
 *
 * サーバ側 (server.py / store.py / reviews.py) の辞書を手で写したもので、
 * 自動では同期しない。API のフィールドを変えたらここも直すこと。
 *
 * このファイルは型検査 (npx -p typescript@5 tsc --noEmit) だけに使い、ブラウザへは配信しない
 * (index.html が読むのは app.js のみ)。
 */

/** ジョブの状態。store.py の QUEUED / RUNNING / SUCCEEDED / FAILED に対応する。 */
type JobStatus = "queued" | "running" | "succeeded" | "failed";

/** store.Job.to_dict() に server._job_public() が has_result を足したもの。 */
interface Job {
  id: string;
  url: string;
  owner: string;
  repo: string;
  number: number;
  status: JobStatus;
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
  error: string | null;
  auto_post: boolean;
  include_quality: boolean;
  posted: boolean;
  posting: boolean;
  post_error: string | null;
  review_id: number | null;
  pr_title: string | null;
  rev: number | null;
  base_ref: string | null;
  progress_count: number;
  /** 実行中・待機中のジョブにだけ載る。終了後は null で、詳細から取り直す。 */
  progress: string[] | null;
  has_result: boolean;
}

/** GET /api/reviews/<job_id>。結果があるジョブには本文が付く。 */
interface JobDetail extends Job {
  review_text?: string;
}

/** GET /api/reviews */
interface ReviewsResponse {
  /** 新しい順に最大 limit 件。 */
  jobs: Job[];
  /** 履歴に残っているジョブの総数。 */
  total: number;
  /** 実際に適用した件数の上限 (要求値をサーバの上限で切り詰めたもの)。 */
  limit: number;
  busy: boolean;
}

/** POST /api/reviews (201)。投入したジョブを返す。 */
interface CreateReviewResponse {
  skipped: false;
  job: Job;
  was_busy: boolean;
}

/**
 * POST /api/reviews (200)。skip_if_reviewed を指定し、同じ PR のレビューか
 * 待機中・実行中のジョブが既にあったため投入しなかった。画面からは指定しない。
 */
interface SkippedReviewResponse {
  skipped: true;
  /** 待機中・実行中のジョブ。無ければ null。 */
  job: Job | null;
  /** 最新のレビュー (reviews.id)。まだ無ければ null。 */
  review_id: number | null;
}

/** GET /api/config。いずれも未設定なら空文字 (null にはならない)。 */
interface ConfigResponse {
  owner: string;
  model: string;
  reasoning_effort: string;
  description: string;
}

/** GET /api/summary の 1 件 (reviews.list_summary)。本文は含まない。 */
interface SummaryEntry {
  id: number;
  repo: string;
  pr_num: number;
  rev: number;
  created_at: number;
  url: string;
  title: string;
  model: string | null;
  reasoning_effort: string | null;
  include_quality: boolean;
  body_bytes: number;
  has_details: boolean;
}

/** GET /api/summary */
interface SummaryResponse {
  entries: SummaryEntry[];
  total: number;
  limit: number;
  offset: number;
  repos: string[];
}

/** GET /api/summary/<review_id> (reviews.get_detail) */
interface SummaryDetail {
  id: number;
  review_text: string;
  details: string;
}
