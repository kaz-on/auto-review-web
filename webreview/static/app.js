"use strict";

const STATUS_LABEL = {
  queued: "待機中",
  running: "実行中",
  succeeded: "完了",
  failed: "失敗",
};

// ---- ユーティリティ --------------------------------------------------------
function el(tag, opts = {}, children = []) {
  const node = document.createElement(tag);
  if (opts.class) node.className = opts.class;
  if (opts.text != null) node.textContent = opts.text;
  if (opts.href) node.href = opts.href;
  if (opts.attrs) for (const [k, v] of Object.entries(opts.attrs)) node.setAttribute(k, v);
  for (const c of children) if (c) node.appendChild(c);
  return node;
}

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

function fmtTime(epoch) {
  if (!epoch) return "";
  const d = new Date(epoch * 1000);
  const p = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
    `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

// ---- タブ切り替え ----------------------------------------------------------
document.querySelectorAll(".tab-btn").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab-btn").forEach((b) => b.classList.remove("active"));
    document.querySelectorAll(".tab-panel").forEach((p) => p.classList.remove("active"));
    btn.classList.add("active");
    document.getElementById("tab-" + btn.dataset.tab).classList.add("active");
    if (btn.dataset.tab === "summary") loadSummary();
  });
});

// ---- 設定表示 --------------------------------------------------------------
fetchJSON("/api/config").then((cfg) => {
  const model = cfg.reasoning_effort
    ? `${cfg.model}（reasoning: ${cfg.reasoning_effort}）`
    : cfg.model;
  document.getElementById("config-meta").textContent =
    `対象 Organization: ${cfg.org} / モデル: ${model}`;
}).catch(() => {});

// ---- レビュー投入フォーム --------------------------------------------------
const form = document.getElementById("review-form");
const urlInput = document.getElementById("pr-url");
const autoPostInput = document.getElementById("auto-post");
const submitBtn = document.getElementById("submit-btn");
const formMessage = document.getElementById("form-message");

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  formMessage.className = "form-message";
  formMessage.textContent = "";
  submitBtn.disabled = true;
  try {
    const data = await fetchJSON("/api/reviews", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: urlInput.value, auto_post: autoPostInput.checked }),
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
const jobList = document.getElementById("job-list");
const jobEmpty = document.getElementById("job-empty");
const busyBanner = document.getElementById("busy-banner");
const cards = new Map(); // jobId -> { root, refs, loadedResult }

function buildCard(job) {
  const badge = el("span", { class: "badge" });
  const title = el("span", { class: "job-title" });
  const autopost = el("span", { class: "chip hidden", text: "自動投稿" });
  const delBtn = el("button", {
    class: "del-btn secondary hidden", text: "×",
    attrs: { title: "履歴から削除", type: "button" },
  });
  delBtn.addEventListener("click", () => doDelete(job.id));
  const head = el("div", { class: "job-head" },
    [badge, title, autopost, el("span", { class: "spacer" }), delBtn]);
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
    badge, title, autopost, delBtn, sub, current, errorBox,
    actions, progressPre, progressDetails,
  };
  return { root, refs, loadedResult: false };
}

function updateCard(card, job) {
  const r = card.refs;

  // バッジ
  r.badge.className = "badge " + job.status;
  const spinning = job.status === "running" || job.status === "queued";
  r.badge.textContent = "";
  if (spinning) r.badge.appendChild(el("span", { class: "spinner" }));
  r.badge.appendChild(document.createTextNode(STATUS_LABEL[job.status] || job.status));

  // 自動投稿チップ / 削除ボタン
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

  // エラー
  if (job.status === "failed" && job.error) {
    r.errorBox.textContent = job.error;
    r.errorBox.classList.remove("hidden");
  } else {
    r.errorBox.classList.add("hidden");
  }

  // 進捗ログ
  r.progressPre.textContent = (job.progress || []).join("\n");
  r.progressDetails.classList.toggle("hidden", !(job.progress && job.progress.length));

  // アクション (完了時のみ: 結果表示 + 投稿)
  updateActions(card, job);
}

function updateActions(card, job) {
  const r = card.refs;
  if (job.status !== "succeeded") {
    r.actions.textContent = "";
    return;
  }
  // 既に構築済みなら投稿状態だけ更新
  if (!r.postBtn) {
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
          const d = await fetchJSON(`/api/reviews/${job.id}`);
          resultPre.textContent = d.review_text || "(空)";
        } catch (err) {
          resultPre.textContent = err.message;
        }
      }
    });

    const postBtn = el("button", { text: "PR に投稿" });
    const postStatus = el("span", { class: "post-status" });
    postBtn.addEventListener("click", () => doPost(job.id, card));

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
  const btn = r.postBtn, st = r.postStatus;
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

async function doDelete(jobId) {
  if (!confirm("このレビューを履歴から削除しますか?（ログファイルは残ります）")) return;
  try {
    await fetchJSON(`/api/reviews/${jobId}`, { method: "DELETE" });
    await refreshJobs();
  } catch (err) {
    alert(err.message);
  }
}

async function doPost(jobId, card) {
  const btn = card.refs.postBtn;
  btn.disabled = true;
  try {
    await fetchJSON(`/api/reviews/${jobId}/post`, { method: "POST" });
    await refreshJobs();
  } catch (err) {
    card.refs.postStatus.className = "post-status err";
    card.refs.postStatus.textContent = err.message;
    btn.disabled = false;
  }
}

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
    const data = await fetchJSON("/api/reviews");
    renderJobs(data);
  } catch (_) { /* ポーリングの一時的失敗は無視 */ }
}

// ---- 集計 ------------------------------------------------------------------
const summaryList = document.getElementById("summary-list");
const summaryStatus = document.getElementById("summary-status");
document.getElementById("reload-summary").addEventListener("click", loadSummary);

let summaryLoading = false;
async function loadSummary() {
  if (summaryLoading) return;
  summaryLoading = true;
  summaryStatus.textContent = "読み込み中...";
  try {
    const data = await fetchJSON("/api/summary");
    renderSummary(data.entries || []);
    summaryStatus.textContent = `${data.entries.length} 件`;
  } catch (err) {
    summaryStatus.textContent = "読み込みに失敗しました: " + err.message;
  } finally {
    summaryLoading = false;
  }
}

function renderSummary(entries) {
  summaryList.textContent = "";
  for (const e of entries) {
    const heading = el("h3", {}, [
      el("a", {
        href: e.url, text: `${e.repo} #${e.pr_num} — ${e.title}`,
        attrs: { target: "_blank", rel: "noopener" },
      }),
    ]);
    const meta = el("div", { class: "meta", text: `${e.mtime}（rev-${e.rev}）` });

    // 一覧性を優先し、レビュー結果は折りたたんで表示する。
    const reviewDetails = el("details", {}, [
      el("summary", { text: "レビュー結果を表示" }),
      el("pre", { class: "code", text: e.review_text }),
    ]);

    const children = [heading, meta, reviewDetails];
    if (e.details) {
      children.push(el("details", {}, [
        el("summary", { text: "詳細情報" }),
        el("pre", { class: "code", text: e.details }),
      ]));
    }
    summaryList.appendChild(el("div", { class: "summary-item" }, children));
  }
  if (!entries.length) {
    summaryList.appendChild(el("p", { class: "empty", text: "集計対象のレビューはありません。" }));
  }
}

// ---- ポーリング開始 --------------------------------------------------------
refreshJobs();
setInterval(refreshJobs, 2000);
