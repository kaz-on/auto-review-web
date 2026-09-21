/**
 * フロントエンドが受け取る API レスポンスの形。
 *
 * サーバ側 (server.py / store.py / reviews.py) の辞書を手で写したもので、
 * 自動では同期しない。API のフィールドを変えたらここも直すこと。
 *
 * このファイルは型検査 (npx tsc --noEmit) だけに使い、ブラウザへは配信しない
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
  jobs: Job[];
  busy: boolean;
}

/** POST /api/reviews */
interface CreateReviewResponse {
  job: Job;
  was_busy: boolean;
}

/** GET /api/config。いずれも未設定なら空文字 (null にはならない)。 */
interface ConfigResponse {
  org: string;
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
