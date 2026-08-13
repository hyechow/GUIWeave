const state = {
  runs: [], active: null, filter: "all", modelReady: false, platformReady: false,
  platformCheck: 0, runRefresh: 0, eventFilter: "all", followTrace: true,
  events: [], frames: [], frameIndex: 0, frameName: null, frameLayout: "wide", expandedEvents: new Set(),
  chatTurn: null,
};
const $ = (id) => document.getElementById(id);
const CONSOLE_HEADLESS = true;
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
  completed: "已完成", waiting: "待确认", failed: "失败", exhausted: "已耗尽", interrupted: "已中断",
};
const routeLabel = {
  gui: "执行 GUI", respond: "直接回复", clarify: "需要补充", cancel: "中止任务",
};
const timeLabel = (value) => value
  ? new Date(value).toLocaleString("zh-CN", { hour12: false })
  : "—";
const isCancellable = (run) => Boolean(
  run?.active_task_id && ["queued", "running", "cancelling"].includes(run.phase),
);
const activePhases = new Set(["queued", "running", "cancelling"]);
const actionState = (event) => event.event === "runtime_action_started"
  ? "started" : event.status === "executed" && !event.no_effect
    ? "success" : event.no_effect ? "warning" : "failed";
const isIssue = (event) => event.no_effect || /fail|error|warn|interrupt|exhaust/.test(
  `${event.level || ""} ${event.status || ""} ${event.event || ""} ${event.layer || ""}`.toLowerCase(),
);

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
  updateChatSend();
  return result;
}

function updateChatSend() {
  $("chat-send").disabled = !state.modelReady;
}

function updateChatPlatform() {
  const platform = $("chat-platform").value;
  const android = platform === "android";
  $("chat-android-field").classList.toggle("hidden", !android);
  $("chat-environment").className = "";
  $("chat-environment").textContent = `${platform.toUpperCase()} · 仅在需要 GUI 时检查`;
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
    if (platform === "browser") {
      query.set("headless", String(CONSOLE_HEADLESS));
    } else if (platform === "android") {
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
  const [runs, tasks, chat] = await Promise.all([
    request("/api/runs"), request("/api/tasks"), request("/api/chat"),
  ]);
  if (refresh !== state.runRefresh) return;
  renderChat(chat.turns);
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

function renderChat(turns) {
  const thread = $("chat-thread");
  const nearBottom = thread.scrollHeight - thread.scrollTop - thread.clientHeight < 80;
  if (!turns.some((turn) => turn.turn_id === state.chatTurn)) {
    state.chatTurn = turns.at(-1)?.turn_id || null;
  }
  thread.innerHTML = turns.length ? turns.map((turn) => {
    const running = activePhases.has(turn.status);
    const runMeta = turn.run_id
      ? `<div class="chat-run-meta" title="${escapeHtml(turn.run_id)}"><i></i><span>RUN</span><code>${escapeHtml(turn.run_id.split("/").at(-1))}</code></div>`
      : "";
    return `<article class="chat-exchange ${turn.turn_id === state.chatTurn ? "selected" : ""}" data-chat-turn="${escapeHtml(turn.turn_id)}"><div class="chat-message user"><small>YOU</small><p>${escapeHtml(turn.user)}</p></div><div class="chat-message agent ${running ? "working" : escapeHtml(turn.status)}"><span class="chat-avatar">GW</span><div><header><b>GUIWeave</b><span class="chat-route ${escapeHtml(turn.route)}">${routeLabel[turn.route] || escapeHtml(turn.route)}</span><span class="status ${escapeHtml(turn.status)}">${phaseLabel[turn.status] || escapeHtml(turn.status)}</span></header><p>${escapeHtml(turn.assistant)}</p>${runMeta}</div></div></article>`;
  }).join("") : '<div class="chat-welcome"><span>GW</span><div><b>GUIWeave 已就绪</b><p>你可以直接对话；需要界面证据或操作时，我会自动启动 GUI 任务。</p></div></div>';
  document.querySelectorAll("[data-chat-turn]").forEach((item) => {
    item.onclick = () => {
      state.chatTurn = item.dataset.chatTurn;
      document.querySelectorAll("[data-chat-turn]").forEach((candidate) => {
        candidate.classList.toggle("selected", candidate === item);
      });
      renderChatDetail(turns.find((turn) => turn.turn_id === state.chatTurn));
    };
  });
  const selectedTurn = turns.find((turn) => turn.turn_id === state.chatTurn);
  renderChatDetail(selectedTurn);
  if (nearBottom || activePhases.has(selectedTurn?.status)) {
    thread.scrollTop = thread.scrollHeight;
  }
}

function renderChatDetail(turn) {
  const detail = $("chat-detail");
  if (!turn) {
    detail.innerHTML = '<div class="chat-detail-empty"><span>⌁</span><b>TURN DETAIL</b><p>发送消息后，这里会显示路由判断和运行详情。</p></div>';
    return;
  }
  const usesGui = turn.route === "gui" || turn.route === "cancel";
  const turnLabel = turn.route === "gui" ? "GUI 任务目标" : turn.route === "cancel" ? "取消请求" : "用户消息";
  detail.innerHTML = `<header><div><p class="kicker">TURN DETAIL</p><h2>${routeLabel[turn.route] || escapeHtml(turn.route)}</h2></div><span class="status ${escapeHtml(turn.status)}">${phaseLabel[turn.status] || escapeHtml(turn.status)}</span></header><section><label>${turnLabel}</label><p>${escapeHtml(turn.gui_goal || turn.user)}</p></section><dl><div><dt>ROUTE</dt><dd>${routeLabel[turn.route] || escapeHtml(turn.route)}</dd></div><div><dt>ROUTING REASON</dt><dd>${escapeHtml(turn.reason)}</dd></div><div><dt>PLATFORM</dt><dd>${usesGui ? escapeHtml(turn.platform).toUpperCase() : "NOT USED"}</dd></div><div><dt>TASK ID</dt><dd><code>${escapeHtml(turn.task_id || "未触发 GUI")}</code></dd></div><div><dt>RUN ID</dt><dd><code>${escapeHtml(turn.run_id || "未创建")}</code></dd></div></dl><section class="chat-detail-result"><label>回复 / 当前结果</label><p>${escapeHtml(turn.assistant)}</p></section>`;
}

function setConsoleMode(mode) {
  const chat = mode === "chat";
  $("runs-mode").classList.toggle("hidden", chat);
  $("chat-mode").classList.toggle("hidden", !chat);
  document.querySelectorAll("[data-console-mode]").forEach((button) => {
    button.classList.toggle("active", button.dataset.consoleMode === mode);
  });
  if (chat) {
    updateChatPlatform();
    $("chat-input").focus();
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
  $("run-view").classList.toggle("is-live", ["running", "starting"].includes(run?.phase));
  const cancel = $("cancel-run");
  cancel.classList.toggle("hidden", !isCancellable(run));
  cancel.dataset.task = run?.active_task_id || "";
  cancel.disabled = run?.phase === "cancelling";
  cancel.textContent = run?.phase === "cancelling" ? "中止中…" : "■ 中止任务";
  if (runId.startsWith("task_")) {
    $("run-summary").textContent = run.summary;
    $("run-metrics").textContent = "等待运行数据";
    state.events = [];
    state.frames = [];
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
  const frameLayout = ["android", "iphone"].includes(run.platform) ? "portrait" : "wide";
  state.events = events.events;
  state.frameLayout = frameLayout;
  const actionStarts = state.events.filter((event) => event.event === "runtime_action_started").length;
  const actionResults = state.events.filter((event) => event.layer === "action" && event.event !== "runtime_action_started").length;
  const actionCount = actionStarts || actionResults;
  const frameCount = state.events.filter((event) => event.screenshot?.name).length;
  $("run-metrics").textContent = `${actionCount} 次操作 · ${frameCount} 张截图`;
  renderEvents(runId);
  const labels = { report: "HTML REPORT", trace: "TRACE JSON", replay: "REPLAY", stdout: "STDOUT", stderr: "STDERR" };
  const entries = Object.keys(detail.artifacts || {});
  $("artifact-list").innerHTML = entries.length
    ? entries.map((key) => `<a class="artifact" href="/api/runs/${encodeURI(runId)}/artifacts/${key}" target="_blank"><span>${labels[key] || key}</span><b>${key === "report" ? "打开可视化报告" : "查看本地产物"} ↗</b></a>`).join("")
    : '<p class="empty">暂无产物</p>';
}

function renderEvents(runId = state.active) {
  const list = $("event-list");
  const previousScroll = list.scrollTop;
  const previousHeight = list.scrollHeight;
  const visible = state.events.slice().reverse().filter((event) => (
    state.eventFilter === "all"
    || (state.eventFilter === "action" && event.layer === "action")
    || (state.eventFilter === "issue" && isIssue(event))
  ));
  const frameLayout = state.frameLayout;
  state.frames = visible.filter((event) => event.screenshot?.name).map((event) => {
    const name = event.screenshot.name;
    const number = String(event.frame_id || "").split(":").at(-1) || event.index || "—";
    return {
      event, name, layout: frameLayout,
      url: `/api/run-frame?run_id=${encodeURIComponent(runId)}&frame=${encodeURIComponent(name)}`,
      label: `FRAME ${number} · ${event.worker_id || event.layer || "runtime"}`,
    };
  });
  const frameIndexes = new Map(state.frames.map((frame, index) => [frame.event, index]));
  list.innerHTML = visible.length
    ? visible.map((event, displayIndex) => {
      const frameName = event.screenshot?.name;
      const frameIndex = frameName ? frameIndexes.get(event) : -1;
      const frame = frameIndex >= 0 ? state.frames[frameIndex] : null;
      const frameUrl = frame?.url || "";
      const frameNumber = String(event.frame_id || "").split(":").at(-1) || event.index || "—";
      const frameLabel = `FRAME ${frameNumber} · ${event.worker_id || event.layer || "runtime"}`;
      const isAction = event.layer === "action";
      const eventActionState = isAction ? actionState(event) : "";
      const actionBadge = !isAction ? "" : {
        started: "▶ 准备执行", success: "✓ 执行成功", warning: "! 未确认效果", failed: "× 执行失败",
      }[eventActionState];
      const message = String(event.message || event.event || "");
      const eventKey = `${runId}:${event.index ?? event.frame_id ?? displayIndex}`;
      const isLong = message.length > 220;
      const collapsed = isLong && !state.expandedEvents.has(eventKey);
      return `<div class="event ${isAction ? `action-event action-${eventActionState}` : ""}"><time>${escapeHtml(event.elapsed_s ?? "—")}s</time><span class="layer">${escapeHtml(event.layer || event.event || "event")}</span><span class="message">${actionBadge ? `<b class="action-badge">${escapeHtml(actionBadge)}</b>` : ""}<span class="message-copy ${collapsed ? "collapsed" : ""}">${escapeHtml(message)}</span>${isLong ? `<button type="button" class="message-toggle" data-event-key="${escapeHtml(eventKey)}">${collapsed ? "展开" : "收起"}</button>` : ""}</span><span class="worker">${escapeHtml(event.worker_id || "")}</span>${frameUrl ? `<button type="button" class="event-frame frame-${frameLayout}" data-frame-layout="${frameLayout}" data-frame-index="${frameIndex}" data-frame-url="${escapeHtml(frameUrl)}" data-frame-label="${escapeHtml(frameLabel)}"><img src="${escapeHtml(frameUrl)}" loading="lazy" alt="${escapeHtml(frameLabel)}"><span><b>${escapeHtml(frameLabel)}</b><small>点击查看完整截图</small></span></button>` : ""}</div>`;
    }).join("")
    : `<p class="empty">${state.events.length ? "当前筛选下暂无事件" : "暂无结构化事件"}</p>`;
  document.querySelectorAll(".event-frame").forEach((button) => {
    button.onclick = () => showFrame(Number(button.dataset.frameIndex));
  });
  document.querySelectorAll(".message-toggle").forEach((button) => {
    button.onclick = () => {
      const copy = button.previousElementSibling;
      const collapsed = copy.classList.toggle("collapsed");
      if (collapsed) state.expandedEvents.delete(button.dataset.eventKey);
      else state.expandedEvents.add(button.dataset.eventKey);
      button.textContent = collapsed ? "展开" : "收起";
    };
  });
  list.scrollTop = state.followTrace ? 0 : previousScroll + list.scrollHeight - previousHeight;
  if ($("frame-dialog").open) {
    const index = state.frames.findIndex((frame) => frame.name === state.frameName);
    if (index < 0) $("frame-dialog").close();
    else showFrame(index);
  }
}

function showFrame(index) {
  const frame = state.frames[index];
  if (!frame) return;
  state.frameIndex = index;
  state.frameName = frame.name;
  $("frame-image").src = frame.url;
  $("frame-title").textContent = frame.label;
  $("frame-counter").textContent = `${index + 1} / ${state.frames.length}`;
  $("previous-frame").disabled = index === 0;
  $("next-frame").disabled = index === state.frames.length - 1;
  frameDialog.classList.toggle("frame-portrait", frame.layout === "portrait");
  if (!frameDialog.open) frameDialog.showModal();
}

function setTraceFollow(enabled) {
  state.followTrace = enabled;
  const button = $("trace-follow");
  button.classList.toggle("active", enabled);
  button.setAttribute("aria-pressed", String(enabled));
  button.textContent = enabled ? "◎ 跟随" : "○ 已停";
  if (enabled) $("event-list").scrollTop = 0;
}

function showRefreshError(error) {
  if (!state.runs.length) {
    $("task-list").innerHTML = `<p class="empty">${escapeHtml(error.message)}</p>`;
  }
}

async function cancelTask() {
  const button = $("cancel-run");
  const taskId = button.dataset.task;
  if (!taskId || !confirm("中止这个任务？Agent 会在当前安全边界停止；已经执行的 GUI 操作不会自动撤销。")) return;
  button.disabled = true;
  button.textContent = "中止中…";
  try {
    await request(`/api/tasks/${taskId}/cancel`, { method: "POST" });
    await loadRuns();
  } catch (error) {
    button.disabled = false;
    button.textContent = "■ 中止任务";
    alert(`无法中止任务：${error.message}`);
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
document.querySelectorAll("[data-event-filter]").forEach((button) => {
  button.onclick = () => {
    document.querySelectorAll("[data-event-filter]").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    state.eventFilter = button.dataset.eventFilter;
    renderEvents();
  };
});
document.querySelectorAll("[data-console-mode]").forEach((button) => {
  button.onclick = () => setConsoleMode(button.dataset.consoleMode);
});
const dialog = $("task-dialog");
const frameDialog = $("frame-dialog");
$("close-frame").onclick = () => frameDialog.close();
$("previous-frame").onclick = () => showFrame(state.frameIndex - 1);
$("next-frame").onclick = () => showFrame(state.frameIndex + 1);
frameDialog.onkeydown = (event) => {
  if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
    event.preventDefault();
    showFrame(state.frameIndex + (event.key === "ArrowLeft" ? -1 : 1));
  }
};
frameDialog.onclick = (event) => {
  if (event.target === frameDialog) frameDialog.close();
};
$("sidebar-toggle").onclick = () => {
  const collapsed = document.body.classList.toggle("sidebar-collapsed");
  const button = $("sidebar-toggle");
  button.setAttribute("aria-expanded", String(!collapsed));
  button.setAttribute("aria-label", collapsed ? "展开任务列表" : "收起任务列表");
  button.title = collapsed ? "展开任务列表" : "收起任务列表";
};
$("trace-follow").onclick = () => setTraceFollow(!state.followTrace);
$("event-list").onscroll = () => {
  if (state.followTrace && $("event-list").scrollTop > 24) setTraceFollow(false);
};
$("chat-platform").onchange = updateChatPlatform;
$("chat-adb-serial").value = rememberedAndroidAddress();
$("chat-adb-serial").onchange = () => {
  const address = normalizeAndroidAddress($("chat-adb-serial").value);
  $("chat-adb-serial").value = address;
  rememberAndroidAddress(address);
  updateChatPlatform();
};
$("chat-input").onkeydown = (event) => {
  if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    $("chat-form").requestSubmit();
  }
};
$("chat-new-session").onclick = async () => {
  if (!confirm("清空当前对话并开启新的 Session？已有 Runs 不会被删除。")) return;
  const button = $("chat-new-session");
  button.disabled = true;
  try {
    await request("/api/chat/session", { method: "POST" });
    state.chatTurn = null;
    $("chat-input").value = "";
    await loadRuns();
    $("chat-input").focus();
  } catch (error) {
    alert(error.message);
  } finally {
    button.disabled = false;
  }
};
$("chat-form").onsubmit = async (event) => {
  event.preventDefault();
  const message = $("chat-input").value.trim();
  if (!message || !state.modelReady) return;
  const platform = $("chat-platform").value;
  const address = platform === "android" ? normalizeAndroidAddress($("chat-adb-serial").value) : null;
  $("chat-send").disabled = true;
  $("chat-send").textContent = "判断中…";
  try {
    const response = await request("/api/chat/messages", {
      method: "POST", headers: { "content-type": "application/json" },
      body: JSON.stringify({
        message, platform, perception: "enhanced", max_turns: 50,
        adb_serial: address, multi_action: true,
      }),
    });
    state.chatTurn = response.turn.turn_id;
    $("chat-input").value = "";
    if (address) rememberAndroidAddress(address);
    await loadRuns();
  } catch (error) {
    $("chat-thread").insertAdjacentHTML("beforeend", `<div class="chat-local-error">${escapeHtml(error.message)}</div>`);
    $("chat-thread").scrollTop = $("chat-thread").scrollHeight;
  } finally {
    $("chat-send").textContent = "发送 ↗";
    updateChatSend();
    $("chat-input").focus();
  }
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
$("cancel-run").onclick = cancelTask;
$("task-form").onsubmit = async (event) => {
  event.preventDefault();
  const data = Object.fromEntries(new FormData(event.target));
  data.adb_serial = normalizeAndroidAddress(data.adb_serial) || null;
  Object.assign(data, { max_turns: Number(data.max_turns), headless: CONSOLE_HEADLESS, multi_action: true, show_hud: false });
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
