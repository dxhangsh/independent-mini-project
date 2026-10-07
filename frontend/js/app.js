/* PicNamer 前端逻辑：工作台（批量扫描→进度→复核→落盘）+ 回滚 + 设置
 * 原生 JS，无依赖。所有与后端的交互都走 /api/*。 */
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  planKey: null,
  plan: null,          // 后端 plan dict（含 rows）
  pollTimer: null,     // 批量轮询
};

/* ---------------- 通用 ---------------- */

async function api(path, opts = {}) {
  const res = await fetch("/api" + path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  if (!res.ok) {
    let msg = `HTTP ${res.status}`;
    try { msg = (await res.json()).detail || msg; } catch (e) { /* keep */ }
    throw new Error(msg);
  }
  return res.json();
}

/* ---------------- 标签页 ---------------- */

$$(".tab").forEach((btn) => {
  btn.addEventListener("click", () => {
    $$(".tab").forEach((b) => b.classList.remove("active"));
    $$(".view").forEach((v) => v.classList.remove("active"));
    btn.classList.add("active");
    $("#view-" + btn.dataset.view).classList.add("active");
    if (btn.dataset.view === "rollback") loadBatches();
    if (btn.dataset.view === "settings") loadSettings();
  });
});

/* ---------------- 健康指示 ---------------- */

let lastHealth = null;

async function refreshHealth() {
  const dot = $("#health .dot");
  const txt = $("#health .txt");
  try {
    const h = await api("/health");
    lastHealth = h;
    dot.className = "dot " + (h.ping.ok ? "ok" : "bad");
    txt.textContent = h.ping.ok
      ? `${h.provider === "local_ollama" ? "本地" : "云端"} · ${h.model}`
      : "AI 服务未连接";
  } catch (e) {
    lastHealth = null;
    dot.className = "dot bad";
    txt.textContent = "后端不可达";
  }
}

/* ---------------- 工作台：批量扫描 ---------------- */

const RECENT_KEY = "picnamer-recent-folders";

function loadRecents() {
  let list = [];
  try { list = JSON.parse(localStorage.getItem(RECENT_KEY) || "[]"); } catch (e) { /* ignore */ }
  const box = $("#recent-folders");
  box.innerHTML = "";
  if (!list.length) return;
  const label = document.createElement("span");
  label.className = "muted";
  label.textContent = "最近用过：";
  box.appendChild(label);
  list.forEach((p) => {
    const chip = document.createElement("button");
    chip.className = "chip";
    chip.textContent = p.split(/[\\/]/).filter(Boolean).pop() || p;
    chip.title = p;
    chip.addEventListener("click", () => {
      // 点一个近期路径 = 填入它（保留其他行，方便拼多目录）
      const cur = $("#folders").value.trim();
      $("#folders").value = cur ? cur + "\n" + p : p;
    });
    box.appendChild(chip);
  });
}

function rememberFolders(lines) {
  let list = [];
  try { list = JSON.parse(localStorage.getItem(RECENT_KEY) || "[]"); } catch (e) { /* ignore */ }
  lines.forEach((p) => {
    list = list.filter((x) => x !== p);
    list.unshift(p);
  });
  list = list.slice(0, 6);
  localStorage.setItem(RECENT_KEY, JSON.stringify(list));
  loadRecents();
}

$("#btn-scan").addEventListener("click", startBatch);

async function startBatch() {
  const lines = $("#folders").value.split("\n").map((s) => s.trim()).filter(Boolean);
  if (!lines.length) { alert("请先粘贴图片文件夹路径（每行一个）"); return; }
  if (lastHealth && !lastHealth.ping.ok) {
    const go = confirm(`AI 服务未连接：${lastHealth.ping.detail}\n\n`
      + "请先启动 Ollama（或在「设置」页切换云端 API）。仍要尝试吗？");
    if (!go) return;
  }
  const btn = $("#btn-scan");
  btn.disabled = true;
  try {
    await api("/batch", {
      method: "POST",
      body: JSON.stringify({ folders: lines, product_word: $("#product-word").value.trim() }),
    });
    rememberFolders(lines);
    $("#review-card").classList.add("hidden");
    openBatchPolling();
  } catch (e) {
    alert("启动失败：" + e.message);
  } finally {
    btn.disabled = false;
  }
}

$("#btn-resume").addEventListener("click", async () => {
  try {
    await api("/batch/resume", { method: "POST", body: "{}" });
    openBatchPolling();
  } catch (e) {
    alert("续跑失败：" + e.message);
  }
});

$("#btn-cancel").addEventListener("click", async () => {
  try {
    await api("/batch/cancel", { method: "POST", body: "{}" });
    renderBatch(await api("/batch"));
  } catch (e) { /* ignore */ }
});

function openBatchPolling() {
  $("#batch-card").classList.remove("hidden");
  $("#btn-cancel").classList.remove("hidden");
  stopBatchPolling();
  state.pollTimer = setInterval(pollBatch, 900);
  pollBatch();
}

function stopBatchPolling() {
  if (state.pollTimer) { clearInterval(state.pollTimer); state.pollTimer = null; }
  $("#btn-cancel").classList.add("hidden");
}

async function pollBatch() {
  let b;
  try { b = await api("/batch"); }
  catch (e) { stopBatchPolling(); alert("查询进度失败：" + e.message); return; }
  renderBatch(b);
  if (b.exists && !b.running) {
    stopBatchPolling();
    refreshHealth();
    // 单目录批次直接进入复核；多目录留给用户挑
    const done = (b.folders || []).filter((f) => f.status === "done");
    if (done.length === 1 && (b.folders || []).length === 1) openPlan(done[0].plan_key);
  }
}

const FOLDER_BADGE = {
  pending: '<span class="badge gray">等待</span>',
  running: '<span class="badge blue">处理中</span>',
  done: '<span class="badge green">完成</span>',
  error: '<span class="badge red">失败</span>',
};

function renderBatch(b) {
  if (!b.exists) { $("#batch-card").classList.add("hidden"); maybeShowResume(b); return; }
  $("#batch-card").classList.remove("hidden");
  const folders = b.folders || [];
  const done = folders.filter((f) => f.status === "done").length;
  $("#batch-overall").textContent = `已完成 ${done} / ${folders.length}`;
  const list = $("#batch-list");
  list.innerHTML = "";
  folders.forEach((f) => {
    const row = document.createElement("div");
    row.className = "batch-row" + (f.status === "running" ? " active" : "");
    const name = f.path.split(/[\\/]/).filter(Boolean).pop() || f.path;
    let inner = `<div class="batch-line">${FOLDER_BADGE[f.status] || ""}
      <b>${escapeHtml(name)}</b>
      <span class="muted">${f.status === "running" && f.total ? `${f.done}/${f.total} · ${escapeHtml(f.current)}` : escapeHtml(f.path)}</span></div>`;
    if (f.status === "done") inner += `<div class="batch-line muted">共 ${f.total} 张 · 待复核 ${f.review} 张</div>`;
    if (f.status === "error") inner += `<div class="batch-line err-text">${escapeHtml(f.error || "")}</div>`;
    row.innerHTML = inner;
    if (f.status === "done" && f.plan_key) {
      const btn = document.createElement("button");
      btn.textContent = f.review ? "去复核" : "查看结果";
      btn.addEventListener("click", () => openPlan(f.plan_key));
      row.appendChild(btn);
      // 单目录重扫（目录内容有变动时的就近入口）
      const rb = document.createElement("button");
      rb.textContent = "重新扫描";
      rb.title = "这个文件夹的内容改过？点此立即重扫该目录";
      rb.addEventListener("click", () => rescanFolder(f.path));
      row.appendChild(rb);
    }
    list.appendChild(row);
  });
  maybeShowResume(b);
}

async function rescanFolder(path) {
  try {
    await api("/batch/rescan", {
      method: "POST",
      body: JSON.stringify({ path }),
    });
    openBatchPolling();
  } catch (e) {
    alert("重扫失败：" + e.message);
  }
}

function maybeShowResume(b) {
  // 有历史批次、当前没在跑、还有未完成目录 → 显示「继续上次批次」
  const need = b && b.exists && !b.running
    && (b.folders || []).some((f) => f.status !== "done");
  $("#btn-resume").classList.toggle("hidden", !need);
}

/* ---------------- 工作台：复核 ---------------- */

async function openPlan(key) {
  state.planKey = key;
  state.plan = await api(`/plans/${key}`);
  renderIssues(state.plan);
  renderGrid(state.plan);
  $("#review-folder").textContent = state.plan.folder.split(/[\\/]/).filter(Boolean).pop() || state.plan.folder;
  // 明确的扫描结果反馈（试用者 1 建议）+ 快照生成时间（避免"目录改了页面没变"的困惑）
  $("#review-stats").textContent = `共 ${state.plan.total} 张 · 待复核 ${state.plan.review_count} 张`;
  $("#review-created").textContent = state.plan.created
    ? `（快照生成于 ${state.plan.created.replace("T", " ")}，目录有变动请重新扫描）` : "";
  $("#review-card").classList.remove("hidden");
  $("#review-card").scrollIntoView({ behavior: "smooth", block: "start" });
  const blocking = (state.plan.issues || []).some((it) => it.kind === "stem_collision");
  $("#accept-row").classList.toggle("hidden", !blocking);
  $("#apply-result").classList.add("hidden");
  // 映射表导出（试用者 3 建议）
  const csvBtn = $("#btn-export-csv");
  csvBtn.classList.remove("hidden");
  csvBtn.onclick = () => window.open(`/api/plans/${state.planKey}/export.csv`, "_blank");
}

function issueLine(it) {
  const div = document.createElement("div");
  const blockingKinds = { stem_collision: "block", merge_failed: "warn", vocab_degraded: "warn", guess_failed: "warn", review_heavy: "warn" };
  div.className = "issue-line " + (blockingKinds[it.kind] || "info");
  div.textContent = `[${it.kind}] ${it.detail}`;
  return div;
}

function renderIssues(plan) {
  const box = $("#issues");
  box.innerHTML = "";
  (plan.issues || []).forEach((it) => box.appendChild(issueLine(it)));
}

function renderGrid(plan) {
  const grid = $("#review-grid");
  grid.innerHTML = "";
  plan.rows.forEach((row, idx) => {
    const cell = document.createElement("div");
    cell.className = "cell"
      + (row.needs_review ? " needs-review" : "")
      + (row.skip ? " skipped" : "");

    const img = document.createElement("img");
    img.className = "thumb";
    img.loading = "lazy";
    img.src = `/api/thumb?path=${encodeURIComponent(row.src)}`;
    img.title = "点击放大对照（原名 → 新名）";
    img.addEventListener("click", () => openLightbox(row));

    const body = document.createElement("div");
    body.className = "body";

    const src = document.createElement("div");
    src.className = "src";
    src.textContent = row.src.split(/[\\/]/).pop();

    const ai = document.createElement("div");
    ai.className = "ai-line";
    const badges = [];
    if (row.needs_review) badges.push('<span class="badge amber">待复核</span>');
    if (row.exif_ordinal) badges.push('<span class="badge blue">按时间补序</span>');
    ai.innerHTML = `AI：${escapeHtml(row.part)}${row.info ? "（" + escapeHtml(row.info) + "）" : ""} ${badges.join(" ")}`;

    const input = document.createElement("input");
    input.type = "text";
    input.value = row.final;
    input.spellcheck = false;
    input.addEventListener("input", () => { row.final = input.value; });

    const skipRow = document.createElement("label");
    skipRow.className = "skip-row";
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.checked = !!row.skip;
    cb.addEventListener("change", () => {
      row.skip = cb.checked;
      cell.classList.toggle("skipped", cb.checked);
    });
    skipRow.appendChild(cb);
    skipRow.appendChild(document.createTextNode("跳过这张（不改名）"));

    body.appendChild(src);
    body.appendChild(ai);
    body.appendChild(input);
    body.appendChild(skipRow);
    cell.appendChild(img);
    cell.appendChild(body);
    grid.appendChild(cell);
  });
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

/* ---------------- 放大预览（试用者 2 建议） ---------------- */

function openLightbox(row) {
  const name = row.src.split(/[\\/]/).pop();
  $("#lightbox-img").src = `/api/thumb?path=${encodeURIComponent(row.src)}&side=1600`;
  $("#lightbox-cap").innerHTML =
    `<b>${escapeHtml(name)}</b> → <b>${escapeHtml(row.final)}</b>` +
    (row.needs_review ? ' <span class="badge amber">待复核</span>' : "");
  $("#lightbox").classList.remove("hidden");
}

$("#lightbox").addEventListener("click", () => $("#lightbox").classList.add("hidden"));
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") $("#lightbox").classList.add("hidden");
});

/* ---------------- 工作台：落盘 ---------------- */

$("#btn-apply").addEventListener("click", applyPlan);

document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter"
      && !$("#review-card").classList.contains("hidden")) {
    e.preventDefault();
    applyPlan();
  }
});

async function applyPlan() {
  if (!state.planKey || !state.plan) return;
  if (!confirm("确认按复核结果落盘改名？\n\n安全承诺：本次不会删除或覆盖任何文件（零写入预检 + 逐条不覆盖），改错了随时可在「回滚」页一键整批还原。")) return;
  const btn = $("#btn-apply");
  btn.disabled = true;
  try {
    // 1) 先提交复核编辑（final / skip）
    await api(`/plans/${state.planKey}/review`, {
      method: "POST",
      body: JSON.stringify({
        rows: state.plan.rows.map((r) => ({ final: r.final, skip: !!r.skip })),
      }),
    });
    // 2) 预检并落盘
    const r = await api(`/plans/${state.planKey}/apply`, {
      method: "POST",
      body: JSON.stringify({ accept_review: $("#accept-review").checked }),
    });
    showApplyResult(r);
  } catch (e) {
    toastMsg($("#apply-result"), "落盘失败：" + e.message, "bad");
  } finally {
    btn.disabled = false;
  }
}

function toastMsg(el, text, cls) {
  el.textContent = text;
  el.classList.remove("hidden");
  el.className = el.className.replace(/ (ok|bad)/g, "") + (cls ? " " + cls : "");
}

function showApplyResult(r) {
  const el = $("#apply-result");
  el.classList.remove("hidden", "ok", "bad");
  if (r.aborted) {
    el.classList.add("bad");
    let html = "<b>已阻止落盘（零写入，文件未被改动）</b>";
    if (r.blocked.length) {
      // 语义冲突（不同部件共用同名）：可改开，也可显式放行
      $("#accept-row").classList.remove("hidden");
      html += `<div class="detail"><b>命名冲突：</b>${r.blocked.map((b) => escapeHtml(b.detail)).join("；")}
        <br>处理方式：在上方复核区把相关文件名<b>改成不同的名字</b>（推荐），或勾选下方「接受冲突放行」。</div>`;
    }
    if (r.conflicts.length) {
      const by = (reason) => r.conflicts.filter((c) => c.reason === reason);
      const dup = by("dst_duplicate"), exists = by("dst_exists"), cyc = by("rename_cycle");
      if (dup.length) html += `<div class="detail"><b>批内重名：</b>有 ${dup.length} 条在复核表里改成了彼此相同的文件名——请把它们改成不同的名字，或勾选「跳过」其中一张（这类冲突不能放行）。</div>`;
      if (exists.length) html += `<div class="detail"><b>目标名被占用：</b>${exists.length} 个目标文件名已被磁盘上的其他文件占用（常见于对同一文件夹重复执行）：${exists.map((c) => escapeHtml(c.dst.split(/[\\/]/).pop())).join("、")}。请换一个名字或勾选「跳过」（这类冲突不能放行，放行会覆盖已有文件）。</div>`;
      if (cyc.length) html += `<div class="detail"><b>改名环：</b>存在互换名字的循环（A→B 且 B→A），请调整其中一个名字。</div>`;
    }
    if (r.rolled_back) html += `<div class="detail">本次已自动逆序回滚 ${r.rolled_back} 条</div>`;
    el.innerHTML = html;
  } else {
    el.classList.add("ok");
    el.innerHTML = `<b>落盘完成 ✓</b> 改名 ${r.renamed} 张，保持原名 ${r.noop} 张。原图无任何删除或覆盖。
      <div class="detail">批次号：<b>${r.plan_id}</b>（已写入 journal，可在「回滚」页整批还原）</div>`;
    // 回滚快捷入口（试用者 3 建议：误操作时第一反应是在当前页找撤销）
    const btn = document.createElement("button");
    btn.textContent = "刚刚落错了？一键回滚此批";
    btn.addEventListener("click", () => quickRollback(r.plan_id));
    el.querySelector(".detail").appendChild(document.createElement("br"));
    el.querySelector(".detail").appendChild(btn);
  }
}

async function quickRollback(planId) {
  if (!confirm(`确认整批回滚 ${planId}？文件名将逆序还原为落盘前。`)) return;
  try {
    const r = await api("/rollback", {
      method: "POST",
      body: JSON.stringify({ plan_id: planId }),
    });
    toastMsg($("#apply-result"),
      `已回滚 ✓ 还原 ${r.restored} 条（文件名已还原为落盘前）。`, "ok");
  } catch (e) {
    toastMsg($("#apply-result"), "回滚失败：" + e.message, "bad");
  }
}

/* ---------------- 回滚 ---------------- */

async function loadBatches() {
  const box = $("#batches");
  try {
    const r = await api("/batches");
    box.innerHTML = "";
    if (!r.batches.length) {
      box.innerHTML = '<p class="muted">还没有任何落盘记录。</p>';
      return;
    }
    r.batches.forEach((b) => {
      const div = document.createElement("div");
      div.className = "batch";
      div.innerHTML = `<span class="pid">${escapeHtml(b.plan_id)}</span>
        <span class="meta">${escapeHtml(b.ts || "")} · ${b.count} 条 · ${escapeHtml(b.folder)}</span>`;
      const btn = document.createElement("button");
      btn.textContent = "回滚此批";
      btn.addEventListener("click", () => doRollback(b.plan_id));
      div.appendChild(btn);
      box.appendChild(div);
    });
  } catch (e) {
    box.innerHTML = `<p class="muted">加载失败：${escapeHtml(e.message)}</p>`;
  }
}

async function doRollback(planId) {
  if (!confirm(`确认整批回滚 ${planId}？文件名将逆序还原为落盘前。`)) return;
  const card = $("#rollback-result-card");
  const el = $("#rollback-result");
  card.classList.remove("hidden");
  el.className = "apply-result";
  el.innerHTML = '<p class="muted">回滚中…</p>';
  try {
    const r = await api("/rollback", {
      method: "POST",
      body: JSON.stringify({ plan_id: planId }),
    });
    if (r.mismatched.length || r.missing.length) {
      el.classList.add("bad");
      el.innerHTML = `<b>回滚未完全：</b>还原 ${r.restored} 条，跳过 ${r.already} 条；
        ${r.mismatched.length} 条拒绝、${r.missing.length} 条丢失。
        <div class="detail">${[...r.mismatched, ...r.missing].map(escapeHtml).join("<br>")}</div>`;
    } else {
      el.classList.add("ok");
      el.innerHTML = `<b>回滚完成 ✓</b> 还原 ${r.restored} 条，跳过 ${r.already} 条（本来就没改名或已回滚过）。`;
    }
    loadBatches();
  } catch (e) {
    el.classList.add("bad");
    el.textContent = "回滚失败：" + e.message;
  }
}

/* ---------------- 设置 ---------------- */

async function loadSettings() {
  try {
    const s = await api("/settings");
    $$('input[name="provider"]').forEach((r) => { r.checked = r.value === s.provider; });
    $("#ollama-host").value = s.ollama_host || "";
    $("#model").value = s.model || "";
    $("#num-ctx").value = s.num_ctx || 8192;
    $("#max-side").value = s.max_side || 1024;
    $("#default-product-word").value = s.default_product_word || "";
    $("#cloud-base").value = s.cloud_base_url || "";
    $("#cloud-key").value = s.cloud_api_key || "";
    $("#cloud-model").value = s.cloud_model || "";
    toggleProviderFields(s.provider);
  } catch (e) {
    $("#settings-msg").textContent = "加载设置失败：" + e.message;
  }
}

function toggleProviderFields(p) {
  $("#settings-local").classList.toggle("hidden", p !== "local_ollama");
  $("#settings-cloud").classList.toggle("hidden", p !== "cloud_openai");
}

$$('input[name="provider"]').forEach((r) => {
  r.addEventListener("change", () => toggleProviderFields(r.value));
});

function collectSettings() {
  const provider = $$('input[name="provider"]').find((r) => r.checked)?.value;
  const patch = {
    provider,
    ollama_host: $("#ollama-host").value.trim(),
    model: $("#model").value.trim(),
    num_ctx: parseInt($("#num-ctx").value, 10) || 8192,
    max_side: parseInt($("#max-side").value, 10) || 1024,
    default_product_word: $("#default-product-word").value.trim(),
    cloud_base_url: $("#cloud-base").value.trim(),
    cloud_api_key: $("#cloud-key").value.trim(),
    cloud_model: $("#cloud-model").value.trim(),
  };
  // Key 为空或掩码时不提交，后端会保留旧值
  if (!patch.cloud_api_key || patch.cloud_api_key.startsWith("****")) patch.cloud_api_key = null;
  return patch;
}

async function saveSettings(test) {
  const msg = $("#settings-msg");
  try {
    const r = await api("/settings", {
      method: "POST",
      body: JSON.stringify(collectSettings()),
    });
    msg.textContent = `已保存（当前 Provider：${r.provider}）` + (test ? "，正在测试连接…" : "");
    $("#product-word").value = r.settings.default_product_word || "";
    if (test) {
      const h = await api("/health");
      msg.textContent = h.ping.ok
        ? `✓ 连接成功：${h.ping.detail}`
        : `✗ 连接失败：${h.ping.detail}`;
      refreshHealth();
    }
  } catch (e) {
    msg.textContent = "保存失败：" + e.message;
  }
}

$("#btn-save-settings").addEventListener("click", () => saveSettings(false));
$("#btn-test").addEventListener("click", () => saveSettings(true));

/* ---------------- 启动 ---------------- */

refreshHealth();
setInterval(refreshHealth, 15000);

// Service Worker 注册（PWA 静态壳缓存；/api/* 永不缓存）
if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/sw.js").catch(() => {
      /* 非 HTTPS/localhost 环境不可注册，网页版功能不受影响 */
    });
  });
}

// 安装引导：捕获浏览器安装事件，露出「安装」按钮
let deferredPrompt = null;
window.addEventListener("beforeinstallprompt", (e) => {
  e.preventDefault();
  deferredPrompt = e;
  $("#btn-install").classList.remove("hidden");
});
$("#btn-install").addEventListener("click", async () => {
  if (!deferredPrompt) return;
  deferredPrompt.prompt();
  await deferredPrompt.userChoice;
  deferredPrompt = null;
  $("#btn-install").classList.add("hidden");
});
window.addEventListener("appinstalled", () => {
  $("#btn-install").classList.add("hidden");
});

// 启动时：带出默认商品词与最近路径；恢复未完成的批次面板
(async () => {
  try {
    const s = await api("/settings");
    if (s.default_product_word) $("#product-word").value = s.default_product_word;
  } catch (e) { /* ignore */ }
  loadRecents();
  try {
    const b = await api("/batch");
    if (b.exists) {
      renderBatch(b);
      if (b.running) openBatchPolling();
    }
  } catch (e) { /* ignore */ }
})();
