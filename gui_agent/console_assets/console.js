const state = { runs: [], active: null, filter: "all" };
const $ = (id) => document.getElementById(id);
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
    button.onclick = () => selectRun(button.dataset.run);
  });
}

async function loadRuns() {
  const [runs, tasks] = await Promise.all([request("/api/runs"), request("/api/tasks")]);
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
  renderList();
  if (state.active) await selectRun(state.active, false);
}

async function selectRun(runId, rerender = true) {
  state.active = runId;
  if (rerender) renderList();
  const run = state.runs.find((item) => item.run_id === runId);
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
  $("run-summary").textContent = detail.summary || detail.reply || "—";
  const models = Object.values(detail.models || {}).filter(Boolean);
  $("run-model").textContent = models.length ? [...new Set(models)].join(" · ") : "按本地配置";
  $("event-list").innerHTML = events.events.length
    ? events.events.slice().reverse().map((event) => `<div class="event"><time>${escapeHtml(event.elapsed_s ?? "—")}s</time><span class="layer">${escapeHtml(event.layer || event.event || "event")}</span><span class="message">${escapeHtml(event.message || event.event || "")}</span><span class="worker">${escapeHtml(event.worker_id || "")}</span></div>`).join("")
    : '<p class="empty">暂无结构化事件</p>';
  const labels = { report: "HTML REPORT", trace: "TRACE JSON", replay: "REPLAY", stdout: "STDOUT", stderr: "STDERR" };
  const entries = Object.keys(detail.artifacts || {});
  $("artifact-list").innerHTML = entries.length
    ? entries.map((key) => `<a class="artifact" href="/api/runs/${encodeURI(runId)}/artifacts/${key}" target="_blank"><span>${labels[key] || key}</span><b>${key === "report" ? "打开可视化报告" : "查看本地产物"} ↗</b></a>`).join("")
    : '<p class="empty">暂无产物</p>';
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
$("new-task-button").onclick = () => dialog.showModal();
$("close-dialog").onclick = $("cancel-dialog").onclick = () => dialog.close();
$("refresh-button").onclick = () => loadRuns();
$("cancel-run").onclick = async () => {
  const task = $("cancel-run").dataset.task;
  if (!task || !confirm("在当前 Agent 轮次结束后安全取消这个任务？")) return;
  await request(`/api/tasks/${task}/cancel`, { method: "POST" });
  await loadRuns();
};
$("task-form").onsubmit = async (event) => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target));
  Object.assign(data, { max_turns: Number(data.max_turns), headless: false, multi_action: true, show_hud: false });
  try {
    const result = await request("/api/tasks", {
      method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(data),
    });
    dialog.close();
    state.active = result.task_id;
    await loadRuns();
    await selectRun(result.task_id);
  } catch (error) {
    alert(error.message);
  }
};
loadRuns().catch((error) => { $("task-list").innerHTML = `<p class="empty">${escapeHtml(error.message)}</p>`; });
setInterval(loadRuns, 3000);
