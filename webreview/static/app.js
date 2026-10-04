// @ts-check
// 型検査は `npx -p typescript@5 tsc --noEmit` (設定は tsconfig.json)。API の型は static/types.d.ts。
"use strict";

/** @type {Record<JobStatus, string>} */
const STATUS_LABEL = {
  queued: "待機中",
  running: "実行中",
  succeeded: "完了",
  failed: "失敗",
};

// ---- ユーティリティ --------------------------------------------------------
/**
 * @typedef {object} ElOptions
 * @property {string} [class]
 * @property {string} [text]
 * @property {string} [href]
 * @property {Record<string, string>} [attrs]
 */

/**
 * @template {keyof HTMLElementTagNameMap} K
 * @param {K} tag
 * @param {ElOptions} [opts]
 * @param {(Node | null | undefined)[]} [children]
 * @returns {HTMLElementTagNameMap[K]}
 */
function el(tag, opts = {}, children = []) {
  const node = document.createElement(tag);
  if (opts.class) node.className = opts.class;
  if (opts.text != null) node.textContent = opts.text;
  // href を持つのは a などに限られるので、型検査のうえではここだけ絞り込む。
  if (opts.href) /** @type {HTMLAnchorElement} */ (node).href = opts.href;
  if (opts.attrs) for (const [k, v] of Object.entries(opts.attrs)) node.setAttribute(k, v);
  for (const c of children) if (c) node.appendChild(c);
  return node;
}

/**
 * JSON API を叩く。レスポンスの型は呼び出し側で注釈する (types.d.ts を参照)。
 * @param {string} url
 * @param {RequestInit} [options]
 * @returns {Promise<any>}
 */
async function fetchJSON(url, options) {
  const res = await fetch(url, options);
  let data = null;
  try { data = await res.json(); } catch (_) { /* ignore */ }
  if (!res.ok) {
    const msg = (data && data.error) || `エラー (HTTP ${res.status})`;
    throw new Error(msg);
  }
  return data;
}

/**
 * @param {number | null} epoch
 * @returns {string}
 */
function fmtTime(epoch) {
  if (!epoch) return "";
  const d = new Date(epoch * 1000);
  /** @param {number} n */
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
    `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

// ---- タブ切り替え ----------------------------------------------------------
const tabButtons =
  /** @type {NodeListOf<HTMLButtonElement>} */ (document.querySelectorAll(".tab-btn"));
tabButtons.forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    const panel =
      /** @type {HTMLElement} */ (document.getElementById("tab-" + btn.dataset.tab));
    panel.classList.add("active");
    if (btn.dataset.tab === "summary") loadSummary();
  });
});

// ---- 説明文 (REVIEW_DESCRIPTION) -------------------------------------------
// 書式: 空行で段落を分け、行頭 "- " の行は箇条書きになる。
// リンクは [表示文字列](URL) 形式か、裸の http(s) URL を書く。
// HTML は解釈せず DOM 構築だけで描画するため、設定値がマークアップとして
// 埋め込まれることはない。
// 裸の URL は RFC 3986 で使える文字だけを拾う (和文の約物を巻き込まないため)。
const LINK_RE =
  /\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)|(https?:\/\/[\w\-._~:/?#@!$&'*+,;=%[\]]+)/g;

/**
 * @param {HTMLElement} parent
 * @param {string} text
 */
function appendInline(parent, text) {
  let last = 0;
  for (const m of text.matchAll(LINK_RE)) {
    let [matched, label, url, bare] = m;
    if (bare) {
      // 「...です。」のように文末の約物まで URL に取り込まないよう削る。
      const trail = bare.match(/[.,:;!?)\]、。」）]+$/);
      if (trail) {
        bare = bare.slice(0, -trail[0].length);
        matched = bare;
      }
    }
    if (m.index > last) {
      parent.appendChild(document.createTextNode(text.slice(last, m.index)));
    }
    parent.appendChild(el("a", {
      href: url || bare, text: label || bare,
      attrs: { target: "_blank", rel: "noopener" },
    }));
    last = m.index + matched.length;
  }
  if (last < text.length) parent.appendChild(document.createTextNode(text.slice(last)));
}

/** @param {string} text */
function renderDescription(text) {
  const box = /** @type {HTMLDivElement} */ (document.getElementById("description"));
  if (!text) return; // 未設定なら領域ごと出さない
  for (const block of text.split(/\n\s*\n/)) {
    const lines = block.split("\n").map((l) => l.trim()).filter((l) => l);
    if (!lines.length) continue;
    if (lines.every((l) => l.startsWith("- "))) {
      box.appendChild(el("ul", {}, lines.map((line) => {
        const li = el("li");
        appendInline(li, line.slice(2));
        return li;
      })));
    } else {
      const p = el("p");
      lines.forEach((line, i) => {
        if (i) p.appendChild(el("br"));
        appendInline(p, line);
      });
      box.appendChild(p);
    }
  }
  box.classList.remove("hidden");
}

// ---- 設定表示 --------------------------------------------------------------
fetchJSON("/api/config").then((/** @type {ConfigResponse} */ cfg) => {
  const model = cfg.reasoning_effort
    ? `${cfg.model}（reasoning: ${cfg.reasoning_effort}）`
    : cfg.model;
  const meta = /** @type {HTMLElement} */ (document.getElementById("config-meta"));
  meta.textContent = `対象 Organization: ${cfg.org} / モデル: ${model}`;
  renderDescription(cfg.description);
}).catch(() => {});

// ---- レビュー投入フォーム --------------------------------------------------
const form = /** @type {HTMLFormElement} */ (document.getElementById("review-form"));
const urlInput = /** @type {HTMLInputElement} */ (document.getElementById("pr-url"));
const autoPostInput = /** @type {HTMLInputElement} */ (document.getElementById("auto-post"));
const includeQualityInput =
  /** @type {HTMLInputElement} */ (document.getElementById("include-quality"));
const submitBtn = /** @type {HTMLButtonElement} */ (document.getElementById("submit-btn"));
const formMessage = /** @type {HTMLParagraphElement} */ (document.getElementById("form-message"));

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  formMessage.className = "form-message";
  formMessage.textContent = "";
  submitBtn.disabled = true;
  try {
    /** @type {CreateReviewResponse} */
    const data = await fetchJSON("/api/reviews", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url: urlInput.value,
        include_quality: includeQualityInput.checked,
        auto_post: autoPostInput.checked,
      }),
    });
    formMessage.classList.add("ok");
    formMessage.textContent = data.was_busy
      ? "他のレビューが実行中のため、キューに追加しました。"
      : "レビューを開始しました。";
    urlInput.value = "";
    await refreshJobs();
  } catch (err) {
    formMessage.classList.add("error");
    formMessage.textContent = err.message;
  } finally {
    submitBtn.disabled = false;
  }
});

// ---- ジョブ一覧 (差分更新) --------------------------------------------------
const jobList = /** @type {HTMLDivElement} */ (document.getElementById("job-list"));
const jobEmpty = /** @type {HTMLParagraphElement} */ (document.getElementById("job-empty"));
const busyBanner = /** @type {HTMLDivElement} */ (document.getElementById("busy-banner"));

/**
 * カード内で更新し続ける要素。postBtn / postStatus はジョブが完了したときに
 * 初めて作られるので省略可能。
 * @typedef {object} CardRefs
 * @property {HTMLSpanElement} badge
 * @property {HTMLSpanElement} title
 * @property {HTMLSpanElement} quality
 * @property {HTMLSpanElement} autopost
 * @property {HTMLButtonElement} delBtn
 * @property {HTMLDivElement} sub
 * @property {HTMLDivElement} current
 * @property {HTMLDivElement} errorBox
 * @property {HTMLDivElement} actions
 * @property {HTMLPreElement} progressPre
 * @property {HTMLDetailsElement} progressDetails
 * @property {HTMLButtonElement} [postBtn]
 * @property {HTMLSpanElement} [postStatus]
 */

/**
 * @typedef {object} Card
 * @property {HTMLDivElement} root
 * @property {CardRefs} refs
 * @property {boolean} loadedResult
 * @property {boolean} loadedProgress
 * @property {boolean} needsProgressFetch
 * @property {() => void} fetchProgress
 */

/** @type {Map<string, Card>} */
const cards = new Map(); // jobId -> Card

/**
 * @param {Job} job
 * @returns {Card}
 */
function buildCard(job) {
  const badge = el("span", { class: "badge" });
  const title = el("span", { class: "job-title" });
  const quality = el("span", { class: "chip hidden", text: "品質改善" });
  const autopost = el("span", { class: "chip hidden", text: "自動投稿" });
  const delBtn = el("button", {
    class: "del-btn secondary hidden", text: "×",
    attrs: { title: "履歴から削除", type: "button" },
  });
  delBtn.addEventListener("click", () => doDelete(job.id));
  const head = el("div", { class: "job-head" },
    [badge, title, quality, autopost, el("span", { class: "spacer" }), delBtn]);
  const sub = el("div", { class: "job-sub" });
  const current = el("div", { class: "job-sub" });
  const errorBox = el("div", { class: "error-box hidden" });

  const actions = el("div", { class: "job-actions" });

  const progressPre = el("pre", { class: "code" });
  const progressDetails = el("details", {}, [
    el("summary", { text: "進捗ログ" }), progressPre,
  ]);

  const root = el("div", { class: "job" },
    [head, sub, current, errorBox, actions, progressDetails]);

  const refs = {
    badge, title, quality, autopost, delBtn, sub, current, errorBox,
    actions, progressPre, progressDetails,
  };
  // 終了したジョブの進捗は一覧に含まれないので、必要になった時点で取りに行く。
  // 開かれたときだけでなく、開いたままジョブが終わったときにも呼ばれる。
  async function fetchProgress() {
    if (card.loadedProgress || !card.needsProgressFetch) return;
    card.loadedProgress = true;
    try {
      /** @type {JobDetail} */
      const d = await fetchJSON(`/api/reviews/${job.id}`);
      progressPre.textContent = (d.progress || []).join("\n");
    } catch (err) {
      // 次のポーリングか開き直しで取り直せるように戻す。
      card.loadedProgress = false;
      progressPre.textContent = err.message;
    }
  }

  /** @type {Card} */
  const card = {
    root, refs, loadedResult: false, loadedProgress: false,
    needsProgressFetch: false, fetchProgress,
  };

  progressDetails.addEventListener("toggle", () => {
    if (progressDetails.open) fetchProgress();
  });

  return card;
}

/**
 * @param {Card} card
 * @param {Job} job
 */
function updateCard(card, job) {
  const r = card.refs;

  // バッジ
  r.badge.className = "badge " + job.status;
  const spinning = job.status === "running" || job.status === "queued";
  r.badge.textContent = "";
  if (spinning) r.badge.appendChild(el("span", { class: "spinner" }));
  r.badge.appendChild(document.createTextNode(STATUS_LABEL[job.status] || job.status));

  // 品質改善・自動投稿チップ / 削除ボタン
  r.quality.classList.toggle("hidden", !job.include_quality);
  r.autopost.classList.toggle("hidden", !job.auto_post);
  const terminal = job.status === "succeeded" || job.status === "failed";
  r.delBtn.classList.toggle("hidden", !terminal);

  // タイトル (リンク)
  r.title.textContent = "";
  const label = `${job.repo} #${job.number}` + (job.pr_title ? ` — ${job.pr_title}` : "");
  r.title.appendChild(el("a", {
    href: job.url, text: label,
    attrs: { target: "_blank", rel: "noopener" },
  }));

  // タイムスタンプ
  const parts = [`投入: ${fmtTime(job.created_at)}`];
  if (job.started_at) parts.push(`開始: ${fmtTime(job.started_at)}`);
  if (job.finished_at) parts.push(`終了: ${fmtTime(job.finished_at)}`);
  r.sub.textContent = parts.join(" / ");

  // 実行中の現在ステップ
  if (job.status === "running" && job.progress && job.progress.length) {
    r.current.textContent = "現在: " + job.progress[job.progress.length - 1];
    r.current.classList.remove("hidden");
  } else {
    r.current.classList.add("hidden");
  }

  // 終了したジョブの進捗は一覧に載らない (progress は null)。開かれた時点で取得する。
  if (!job.progress && job.progress_count && !card.loadedProgress) {
    card.needsProgressFetch = true;
    // 実行中から開いたままだと toggle が起きないので、ここで取りに行く。
    // 同期的に loadedProgress が立つので、下のプレースホルダには入れ替わらない。
    if (r.progressDetails.open) card.fetchProgress();
  }

  // エラー
  if (job.status === "failed" && job.error) {
    r.errorBox.textContent = job.error;
    r.errorBox.classList.remove("hidden");
  } else {
    r.errorBox.classList.add("hidden");
  }

  // 進捗ログ。実行中は一覧のポーリングで届く内容をそのまま反映し、終了後は
  // 開かれたときに取得した内容を保持する (再取得しない)。
  if (job.progress) {
    r.progressPre.textContent = job.progress.join("\n");
  } else if (!card.loadedProgress) {
    r.progressPre.textContent = "開くと読み込みます...";
  }
  r.progressDetails.classList.toggle("hidden", !job.progress_count);

  // アクション (完了時のみ: 結果表示 + 投稿)
  updateActions(card, job);
}

/**
 * @param {Card} card
 * @param {Job} job
 */
function updateActions(card, job) {
  const r = card.refs;
  if (job.status !== "succeeded") {
    r.actions.textContent = "";
    return;
  }
  // 既に構築済みなら投稿状態だけ更新
  let btn = r.postBtn;
  let st = r.postStatus;
  if (!btn || !st) {
    r.actions.textContent = "";

    // レビュー結果 (遅延読み込み)
    const resultPre = el("pre", { class: "code", text: "読み込み中..." });
    const resultDetails = el("details", {}, [
      el("summary", { text: "レビュー結果を表示" }), resultPre,
    ]);
    resultDetails.addEventListener("toggle", async () => {
      if (resultDetails.open && !card.loadedResult) {
        card.loadedResult = true;
        try {
          /** @type {JobDetail} */
          const d = await fetchJSON(`/api/reviews/${job.id}`);
          resultPre.textContent = d.review_text || "(空)";
        } catch (err) {
          resultPre.textContent = err.message;
        }
      }
    });

    const postBtn = el("button", { text: "PR に投稿" });
    const postStatus = el("span", { class: "post-status" });
    postBtn.addEventListener("click", () => doPost(job.id, postBtn, postStatus));

    btn = postBtn;
    st = postStatus;
    r.postBtn = postBtn;
    r.postStatus = postStatus;
    r.actions.appendChild(el("div", {}, [resultDetails]));
    r.actions.appendChild(postBtn);
    r.actions.appendChild(postStatus);
    // resultDetails をアクション行の外(上)に置くとレイアウトが崩れるためまとめて格納
    r.actions.style.flexDirection = "column";
    r.actions.style.alignItems = "flex-start";
  }

  // 投稿状態
  st.className = "post-status";
  if (job.posted) {
    btn.classList.add("hidden");
    st.classList.add("ok");
    st.textContent = "投稿済み ✓";
  } else if (job.posting) {
    btn.disabled = true;
    btn.textContent = "投稿中...";
    st.textContent = "";
  } else {
    btn.classList.remove("hidden");
    btn.disabled = false;
    btn.textContent = "PR に投稿";
    if (job.post_error) {
      st.classList.add("err");
      st.textContent = "投稿に失敗しました:\n" + job.post_error;
    } else {
      st.textContent = "";
    }
  }
}

/** @param {string} jobId */
async function doDelete(jobId) {
  if (!confirm("このレビューを履歴から削除しますか?（ログファイルは残ります）")) return;
  try {
    await fetchJSON(`/api/reviews/${jobId}`, { method: "DELETE" });
    await refreshJobs();
  } catch (err) {
    alert(err.message);
  }
}

/**
 * @param {string} jobId
 * @param {HTMLButtonElement} btn
 * @param {HTMLSpanElement} status
 */
async function doPost(jobId, btn, status) {
  btn.disabled = true;
  try {
    await fetchJSON(`/api/reviews/${jobId}/post`, { method: "POST" });
    await refreshJobs();
  } catch (err) {
    status.className = "post-status err";
    status.textContent = err.message;
    btn.disabled = false;
  }
}

/** @param {ReviewsResponse} data */
function renderJobs(data) {
  const jobs = data.jobs || [];
  jobEmpty.classList.toggle("hidden", jobs.length > 0);

  // busy バナー
  if (data.busy) {
    busyBanner.classList.remove("hidden");
    busyBanner.textContent = "現在レビューを実行中です。新しい投入はキューに追加されます。";
  } else {
    busyBanner.classList.add("hidden");
  }

  /** @type {Set<string>} */
  const seen = new Set();
  for (const job of jobs) {
    seen.add(job.id);
    let card = cards.get(job.id);
    if (!card) {
      card = buildCard(job);
      cards.set(job.id, card);
    }
    updateCard(card, job);
    jobList.appendChild(card.root); // 既存要素の再 append は並べ替えとして機能
  }

  // 消えたジョブを除去
  for (const [id, card] of cards) {
    if (!seen.has(id)) {
      card.root.remove();
      cards.delete(id);
    }
  }
}

async function refreshJobs() {
  try {
    /** @type {ReviewsResponse} */
    const data = await fetchJSON("/api/reviews");
    renderJobs(data);
  } catch (_) { /* ポーリングの一時的失敗は無視 */ }
}

// ---- 集計 ------------------------------------------------------------------
// 一覧はメタデータだけを取得する。本文は件数に比例して重くなるため、行が
// 開かれた時点で /api/summary/<id> から個別に読み込む。
const summaryList = /** @type {HTMLDivElement} */ (document.getElementById("summary-list"));
const summaryStatus = /** @type {HTMLParagraphElement} */ (document.getElementById("summary-status"));
const summaryRepo = /** @type {HTMLSelectElement} */ (document.getElementById("summary-repo"));
const summaryQ = /** @type {HTMLInputElement} */ (document.getElementById("summary-q"));
const summaryPager = /** @type {HTMLDivElement} */ (document.getElementById("summary-pager"));
const summaryRange = /** @type {HTMLSpanElement} */ (document.getElementById("summary-range"));
const summaryPrev = /** @type {HTMLButtonElement} */ (document.getElementById("summary-prev"));
const summaryNext = /** @type {HTMLButtonElement} */ (document.getElementById("summary-next"));

const PAGE_SIZE = 50;
let summaryOffset = 0;
let summaryLoading = false;
let summaryTotal = 0;

const reloadSummaryBtn =
  /** @type {HTMLButtonElement} */ (document.getElementById("reload-summary"));
reloadSummaryBtn.addEventListener("click", () => {
  summaryOffset = 0;
  loadSummary();
});
summaryRepo.addEventListener("change", () => { summaryOffset = 0; loadSummary(); });
summaryPrev.addEventListener("click", () => {
  summaryOffset = Math.max(0, summaryOffset - PAGE_SIZE);
  loadSummary();
});
summaryNext.addEventListener("click", () => {
  if (summaryOffset + PAGE_SIZE < summaryTotal) {
    summaryOffset += PAGE_SIZE;
    loadSummary();
  }
});

// 入力のたびに投げず、打ち終わりを待ってから検索する。
/** @type {number | undefined} */
let searchTimer;
summaryQ.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => { summaryOffset = 0; loadSummary(); }, 300);
});

/**
 * @param {number | null} n
 * @returns {string}
 */
function fmtBytes(n) {
  if (n == null) return "";
  return n < 1024 ? `${n} B` : `${(n / 1024).toFixed(1)} KB`;
}

async function loadSummary() {
  if (summaryLoading) return;
  summaryLoading = true;
  summaryStatus.textContent = "読み込み中...";
  try {
    const params = new URLSearchParams({
      limit: String(PAGE_SIZE), offset: String(summaryOffset),
    });
    if (summaryRepo.value) params.set("repo", summaryRepo.value);
    if (summaryQ.value.trim()) params.set("q", summaryQ.value.trim());

    /** @type {SummaryResponse} */
    const data = await fetchJSON("/api/summary?" + params);
    summaryTotal = data.total || 0;
    syncRepoOptions(data.repos || []);
    const entries = data.entries || [];
    renderSummary(entries);
    updatePager(entries.length);
  } catch (err) {
    summaryStatus.textContent = "読み込みに失敗しました: " + err.message;
  } finally {
    summaryLoading = false;
  }
}

/** @param {string[]} repos */
function syncRepoOptions(repos) {
  // 選択中の値は保ったまま、選択肢だけ実データに合わせる。
  const current = summaryRepo.value;
  if (summaryRepo.dataset.repos === repos.join("\u0000")) return;
  summaryRepo.dataset.repos = repos.join("\u0000");
  summaryRepo.textContent = "";
  // value 属性を省くと option の値が表示文字列になってしまうため必ず指定する。
  summaryRepo.appendChild(el("option", {
    text: "すべてのリポジトリ", attrs: { value: "" },
  }));
  for (const repo of repos) {
    summaryRepo.appendChild(el("option", { text: repo, attrs: { value: repo } }));
  }
  summaryRepo.value = repos.includes(current) ? current : "";
}

/** @param {number} shown */
function updatePager(shown) {
  if (!summaryTotal) {
    summaryStatus.textContent = "0 件";
    summaryPager.classList.add("hidden");
    return;
  }
  const from = summaryOffset + 1;
  const to = summaryOffset + shown;
  summaryStatus.textContent = `${summaryTotal} 件`;
  summaryRange.textContent = `${from}–${to} / ${summaryTotal}`;
  summaryPrev.disabled = summaryOffset === 0;
  summaryNext.disabled = summaryOffset + PAGE_SIZE >= summaryTotal;
  summaryPager.classList.toggle("hidden", summaryTotal <= PAGE_SIZE);
}

/** @param {SummaryEntry[]} entries */
function renderSummary(entries) {
  summaryList.textContent = "";
  for (const e of entries) {
    const heading = el("h3", {}, [
      el("a", {
        href: e.url, text: `${e.repo} #${e.pr_num}` + (e.title ? ` — ${e.title}` : ""),
        attrs: { target: "_blank", rel: "noopener" },
      }),
    ]);
    const bits = [fmtTime(e.created_at), `rev-${e.rev}`];
    if (e.model) {
      bits.push(e.reasoning_effort ? `${e.model} (${e.reasoning_effort})` : e.model);
    }
    if (e.include_quality) bits.push("品質改善");
    bits.push(fmtBytes(e.body_bytes));
    const meta = el("div", { class: "meta", text: bits.join(" / ") });

    const children = [heading, meta, buildSummaryDetails(e)];
    summaryList.appendChild(el("div", { class: "summary-item" }, children));
  }
  if (!entries.length) {
    summaryList.appendChild(el("p", {
      class: "empty",
      text: "該当するレビューはありません。",
    }));
  }
}

/**
 * @param {SummaryEntry} entry
 * @returns {HTMLDivElement}
 */
function buildSummaryDetails(entry) {
  const reviewPre = el("pre", { class: "code", text: "開くと読み込みます..." });
  const reviewDetails = el("details", {}, [
    el("summary", { text: "レビュー結果を表示" }), reviewPre,
  ]);
  const detailsPre = el("pre", { class: "code" });
  const detailsBox = el("details", { class: "hidden" }, [
    el("summary", { text: "詳細情報" }), detailsPre,
  ]);

  let loaded = false;
  reviewDetails.addEventListener("toggle", async () => {
    if (!reviewDetails.open || loaded) return;
    loaded = true;
    try {
      /** @type {SummaryDetail} */
      const d = await fetchJSON(`/api/summary/${entry.id}`);
      reviewPre.textContent = d.review_text || "(空)";
      if (d.details) {
        detailsPre.textContent = d.details;
        detailsBox.classList.remove("hidden");
      }
    } catch (err) {
      loaded = false;
      reviewPre.textContent = err.message;
    }
  });

  return el("div", {}, [reviewDetails, detailsBox]);
}

// ---- ポーリング開始 --------------------------------------------------------
refreshJobs();
setInterval(refreshJobs, 2000);
