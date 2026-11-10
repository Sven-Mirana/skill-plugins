"use strict";

const appState = {
  token: "",
  snapshot: null,
  messages: [],
  firstPaint: true,
};

const routingLayerOrder = [
  "metadata_classification",
  "advisory_recommendation",
  "host_selection",
  "actual_invocation",
];

const participationLabels = {
  local_user: "本地用户",
  queue_only: "仅本地队列",
  task_polled: "声明：任务轮询（不等于正在运行）",
  callable: "声明：宿主可调用（不等于已安装或在线）",
  observer: "只读观察",
};

const taskStatusLabels = {
  todo: "待办",
  doing: "进行中",
  blocked: "受阻",
  done: "已完成",
  cancelled: "已取消",
};

const wakeWindowLabels = {
  active: "已武装",
  draining: "排空中",
  closed: "已关闭",
  hold: "异常待核",
};

const wakeOutcomeLabels = {
  not_initialized: "未初始化",
  not_baselined: "尚未建立基线",
  sent_and_verified: "已回写并验真",
  failed: "失败待重试",
  pending: "待宿主认领",
  claimed: "租约已认领（未证明 Agent 已运行）",
  terminal_hold: "已转人工处理",
  timed_out: "超时已转人工",
};

function element(id) {
  return document.getElementById(id);
}

function make(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function showNotice(message, kind = "info") {
  const notice = element("notice");
  notice.textContent = message;
  notice.dataset.kind = kind;
  notice.hidden = false;
  window.clearTimeout(showNotice.timer);
  showNotice.timer = window.setTimeout(() => {
    notice.hidden = true;
  }, 5000);
}

async function request(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (appState.token) headers.set("X-Panel-Token", appState.token);
  if (options.body) headers.set("Content-Type", "application/json");
  const response = await fetch(path, { ...options, headers, credentials: "same-origin" });
  const payload = await response.json().catch(() => ({ message: "服务器返回了无法解析的内容" }));
  if (!response.ok) {
    const detail = payload.details || {};
    const guards = [];
    if (detail.pending_message_ids?.length) guards.push(`${detail.pending_message_ids.length} 条未处理消息`);
    if (detail.open_task_ids?.length) guards.push(`${detail.open_task_ids.length} 个未关闭任务`);
    throw new Error([payload.message || `请求失败（${response.status}）`, ...guards].join("；"));
  }
  return payload;
}

function activeAgents() {
  return (appState.snapshot?.roster?.seats || []).filter((seat) => seat.kind === "agent" && seat.active);
}

function seatById(id) {
  return (appState.snapshot?.roster?.seats || []).find((seat) => seat.id === id);
}

function wakeupSeatById(id) {
  return (appState.snapshot?.wakeup?.seats || []).find((seat) => seat.seat_id === id);
}

function wakeupSeatLabel(id) {
  const wakeup = appState.snapshot?.wakeup;
  const state = wakeupSeatById(id);
  if (!wakeup?.initialized) return "默认关闭";
  if (!state?.enabled) return "唤醒意图策略关闭";
  if (state.requires_rearm && wakeup.wake_window?.state === "active") return "需重新开窗";
  if (state.pending_intent_id) return wakeOutcomeLabels[state.last_outcome] || "有待处理意图";
  if (state.last_outcome && wakeOutcomeLabels[state.last_outcome]) return wakeOutcomeLabels[state.last_outcome];
  if (!state.effective_enabled) return "已登记·窗口关闭";
  return "意图窗已武装（未证明宿主在线）";
}

function renderHeader() {
  const project = appState.snapshot.project;
  const active = activeAgents().length;
  element("project-summary").textContent = `${project.name} · ${active} 个已启用协作席位 + 1 个固定用户席 · 数据仅存本项目`;
  element("seat-count").textContent = `${active}/${appState.snapshot.roster.max_agent_seats} 个 Agent 席位`;
}

function rosterCard(seat) {
  const visibilityClass = seat.visible ? "" : " is-hidden-seat";
  const card = make("article", `seat-card ${seat.active ? "is-active" : "is-retired"}${visibilityClass}`);
  const head = make("div", "seat-card-head");
  const identity = make("div");
  identity.append(make("h3", "seat-name", seat.display_name));
  identity.append(make("p", "seat-id", seat.id));
  const badgeText = seat.kind === "user"
    ? "固定用户席"
    : !seat.active
      ? "已停用·ID 保留"
      : seat.visible
        ? "席位启用·可见"
        : "席位启用·已隐藏";
  const badge = make("span", "seat-status", badgeText);
  head.append(identity, badge);
  card.append(head);
  card.append(make("p", "seat-role", seat.role_label));

  if (seat.kind === "agent") {
    const runtime = appState.snapshot.agent_runtime[seat.id] || {};
    const meta = make("dl", "seat-meta");
    const items = [
      ["席位模式", participationLabels[seat.participation_mode] || seat.participation_mode],
      ["唤醒意图", wakeupSeatLabel(seat.id)],
      ["未处理消息", String(runtime.pending_count || 0)],
      ["未关闭任务", String(runtime.open_task_count || 0)],
    ];
    items.forEach(([label, value]) => {
      meta.append(make("dt", "", label), make("dd", "", value));
    });
    card.append(meta);
    const actions = make("div", "seat-actions");
    if (seat.active) {
      const visibilityButton = make("button", "secondary-button", seat.visible ? "隐藏卡片" : "显示卡片");
      visibilityButton.type = "button";
      visibilityButton.addEventListener("click", () => setAgentVisibility(seat.id, !seat.visible));
      const retireButton = make("button", "danger-button", "停用席位");
      retireButton.type = "button";
      retireButton.disabled = activeAgents().length <= 1;
      retireButton.title = retireButton.disabled ? "至少保留 1 个已启用 Agent 席位" : "停用前会检查未处理消息和未关闭任务";
      retireButton.addEventListener("click", () => disableAgent(seat.id));
      actions.append(visibilityButton, retireButton);
    } else {
      const restoreButton = make("button", "secondary-button", "恢复原席位");
      restoreButton.type = "button";
      restoreButton.disabled = activeAgents().length >= appState.snapshot.roster.max_agent_seats;
      restoreButton.title = "恢复使用原 ID；游标从恢复时的消息末尾继续";
      restoreButton.addEventListener("click", () => restoreAgent(seat.id));
      actions.append(restoreButton);
    }
    card.append(actions);
  } else {
    card.append(make("p", "user-seat-note", "用户席负责决策与授权，运行时不提供停用入口。"));
  }
  return card;
}

function renderRoster() {
  const list = element("roster-list");
  const managedList = element("managed-seat-list");
  const managedBox = element("managed-seats-box");
  list.replaceChildren();
  managedList.replaceChildren();
  const seats = appState.snapshot.roster.seats || [];
  const userSeats = seats.filter((seat) => seat.kind === "user");
  const visibleAgents = seats.filter((seat) => seat.kind === "agent" && seat.active && seat.visible);
  const primaryAgentIds = new Set(visibleAgents.slice(0, 8).map((seat) => seat.id));
  [...userSeats, ...visibleAgents.filter((seat) => primaryAgentIds.has(seat.id))]
    .forEach((seat) => list.append(rosterCard(seat)));
  const managedSeats = seats.filter(
    (seat) => seat.kind === "agent" && (!seat.active || !seat.visible || !primaryAgentIds.has(seat.id)),
  );
  managedSeats.forEach((seat) => managedList.append(rosterCard(seat)));
  managedBox.hidden = managedSeats.length === 0;
  element("managed-seat-summary").textContent = `其余、隐藏或已停用席位（${managedSeats.length}）`;
  const atLimit = activeAgents().length >= appState.snapshot.roster.max_agent_seats;
  element("agent-form").querySelector("button[type='submit']").disabled = atLimit;
}

function formatTime(value) {
  try {
    return new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }).format(new Date(value));
  } catch (_error) {
    return value;
  }
}

function foldedBody(text) {
  const wrapper = make("div", "message-body");
  const paragraph = make("p", "", text.length > 600 ? `${text.slice(0, 600)}…` : text);
  wrapper.append(paragraph);
  if (text.length > 600) {
    const toggle = make("button", "text-button", "展开全文");
    toggle.type = "button";
    let expanded = false;
    toggle.addEventListener("click", () => {
      expanded = !expanded;
      paragraph.textContent = expanded ? text : `${text.slice(0, 600)}…`;
      toggle.textContent = expanded ? "收起" : "展开全文";
    });
    wrapper.append(toggle);
  }
  return wrapper;
}

function messageItem(message) {
  const item = make("li", "message-item");
  const head = make("div", "message-head");
  const speaker = seatById(message.speaker);
  const audience = message.audience === "all" ? "全体" : (seatById(message.audience)?.display_name || message.audience);
  head.append(
    make("strong", "", speaker?.display_name || message.speaker),
    make("span", "message-route", `→ ${audience}`),
    make("time", "", formatTime(message.timestamp)),
  );
  item.append(head, foldedBody(message.body));
  const id = make("code", "message-id", message.id);
  id.title = "精确消息 ID；Agent 处理后应以此推进游标";
  item.append(id);
  return item;
}

function renderMessages({ keepPosition = false } = {}) {
  const list = element("message-list");
  const wasNearBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 80;
  list.replaceChildren();
  appState.messages.forEach((message) => list.append(messageItem(message)));
  if (appState.messages.length === 0) list.append(make("li", "empty-state", "还没有消息。"));
  element("load-more-button").hidden = !appState.snapshot.has_older_messages;
  if (appState.firstPaint || (!keepPosition && wasNearBottom)) {
    list.scrollTop = list.scrollHeight;
    appState.firstPaint = false;
  }
}

function fillDynamicSelects() {
  const audience = element("message-audience");
  const previousAudience = audience.value;
  audience.replaceChildren();
  const allOption = new Option("全体已启用 Agent 席位", "all");
  audience.add(allOption);
  activeAgents().forEach((seat) => audience.add(new Option(seat.display_name, seat.id)));
  if ([...audience.options].some((option) => option.value === previousAudience)) audience.value = previousAudience;

  const owner = element("task-owner");
  const previousOwner = owner.value;
  owner.replaceChildren(new Option("用户", "user"));
  activeAgents().forEach((seat) => owner.add(new Option(seat.display_name, seat.id)));
  if ([...owner.options].some((option) => option.value === previousOwner)) owner.value = previousOwner;
}

function renderRouting() {
  const container = element("routing-stages");
  container.replaceChildren();
  const layers = new Map((appState.snapshot.routing.layers || []).map((layer) => [layer.id, layer]));
  routingLayerOrder.forEach((layerId, index) => {
    const layer = layers.get(layerId) || { id: layerId, label: layerId, status: "unavailable", record: null };
    const record = layer.record;
    const stage = make("article", `routing-stage stage-${index + 1}`);
    const head = make("div", "routing-stage-head");
    head.append(make("span", "stage-number", String(index + 1)), make("h3", "", layer.label));
    stage.append(head);
    if (layerId === "actual_invocation") {
      stage.append(make("p", "muted", "未调用：当前候选未安装宿主调用适配器；只有验真调用收据可改变此状态"));
    } else if (layerId === "host_selection") {
      stage.append(make("p", "muted", "保持宿主原选择；面板不改写、不调用"));
    } else if (!record) {
      stage.append(make("p", "muted", "暂无隐私最小化记录"));
    } else {
      stage.append(make("p", "routing-request", `请求：${record.request_id}`));
      stage.append(make("p", "routing-skills", record.skill_ids.length ? `Skill：${record.skill_ids.join("、")}` : "Skill：尚未确定"));
      if (record.agent_id) stage.append(make("p", "muted", `Agent：${record.agent_id}`));
      if (record.host_receipt_id) stage.append(make("code", "routing-receipt", record.host_receipt_id));
      if (record.note) stage.append(make("p", "routing-note", record.note));
      stage.append(make("time", "muted", formatTime(record.timestamp)));
    }
    container.append(stage);
  });
}

function renderWakeup() {
  const wakeup = appState.snapshot.wakeup || {};
  const windowState = wakeup.wake_window || { state: "closed" };
  const stateLabel = wakeWindowLabels[windowState.state] || windowState.state || "未知";
  const status = element("wakeup-window-status");
  status.textContent = stateLabel;
  status.dataset.state = windowState.state || "closed";

  const summary = element("wakeup-summary");
  if (wakeup.status === "hold_invalid_state") {
    summary.textContent = `唤醒账本校验失败（${wakeup.error_code || "未知错误"}），已停止投影为正常状态。`;
  } else if (!wakeup.initialized) {
    summary.textContent = "唤醒账本未初始化；所有席位的自动唤醒均默认关闭，接入模式以 roster 为准。";
  } else {
    const enabled = wakeup.totals?.enabled ?? 0;
    const pending = windowState.pending_intents ?? 0;
    const leases = windowState.active_leases ?? 0;
    summary.textContent = `${enabled} 个席位已启用意图策略 · ${pending} 个待处理意图 · ${leases} 个活动租约`;
  }

  const list = element("wakeup-seat-list");
  list.replaceChildren();
  (wakeup.seats || []).forEach((seatState) => {
    const seat = seatById(seatState.seat_id);
    const item = make("li", "wakeup-seat-item");
    const top = make("div", "wakeup-seat-head");
    top.append(
      make("strong", "", seat?.display_name || seatState.seat_id),
      make("span", `wakeup-seat-state ${seatState.enabled ? "is-enabled" : ""}`, wakeupSeatLabel(seatState.seat_id)),
    );
    item.append(top);
    const detail = seatState.enabled
      ? `声明的适配器 ID：${seatState.adapter_id || "待绑定"}（不等于已安装或运行） · 本小时 ${seatState.hourly_used || 0}/${seatState.hourly_limit || 0}`
      : "仅本地队列；建席不会自动取得工具或进程权限";
    item.append(make("p", "muted", detail));
    if (seatState.pending_intent_id) item.append(make("code", "wakeup-intent-id", seatState.pending_intent_id));
    list.append(item);
  });
  if (!(wakeup.seats || []).length) list.append(make("li", "empty-state", "没有 Agent 席位。"));
}

function renderTasks() {
  const tasks = appState.snapshot.tasks || [];
  const list = element("task-list");
  list.replaceChildren();
  tasks.forEach((task) => {
    const owner = seatById(task.owner)?.display_name || task.owner;
    const item = make("li", `task-item task-${task.status}`);
    const top = make("div", "task-top");
    top.append(make("strong", "", task.title), make("span", "task-status", taskStatusLabels[task.status] || task.status));
    item.append(top, make("p", "muted", `${owner} · ${formatTime(task.timestamp)}`));
    list.append(item);
  });
  if (!tasks.length) list.append(make("li", "empty-state", "还没有任务。"));
  const openCount = tasks.filter((task) => ["todo", "doing", "blocked"].includes(task.status)).length;
  element("task-count").textContent = `${openCount} 个未关闭`;
}

function renderAll() {
  renderHeader();
  renderRoster();
  fillDynamicSelects();
  renderMessages();
  renderWakeup();
  renderRouting();
  renderTasks();
}

async function refresh({ quiet = false } = {}) {
  try {
    const snapshot = await request("/api/snapshot");
    appState.snapshot = snapshot;
    appState.messages = snapshot.messages;
    renderAll();
    if (!quiet) showNotice("已刷新项目本地状态");
  } catch (error) {
    showNotice(error.message, "error");
  }
}

async function disableAgent(id) {
  const seat = seatById(id);
  if (!window.confirm(`确认停用“${seat.display_name}”？停用后 ID 将永久保留，不能复用。`)) return;
  try {
    await request(`/api/agents/${encodeURIComponent(id)}/disable`, {
      method: "POST",
      body: JSON.stringify({
        expected_revision: appState.snapshot.roster.revision,
        reason: "用户在多 Agent 面板停用席位",
      }),
    });
    showNotice("席位已停用，ID 墓碑已保留");
    await refresh({ quiet: true });
  } catch (error) {
    showNotice(error.message, "error");
    await refresh({ quiet: true });
  }
}

async function setAgentVisibility(id, visible) {
  try {
    await request(`/api/agents/${encodeURIComponent(id)}/${visible ? "show" : "hide"}`, {
      method: "POST",
      body: JSON.stringify({
        expected_revision: appState.snapshot.roster.revision,
        reason: visible ? "用户在多 Agent 面板显示席位" : "用户在多 Agent 面板隐藏席位",
      }),
    });
    showNotice(visible ? "席位卡片已显示" : "席位卡片已隐藏；消息队列仍继续投递");
    await refresh({ quiet: true });
  } catch (error) {
    showNotice(error.message, "error");
    await refresh({ quiet: true });
  }
}

async function restoreAgent(id) {
  const seat = seatById(id);
  if (!window.confirm(`确认恢复“${seat.display_name}”？将沿用原 ID，并从当前消息末尾开始接收。`)) return;
  try {
    await request(`/api/agents/${encodeURIComponent(id)}/restore`, {
      method: "POST",
      body: JSON.stringify({
        expected_revision: appState.snapshot.roster.revision,
        reason: "用户在多 Agent 面板恢复原墓碑席位",
      }),
    });
    showNotice("原席位已恢复；停用期间的广播不会形成新积压");
    await refresh({ quiet: true });
  } catch (error) {
    showNotice(error.message, "error");
    await refresh({ quiet: true });
  }
}

async function addAgent(event) {
  event.preventDefault();
  const payload = {
    id: element("agent-id").value,
    display_name: element("agent-name").value,
    role_label: element("agent-role").value,
    expected_revision: appState.snapshot.roster.revision,
    reason: "用户在多 Agent 面板增加席位",
  };
  try {
    await request("/api/agents", { method: "POST", body: JSON.stringify(payload) });
    event.currentTarget.reset();
    element("agent-role").value = "协作 Agent";
    showNotice("Agent 席位已增加；新席位从加入时的最后一条消息之后开始读取");
    await refresh({ quiet: true });
  } catch (error) {
    showNotice(error.message, "error");
    await refresh({ quiet: true });
  }
}

async function sendMessage(event) {
  event.preventDefault();
  try {
    await request("/api/messages", {
      method: "POST",
      body: JSON.stringify({
        speaker: "user",
        audience: element("message-audience").value,
        body: element("message-body").value,
      }),
    });
    element("message-body").value = "";
    await refresh({ quiet: true });
    showNotice("消息已写入追加日志；这不代表 Agent 已读取");
  } catch (error) {
    showNotice(error.message, "error");
  }
}

async function addTask(event) {
  event.preventDefault();
  try {
    await request("/api/tasks", {
      method: "POST",
      body: JSON.stringify({
        title: element("task-title").value,
        owner: element("task-owner").value,
        status: "todo",
      }),
    });
    element("task-title").value = "";
    await refresh({ quiet: true });
    showNotice("任务已追加记录");
  } catch (error) {
    showNotice(error.message, "error");
  }
}

async function loadOlderMessages() {
  const first = appState.messages[0];
  if (!first) return;
  try {
    const page = await request(`/api/messages?before_id=${encodeURIComponent(first.id)}&limit=120`);
    const list = element("message-list");
    const oldHeight = list.scrollHeight;
    appState.messages = [...page.messages, ...appState.messages];
    appState.snapshot.has_older_messages = page.has_older;
    renderMessages({ keepPosition: true });
    list.scrollTop += list.scrollHeight - oldHeight;
  } catch (error) {
    showNotice(error.message, "error");
  }
}

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  if (appState.snapshot?.project?.id) {
    localStorage.setItem(`fourchat:${appState.snapshot.project.id}:theme`, theme);
  }
}

function toggleTheme() {
  const current = document.documentElement.dataset.theme || "light";
  setTheme(current === "dark" ? "light" : "dark");
}

async function boot() {
  try {
    const bootstrap = await request("/api/bootstrap");
    appState.token = bootstrap.api_token;
    const storedTheme = localStorage.getItem(`fourchat:${bootstrap.project_id}:theme`);
    const preferred = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    setTheme(storedTheme || preferred);
    await refresh({ quiet: true });
  } catch (error) {
    showNotice(`面板启动失败：${error.message}`, "error");
  }
}

element("theme-button").addEventListener("click", toggleTheme);
element("refresh-button").addEventListener("click", () => refresh());
element("load-more-button").addEventListener("click", loadOlderMessages);
element("agent-form").addEventListener("submit", addAgent);
element("message-form").addEventListener("submit", sendMessage);
element("task-form").addEventListener("submit", addTask);

boot();
