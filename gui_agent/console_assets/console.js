const state = {
  runs: [], active: null, filter: "all", modelReady: false, platformReady: false,
  platformCheck: 0, runRefresh: 0,
};
const $ = (id) => document.getElementById(id);
const ANDROID_DEVICE_STORAGE_KEY = "guiweave.android.device";
const normalizeAndroidAddress = (value) => {
  const address = String(value || "").trim();
  return /^(?:\d{1,3}\.){3}\d{1,3}$/.test(address) ? `${address}:5555` : address;
};
const rememberedAndroidAddress = () => {
  try { return localStorage.getItem(ANDROID_DEVICE_STORAGE_KEY) || ""; }
  catch (_error) { return ""; }
};
const rememberAndroidAddress = (address) => {
  try {
    if (address) localStorage.setItem(ANDROID_DEVICE_STORAGE_KEY, address);
    else localStorage.removeItem(ANDROID_DEVICE_STORAGE_KEY);
  } catch (_error) { /* local storage may be disabled; connection still works */ }
};
const escapeHtml = (value) => String(value ?? "").replace(
  /[&<>"']/g,
  (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char],
);
const phaseLabel = {
  running: "运行中", starting: "启动中", queued: "排队中", cancelling: "取消中",
  completed: "已完成", failed: "失败", exhausted: "已耗尽", interrupted: "已中断",
};
const timeLabel = (value) => value
  ? new Date(value).toLocaleString("zh-CN", { hour12: false })
  : "—";

async function request(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body.detail || `请求失败 (${response.status})`);
  }
  return response.json();
}

async function loadModelEnvironment() {
  const result = await request("/api/environment/model");
  const runtime = $("model-runtime");
  const notice = $("model-notice");
  const start = $("start-task");
  runtime.classList.toggle("model-error", !result.ok);
  $("model-status").textContent = result.ok ? "MODEL READY" : "MODEL NOT CONFIGURED";
  $("model-summary").textContent = result.summary;
  notice.className = `notice ${result.ok ? "model-ready" : "model-error"}`;
  const details = (result.details || []).map(escapeHtml).join("<br>");
  notice.innerHTML = result.ok
    ? `<b>模型连接：</b>${escapeHtml(result.summary)}。<br><code>${escapeHtml(result.config_path)}</code><br>${details}`
    : `<b>无法启动任务：</b>${escapeHtml(result.summary)}。<br><code>${escapeHtml(result.config_path)}</code><br>${details}`;
  start.disabled = !result.ok;
  state.modelReady = result.ok;
  start.disabled = !(state.modelReady && state.platformReady);
  return result;
}

async function loadPlatformEnvironment() {
  const check = ++state.platformCheck;
  const platform = $("task-platform").value;
  const androidField = $("android-device-field");
  androidField.classList.toggle("hidden", platform !== "android");
  const notice = $("platform-notice");
  state.platformReady = false;
  $("start-task").disabled = true;
  notice.className = "notice platform-checking";
  notice.innerHTML = `<b>平台依赖：</b>正在检查 ${escapeHtml(platform)}…`;
  try {
    const query = new URLSearchParams();
    let androidAddress = "";
    if (platform === "android") {
      androidAddress = normalizeAndroidAddress($("adb-serial").value);
      if (androidAddress) {
        $("adb-serial").value = androidAddress;
        query.set("adb_serial", androidAddress);
      }
    }
    const suffix = query.size ? `?${query.toString()}` : "";
    const result = await request(`/api/environment/${encodeURIComponent(platform)}${suffix}`);
    if (check !== state.platformCheck) return;
    state.platformReady = result.ok;
    if (result.ok && platform === "android" && androidAddress) {
      rememberAndroidAddress(androidAddress);
    }
    notice.className = `notice ${result.ok ? "platform-ready" : "platform-error"}`;
    const details = (result.details || []).map(escapeHtml).join("<br>");
    notice.innerHTML = `<b>${escapeHtml(result.summary)}：</b><br>${details}`;
  } catch (error) {
    if (check !== state.platformCheck) return;
    notice.className = "notice platform-error";
    notice.textContent = `平台前置检查失败：${error.message}`;
  }
  $("start-task").disabled = !(state.modelReady && state.platformReady);
}

function renderList() {
  const runs = state.runs.filter((run) => state.filter === "all" || run.phase === state.filter);
  $("run-count").textContent = state.runs.length;
  $("task-list").innerHTML = runs.length
    ? runs.map((run) => `<button class="task-item ${run.run_id === state.active ? "active" : ""}" data-run="${escapeHtml(run.run_id)}">
      <div class="task-top"><span class="tiny">${escapeHtml(run.platform)} · ${escapeHtml(run.run_id.split("/").at(-1))}</span><span class="status ${escapeHtml(run.phase)}">${phaseLabel[run.phase] || escapeHtml(run.phase)}</span></div>
      <p class="task-goal">${escapeHtml(run.goal || run.summary || "正在初始化任务")}</p>
      <span class="tiny">${timeLabel(run.modified_at)} · ${run.event_count} events</span>
    </button>`).join("")
    : '<p class="empty">没有匹配的任务</p>';
  document.querySelectorAll("[data-run]").forEach((button) => {
    button.onclick = () => selectRun(button.dataset.run).catch(showRefreshError);
  });
}

async function loadRuns() {
  const refresh = ++state.runRefresh;
  const [runs, tasks] = await Promise.all([request("/api/runs"), request("/api/tasks")]);
  if (refresh !== state.runRefresh) return;
  const activeByRun = new Map(
    tasks.tasks.filter((task) => task.run_id).map((task) => [task.run_id, task]),
  );
  const ephemeral = tasks.tasks.filter((task) => !task.run_id).map((task) => ({
    run_id: task.task_id, goal: task.goal, platform: task.platform, phase: task.status,
    summary: task.error || "任务正在启动", modified_at: new Date().toISOString(),
    event_count: 0, active_task_id: task.task_id,
  }));
  const durable = runs.runs.map((run) => {
    const task = activeByRun.get(run.run_id);
    return task
      ? { ...run, phase: task.status, active_task_id: task.task_id, summary: task.error || run.summary }
      : run;
  });
  const promoted = tasks.tasks.find((task) => task.task_id === state.active && task.run_id);
  if (promoted) state.active = promoted.run_id;
  state.runs = [...ephemeral, ...durable];
  if (!state.active || !state.runs.some((run) => run.run_id === state.active)) {
    state.active = state.runs[0]?.run_id || null;
  }
  renderList();
  if (state.active) {
    await selectRun(state.active, false);
  } else {
    $("empty-state").classList.remove("hidden");
    $("run-view").classList.add("hidden");
  }
}

async function selectRun(runId, rerender = true) {
  state.active = runId;
  if (rerender) renderList();
  const run = state.runs.find((item) => item.run_id === runId);
  if (!run) return;
  $("empty-state").classList.add("hidden");
  $("run-view").classList.remove("hidden");
  $("run-id").textContent = runId;
  $("run-goal").textContent = run?.goal || "正在初始化任务";
  $("run-platform").textContent = (run?.platform || "—").toUpperCase();
  $("run-time").textContent = timeLabel(run?.modified_at);
  $("run-events").textContent = `${run?.event_count || 0} EVENTS`;
  $("run-status").className = `status ${run?.phase || "starting"}`;
  $("run-status").textContent = phaseLabel[run?.phase] || run?.phase || "启动中";
  const cancel = $("cancel-run");
  cancel.classList.toggle(
    "hidden",
    !(run?.active_task_id && ["queued", "running", "cancelling"].includes(run.phase)),
  );
  cancel.dataset.task = run?.active_task_id || "";
  if (runId.startsWith("task_")) {
    $("run-summary").textContent = run.summary;
    $("event-list").innerHTML = '<p class="empty">完成环境检查并创建运行目录后，事件会出现在这里。</p>';
    $("artifact-list").innerHTML = '<p class="empty">暂无产物</p>';
    return;
  }
  const [detail, events] = await Promise.all([
    request(`/api/runs/${encodeURI(runId)}`),
    request(`/api/runs/${encodeURI(runId)}/events`),
  ]);
  if (state.active !== runId) return;
  $("run-summary").textContent = detail.reply || detail.summary || "—";
  const models = Object.values(detail.models || {}).filter(Boolean);
  $("run-model").textContent = models.length ? [...new Set(models)].join(" · ") : "按本地配置";
  $("event-list").innerHTML = events.events.length
    ? events.events.slice().reverse().map((event) => {
      const frameName = event.screenshot?.name;
      const frameUrl = frameName
        ? `/api/run-frame?run_id=${encodeURIComponent(runId)}&frame=${encodeURIComponent(frameName)}`
        : "";
      const frameNumber = String(event.frame_id || "").split(":").at(-1) || event.index || "—";
      const frameLabel = `FRAME ${frameNumber} · ${event.worker_id || event.layer || "runtime"}`;
      const isAction = event.layer === "action";
      const actionState = !isAction ? "" : event.event === "runtime_action_started"
        ? "started" : event.status === "executed" && !event.no_effect
          ? "success" : event.no_effect
            ? "warning" : "failed";
      const actionBadge = !isAction ? "" : {
        started: "▶ 准备执行", success: "✓ 执行成功", warning: "! 未确认效果", failed: "× 执行失败",
      }[actionState];
      return `<div class="event ${isAction ? `action-event action-${actionState}` : ""}"><time>${escapeHtml(event.elapsed_s ?? "—")}s</time><span class="layer">${escapeHtml(event.layer || event.event || "event")}</span><span class="message">${actionBadge ? `<b class="action-badge">${escapeHtml(actionBadge)}</b>` : ""}${escapeHtml(event.message || event.event || "")}</span><span class="worker">${escapeHtml(event.worker_id || "")}</span>${frameUrl ? `<button type="button" class="event-frame" data-frame-url="${escapeHtml(frameUrl)}" data-frame-label="${escapeHtml(frameLabel)}"><img src="${escapeHtml(frameUrl)}" loading="lazy" alt="${escapeHtml(frameLabel)}"><span><b>${escapeHtml(frameLabel)}</b><small>点击查看完整截图</small></span></button>` : ""}</div>`;
    }).join("")
    : '<p class="empty">暂无结构化事件</p>';
  document.querySelectorAll(".event-frame").forEach((button) => {
    button.onclick = () => {
      $("frame-image").src = button.dataset.frameUrl;
      $("frame-title").textContent = button.dataset.frameLabel;
      $("frame-dialog").showModal();
    };
  });
  const labels = { report: "HTML REPORT", trace: "TRACE JSON", replay: "REPLAY", stdout: "STDOUT", stderr: "STDERR" };
  const entries = Object.keys(detail.artifacts || {});
  $("artifact-list").innerHTML = entries.length
    ? entries.map((key) => `<a class="artifact" href="/api/runs/${encodeURI(runId)}/artifacts/${key}" target="_blank"><span>${labels[key] || key}</span><b>${key === "report" ? "打开可视化报告" : "查看本地产物"} ↗</b></a>`).join("")
    : '<p class="empty">暂无产物</p>';
}

function showRefreshError(error) {
  if (!state.runs.length) {
    $("task-list").innerHTML = `<p class="empty">${escapeHtml(error.message)}</p>`;
  }
}

document.querySelectorAll("[data-filter]").forEach((button) => {
  button.onclick = () => {
    document.querySelectorAll("[data-filter]").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    state.filter = button.dataset.filter;
    renderList();
  };
});
const dialog = $("task-dialog");
const frameDialog = $("frame-dialog");
$("close-frame").onclick = () => frameDialog.close();
frameDialog.onclick = (event) => {
  if (event.target === frameDialog) frameDialog.close();
};
$("new-task-button").onclick = () => {
  dialog.showModal();
  loadPlatformEnvironment();
};
$("task-platform").onchange = () => loadPlatformEnvironment();
$("adb-serial").value = rememberedAndroidAddress();
$("adb-serial").onchange = () => {
  if (!$("adb-serial").value.trim()) rememberAndroidAddress("");
  loadPlatformEnvironment();
};
$("close-dialog").onclick = $("cancel-dialog").onclick = () => dialog.close();
$("refresh-button").onclick = () => loadRuns().catch(showRefreshError);
$("cancel-run").onclick = async () => {
  const task = $("cancel-run").dataset.task;
  if (!task || !confirm("在当前 Agent 轮次结束后安全取消这个任务？")) return;
  await request(`/api/tasks/${task}/cancel`, { method: "POST" });
  await loadRuns();
};
$("task-form").onsubmit = async (event) => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target));
  data.adb_serial = normalizeAndroidAddress(data.adb_serial) || null;
  Object.assign(data, { max_turns: Number(data.max_turns), headless: false, multi_action: true, show_hud: false });
  try {
    const result = await request("/api/tasks", {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(data),
    });
    dialog.close();
    state.active = result.task_id;
    await loadRuns();
  } catch (error) {
    alert(error.message);
  }
};
loadModelEnvironment().catch((error) => {
  $("model-status").textContent = "MODEL CHECK FAILED";
  $("model-summary").textContent = error.message;
  $("model-runtime").classList.add("model-error");
  $("model-notice").className = "notice model-error";
  $("model-notice").textContent = `模型前置检查失败：${error.message}`;
  $("start-task").disabled = true;
});
loadRuns().catch(showRefreshError);
setInterval(() => loadRuns().catch(showRefreshError), 3000);
