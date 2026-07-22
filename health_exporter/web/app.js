const $ = (selector) => document.querySelector(selector);
const metricOrder = ["activity", "heart_rate", "sleep", "spo2", "blood_pressure", "weight", "stress"];
const phaseLabels = { paused: "已安全暂停", collecting: "正在采集", stale: "状态待修复", idle: "空闲" };
let selectedLog = "";
let statusTimer;

function fmtBytes(value = 0) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let n = Number(value || 0), index = 0;
  while (n >= 1024 && index < units.length - 1) { n /= 1024; index += 1; }
  return `${n.toFixed(index > 1 ? 2 : 0)} ${units[index]}`;
}
function fmtTime(value) {
  if (!value) return "—";
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}
function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
}
function statusBadge(status) {
  const kind = status === "complete" ? "ok" : status === "running" ? "warn" : ["failed", "stale"].includes(status) ? "danger" : "";
  return `<span class="badge ${kind}">${escapeHtml(status || "unknown")}</span>`;
}

function renderSystem(data) {
  const s = data.system || {};
  const processes = s.processes || [];
  const collector = processes.some((item) => (item.command_line || "").includes(" collect"));
  const supervisor = processes.some((item) => (item.command_line || "").includes("continue_after_heart"));
  const chips = [
    [s.adb ? "ok" : "bad", `ADB ${s.adb ? "已连接" : "断开"}`],
    [s.root === false ? "ok" : s.root === true ? "bad" : "warn", `Root ${s.root === false ? "关闭" : s.root === true ? "开启" : "未知"}`],
    [s.app_pid ? "ok" : "warn", `OPPO 健康 PID ${s.app_pid || "—"}`],
    [collector ? "ok" : "warn", `采集器 ${collector ? "运行中" : "未运行"}`],
    [supervisor ? "ok" : "warn", `监督器 ${supervisor ? "运行中" : "未运行"}`],
    [data.stop_marker ? "warn" : "ok", `停止标志 ${data.stop_marker ? "存在" : "无"}`],
    [s.foreground?.includes("com.heytap.health") ? "ok" : "warn", `前台 ${s.foreground || "未知"}`],
  ];
  $("#system-strip").innerHTML = chips.map(([kind, label]) => `<span class="chip ${kind}">${escapeHtml(label)}</span>`).join("");
}

function renderMetrics(metrics) {
  const sorted = [...metrics].sort((a, b) => metricOrder.indexOf(a.key) - metricOrder.indexOf(b.key));
  $("#metrics-body").innerHTML = sorted.map((metric) => {
    const source = metric.source || {}, pages = metric.pages || {}, coverage = metric.coverage || {};
    const coverageHtml = Object.entries(coverage).map(([key, count]) => `<span class="mini">${escapeHtml(key)} ${count}</span>`).join("") || '<span class="muted">尚无</span>';
    return `<tr>
      <td><strong>${escapeHtml(metric.name)}</strong><span class="muted">${escapeHtml(metric.key)}</span></td>
      <td><strong>${escapeHtml(metric.checkpoint || "—")}</strong></td>
      <td>${escapeHtml(source.earliest_source_date || "—")}<br><span class="muted">至 ${escapeHtml(source.latest_source_date || "—")}</span></td>
      <td>${pages.date_count || 0} 天 / ${pages.page_count || 0} 页<br><span class="muted">最早 ${escapeHtml(pages.earliest_page_date || "—")}</span></td>
      <td><div class="coverage">${coverageHtml}</div></td>
      <td>${fmtTime(metric.checkpoint_updated_at)}</td>
    </tr>`;
  }).join("");
}

function renderRuns(runs) {
  $("#runs").innerHTML = runs.slice(0, 16).map((run) => `<article class="run">
    <div class="run-top"><strong>${escapeHtml(run.command)}</strong>${statusBadge(run.status)}</div>
    <p>${fmtTime(run.started_at)} → ${fmtTime(run.finished_at)}</p>
    <p>${escapeHtml(run.id)}</p>
    ${run.error ? `<p class="danger-text">${escapeHtml(run.error)}</p>` : ""}
  </article>`).join("") || '<p class="muted">尚无运行记录。</p>';
}

function renderFailures(failures) {
  $("#failure-count").textContent = failures.length;
  $("#failure-badge").textContent = failures.length;
  $("#failures").innerHTML = failures.map((failure) => `<article class="failure">
    <div class="run-top"><strong>${escapeHtml(failure.stage)}</strong><span class="badge danger">${escapeHtml(failure.canonical_metric || "全局")}</span></div>
    <p>${escapeHtml(failure.local_date || "无日期")} · ${fmtTime(failure.created_at)}</p>
    <p>${escapeHtml(failure.message)}</p>
  </article>`).join("") || '<p class="muted">没有未解决失败。</p>';
}

function renderCalendar(proof) {
  const passed = Boolean(proof?.passed);
  $("#calendar-result").className = `badge ${passed ? "ok" : "danger"}`;
  $("#calendar-result").textContent = passed ? "已验证通过" : "证据不完整";
  $("#calendar-copy").textContent = passed
    ? `已从最新日期打开日历，滚动到 checkpoint 所在月份，并在完整可见网格中一次点击 ${proof.target}；详情页回读日期完全一致。`
    : "尚未形成完整的打开、对齐和日期回读证据。";
  const titles = { before: "① 打开前", calendar_open: "② 日历已打开", target_aligned: "③ 目标月份已对齐", confirmed: "④ 点击后回读" };
  $("#calendar-proof").innerHTML = Object.entries(proof?.stages || {}).map(([key, stage]) => `<figure class="proof-card">
    ${stage.image_url ? `<img src="${stage.image_url}" alt="${titles[key]}截图" loading="lazy" />` : ""}
    <figcaption><strong>${titles[key]}</strong><br>${escapeHtml(stage.date || [stage.year, stage.month].filter(Boolean).join(" ") || "—")}<code>PNG ${escapeHtml((stage.image_sha256 || "").slice(0, 16))}…</code></figcaption>
  </figure>`).join("");
}

function updateLogOptions(logs) {
  const select = $("#log-select");
  const names = logs.map((log) => log.name);
  if (!selectedLog || !names.includes(selectedLog)) selectedLog = names[0] || "";
  select.innerHTML = logs.map((log) => `<option value="${escapeHtml(log.name)}" ${log.name === selectedLog ? "selected" : ""}>${escapeHtml(log.name)} · ${fmtBytes(log.bytes)}</option>`).join("");
}

async function refreshLog() {
  if (!selectedLog) { $("#log-output").textContent = "没有可用日志。"; return; }
  const lines = Math.max(20, Math.min(2000, Number($("#log-lines").value || 500)));
  try {
    const response = await fetch(`/api/log?name=${encodeURIComponent(selectedLog)}&lines=${lines}`, { cache: "no-store" });
    const data = await response.json();
    $("#log-output").textContent = data.text || "日志为空。";
  } catch (error) { $("#log-output").textContent = `日志读取失败：${error}`; }
}

async function refresh() {
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    const data = await response.json();
    if (!response.ok || data.error) throw new Error(data.error || response.statusText);
    renderSystem(data);
    $("#phase").textContent = phaseLabels[data.phase] || data.phase;
    $("#run-detail").textContent = data.latest_run ? `${data.latest_run.command} · ${data.latest_run.status}` : "尚无运行批次";
    $("#page-count").textContent = Number(data.totals.page_capture_count || 0).toLocaleString();
    $("#artifact-count").textContent = Number(data.totals.artifact_count || 0).toLocaleString();
    $("#artifact-size").textContent = fmtBytes(data.totals.artifact_bytes);
    $("#updated-at").textContent = `刷新于 ${fmtTime(data.generated_at)}`;
    renderMetrics(data.metrics || []);
    renderRuns(data.runs || []);
    renderFailures(data.failures || []);
    renderCalendar(data.calendar_proof || {});
    updateLogOptions(data.logs || []);
    await refreshLog();
  } catch (error) {
    $("#updated-at").textContent = `刷新失败：${error}`;
  }
}

$("#refresh").addEventListener("click", refresh);
$("#log-select").addEventListener("change", (event) => { selectedLog = event.target.value; refreshLog(); });
$("#log-lines").addEventListener("change", refreshLog);
refresh();
statusTimer = setInterval(refresh, 5000);
window.addEventListener("beforeunload", () => clearInterval(statusTimer));
