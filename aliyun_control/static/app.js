const elements = Object.fromEntries([
  "login-view", "dashboard-view", "login-form", "login-error", "totp", "passkey-login",
  "fleet-meta", "fleet-count", "target-list", "target-account", "target-name", "target-meta",
  "status-heading", "status-dot", "traffic-heading", "traffic-threshold", "traffic-percent",
  "traffic-progress", "traffic-error", "threshold-form", "threshold-input", "threshold-save",
  "refresh-button", "manage-button", "logout-button", "start-button", "stop-button",
  "auto-toggle", "check-button", "operation-state", "last-check", "activity-list", "banner", "stop-dialog", "stop-target-name", "confirm-stop",
  "schedule-form", "schedule-toggle", "schedule-days", "schedule-timezone", "schedule-bulk-start", "schedule-bulk-stop", "schedule-bulk-apply",
  "schedule-next", "schedule-save", "fleet-view", "target-detail", "management-view", "management-close",
  "account-list", "instance-list", "passkey-list", "account-add", "instance-add", "passkey-add", "account-dialog", "account-form",
  "account-dialog-title", "account-id", "account-name", "account-key-id", "account-key-secret",
  "account-totp", "account-error", "account-test", "account-save", "instance-dialog", "instance-form",
  "instance-dialog-title", "instance-key", "instance-name", "instance-account", "instance-region",
  "instance-cloud-id", "instance-threshold", "instance-totp", "instance-error", "instance-save",
  "delete-dialog", "delete-form", "delete-title", "delete-description", "delete-totp", "delete-error",
  "delete-confirm", "activity-source-filter", "activity-outcome-filter", "activity-more", "activity-meta",
].map((id) => [id.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase()), document.querySelector(`#${id}`)]));

let targets = [];
let selectedId = null;
let busy = false;
let configuration = { accounts: [], instances: [] };
let managementOpen = false;
let editingAccountId = null;
let editingInstanceId = null;
let pendingDelete = null;
let activityHistory = [];
let activityTargetId = null;
let activityOffset = 0;
let activityHasMore = false;
let activityLoading = false;

function base64urlToBytes(value) {
  const text = String(value);
  const binary = atob(text.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (text.length % 4)) % 4));
  return Uint8Array.from(binary, (char) => char.charCodeAt(0));
}

function bytesToBase64url(value) {
  const bytes = new Uint8Array(value);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/g, "");
}

function prepareCreationOptions(options) {
  return {
    ...options,
    challenge: base64urlToBytes(options.challenge),
    user: { ...options.user, id: base64urlToBytes(options.user.id) },
    excludeCredentials: (options.excludeCredentials || []).map((item) => ({ ...item, id: base64urlToBytes(item.id) })),
  };
}

function prepareRequestOptions(options) {
  return {
    ...options,
    challenge: base64urlToBytes(options.challenge),
    allowCredentials: (options.allowCredentials || []).map((item) => ({ ...item, id: base64urlToBytes(item.id) })),
  };
}

function serializeCredential(credential) {
  const response = credential.response;
  const result = {
    id: credential.id,
    rawId: bytesToBase64url(credential.rawId),
    type: credential.type,
    response: {},
  };
  for (const key of ["clientDataJSON", "attestationObject", "authenticatorData", "signature", "userHandle"]) {
    if (response[key]) result.response[key] = bytesToBase64url(response[key]);
  }
  return result;
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json().catch(() => ({ ok: false, error: "服务器返回了无效响应" }));
  if (!response.ok || !payload.ok) {
    const error = new Error(payload.error || `请求失败 (${response.status})`);
    error.status = response.status;
    throw error;
  }
  return payload.data ?? payload;
}

function selectedTarget() {
  return targets.find((target) => target.id === selectedId) || null;
}

function showLogin() {
  elements.dashboardView.hidden = true;
  elements.loginView.hidden = false;
  elements.totp.focus();
}

function showDashboard() {
  elements.loginView.hidden = true;
  elements.dashboardView.hidden = false;
}

function showBanner(message, error = false) {
  elements.banner.textContent = message;
  elements.banner.classList.toggle("error", error);
  elements.banner.hidden = !message;
}

function statusText(status) {
  return ({ Running: "运行中", Stopped: "已停止", Starting: "启动中", Stopping: "停止中", Error: "异常" })[status] || status || "未知";
}

function formatTime(value) {
  if (!value) return "从未";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(date);
}

function formatThreshold(value) {
  return Number(value).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}

function scheduleOffsetValue(value) {
  const knownOffsets = {
    UTC: "UTC+00:00", "Asia/Shanghai": "UTC+08:00", "Asia/Hong_Kong": "UTC+08:00",
    "Asia/Singapore": "UTC+08:00", "Asia/Tokyo": "UTC+09:00", "Europe/London": "UTC+00:00",
    "America/Los_Angeles": "UTC-08:00",
  };
  return /^UTC[+-](?:0\d|1[0-4]):[0-5]\d$/.test(value || "") ? value : (knownOffsets[value] || "UTC+08:00");
}

function formatScheduleTime(value, offset) {
  const match = /^UTC([+-])(\d{2}):(\d{2})$/.exec(offset || "");
  const date = new Date(value);
  if (!match || Number.isNaN(date.getTime())) return value;
  const minutes = (Number(match[2]) * 60 + Number(match[3])) * (match[1] === "+" ? 1 : -1);
  const localDate = new Date(date.getTime() + minutes * 60_000);
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "UTC", month: "2-digit", day: "2-digit", weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(localDate);
}

const activityActions = {
  start: "启动实例", stop: "停止实例", auto_enabled: "启用自动策略", auto_disabled: "暂停自动策略",
  schedule_enabled: "启用定时计划", schedule_disabled: "暂停定时计划",
  schedule_start: "定时启动", schedule_stop: "定时停止",
  traffic_start: "流量策略启动", traffic_stop: "流量策略停止",
  traffic_check: "流量检查", schedule_check: "定时检查", automation_check: "自动化检查",
};

const activitySources = {
  traffic: "流量守护", schedule: "开关机计划", manual: "手动操作", config: "配置变更",
};

const activityResults = {
  "Start request accepted": "启动请求已提交",
  "Graceful stop request accepted": "正常关机请求已提交",
  "Instance is already running": "实例已在运行",
  "Instance is already stopped": "实例已停止",
  "Automatic traffic policy enabled": "自动策略已启用",
  "Automatic traffic policy paused": "自动策略已暂停",
  "Power schedule enabled": "定时计划已启用",
  "Power schedule paused": "定时计划已暂停",
};

function activitySource(action) {
  const value = String(action || "");
  if (value.startsWith("traffic_")) return "traffic";
  if (value.startsWith("schedule_")) return "schedule";
  if (value === "start" || value === "stop") return "manual";
  return "config";
}

function activityActionText(action) {
  return activityActions[action] || action || "暂无操作";
}

function activityResultText(result) {
  return activityResults[result] || result || "已完成";
}

function activityOutcomeText(outcome) {
  return ({ success: "成功", skipped: "未执行", failed: "失败" })[outcome] || "未知";
}

function renderActivity(history = activityHistory) {
  activityHistory = Array.isArray(history) ? history : [];
  elements.activityList.replaceChildren(...activityHistory.map((entry) => {
    const row = document.createElement("li");
    const outcomeValue = entry.outcome || "unknown";
    const sourceValue = activitySource(entry.action);
    const sourceText = activitySources[sourceValue] || "系统";
    const actionText = activityActionText(entry.action);
    const resultText = activityResultText(entry.result);
    row.className = `activity-row ${outcomeValue}`;
    row.setAttribute("aria-label", `${sourceText}，${actionText}，${resultText}，${activityOutcomeText(outcomeValue)}`);
    const marker = document.createElement("span");
    marker.className = "activity-marker";
    marker.setAttribute("aria-hidden", "true");
    const copy = document.createElement("div");
    copy.className = "activity-copy";
    const heading = document.createElement("div");
    heading.className = "activity-row-heading";
    const source = document.createElement("span");
    source.className = `activity-source ${sourceValue}`;
    source.textContent = sourceText;
    const title = document.createElement("strong");
    title.textContent = actionText;
    heading.append(source, title);
    const result = document.createElement("span");
    result.className = "activity-result";
    result.textContent = resultText;
    const time = document.createElement("time");
    time.textContent = formatTime(entry.at);
    if (entry.at) {
      time.dateTime = entry.at;
      time.title = entry.at;
    }
    const meta = document.createElement("div");
    meta.className = "activity-meta-line";
    meta.append(time);
    copy.append(heading, result, meta);
    const outcome = document.createElement("span");
    outcome.className = `activity-outcome ${outcomeValue}`;
    outcome.textContent = activityOutcomeText(outcomeValue);
    row.append(marker, copy, outcome);
    return row;
  }));
  if (!activityHistory.length) {
    const empty = document.createElement("li");
    empty.className = "activity-empty";
    empty.textContent = "暂无记录";
    elements.activityList.append(empty);
  }
  if (activityHistory.length) {
    const counts = activityHistory.reduce((summary, entry) => {
      const key = ["success", "skipped", "failed"].includes(entry.outcome) ? entry.outcome : "unknown";
      summary[key] = (summary[key] || 0) + 1;
      return summary;
    }, {});
    const parts = [`已显示 ${activityHistory.length} 条`];
    if (counts.success) parts.push(`成功 ${counts.success}`);
    if (counts.skipped) parts.push(`未执行 ${counts.skipped}`);
    if (counts.failed) parts.push(`失败 ${counts.failed}`);
    elements.activityMeta.textContent = parts.join(" · ");
  } else {
    elements.activityMeta.textContent = "";
  }
  elements.activityMore.hidden = !activityHasMore;
  elements.activityMore.disabled = activityLoading;
  elements.activityMore.textContent = activityLoading ? "加载中" : "加载更多";
}

const weekdayLabels = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"];

function renderScheduleDays(days) {
  const rows = Array.isArray(days) ? days : [];
  elements.scheduleDays.replaceChildren(...weekdayLabels.map((label, weekday) => {
    const row = rows.find((item) => item.weekday === weekday) || { weekday, enabled: false, start: "08:00", stop: "23:00" };
    const item = document.createElement("div");
    item.className = "schedule-day-row";
    item.setAttribute("role", "row");
    item.dataset.weekday = String(weekday);
    item.innerHTML = `<label class="schedule-select-cell" role="cell" aria-label="选择${label}用于批量修改"><input type="checkbox" data-schedule-day-selected></label><strong class="schedule-day-name" role="cell">${label}</strong><label class="switch compact" role="cell" aria-label="启用${label}计划"><input type="checkbox" data-schedule-day-enabled><span class="switch-track"></span></label><label class="schedule-time-field" role="cell"><span>开机</span><input type="time" data-schedule-day-start aria-label="${label}开机时间" required></label><label class="schedule-time-field" role="cell"><span>关机</span><input type="time" data-schedule-day-stop aria-label="${label}关机时间" required></label>`;
    item.querySelector("[data-schedule-day-enabled]").checked = Boolean(row.enabled);
    item.querySelector("[data-schedule-day-start]").value = row.start || "08:00";
    item.querySelector("[data-schedule-day-stop]").value = row.stop || "23:00";
    return item;
  }));
}

function selectedScheduleRows() {
  return [...elements.scheduleDays.querySelectorAll(".schedule-day-row")]
    .filter((row) => row.querySelector("[data-schedule-day-selected]").checked);
}

function setBulkSelection(kind) {
  for (const row of elements.scheduleDays.querySelectorAll(".schedule-day-row")) {
    const weekday = Number(row.dataset.weekday);
    row.querySelector("[data-schedule-day-selected]").checked = (
      kind === "all" || (kind === "weekdays" && weekday < 5) || (kind === "weekend" && weekday >= 5)
    );
  }
}

function applyBulkScheduleTimes() {
  const selected = selectedScheduleRows();
  const start = elements.scheduleBulkStart.value;
  const stop = elements.scheduleBulkStop.value;
  if (!start || !stop || start === stop) {
    showBanner("批量开机和关机时间必须不同", true);
    return;
  }
  if (!selected.length) {
    showBanner("请先选择要批量修改的日期", true);
    return;
  }
  for (const row of selected) {
    row.querySelector("[data-schedule-day-start]").value = start;
    row.querySelector("[data-schedule-day-stop]").value = stop;
  }
  showBanner(`已更新 ${selected.length} 个日期，点击保存计划后生效`);
}

function scheduleDaysPayload() {
  return [...elements.scheduleDays.querySelectorAll(".schedule-day-row")].map((row) => ({
    weekday: Number(row.dataset.weekday),
    enabled: row.querySelector("[data-schedule-day-enabled]").checked,
    start: row.querySelector("[data-schedule-day-start]").value,
    stop: row.querySelector("[data-schedule-day-stop]").value,
  }));
}

function setBusy(value, label = "就绪") {
  busy = value;
  elements.operationState.textContent = label;
  for (const control of [
    elements.refreshButton, elements.autoToggle, elements.checkButton, elements.scheduleToggle,
    elements.scheduleTimezone, elements.scheduleSave,
    elements.scheduleBulkStart, elements.scheduleBulkStop, elements.scheduleBulkApply,
    elements.thresholdInput, elements.thresholdSave,
    ...document.querySelectorAll("[data-schedule-select]"),
    ...elements.scheduleDays.querySelectorAll("input"),
  ]) control.disabled = value;
  updateActionAvailability();
}

function updateActionAvailability() {
  const target = selectedTarget();
  const status = target?.status;
  const unavailable = busy || !target || status === "Error";
  elements.startButton.disabled = unavailable || status === "Running" || status === "Starting";
  elements.stopButton.disabled = unavailable || status === "Stopped" || status === "Stopping";
}

function renderTargetList() {
  elements.targetList.replaceChildren(...targets.map((target) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `target-row${target.id === selectedId ? " selected" : ""}`;
    button.dataset.targetId = target.id;
    button.setAttribute("aria-pressed", String(target.id === selectedId));
    const dot = document.createElement("span");
    dot.className = `status-dot mini ${String(target.status || "unknown").toLowerCase()}`;
    dot.setAttribute("aria-hidden", "true");
    const copy = document.createElement("span");
    copy.className = "target-copy";
    const name = document.createElement("strong");
    name.textContent = target.name;
    const meta = document.createElement("small");
    meta.textContent = `${target.account} · ${target.region}`;
    copy.append(name, meta);
    const state = document.createElement("span");
    state.className = "target-state";
    state.textContent = statusText(target.status);
    button.append(dot, copy, state);
    button.addEventListener("click", () => {
      selectedId = target.id;
      render();
      void loadActivity(true);
    });
    return button;
  }));
}

function renderDetail() {
  const target = selectedTarget();
  elements.targetDetail.hidden = !target;
  if (!target) return;
  elements.targetAccount.textContent = target.account;
  elements.targetName.textContent = target.name;
  elements.targetMeta.textContent = `${target.instance} · ${target.region}`;
  elements.statusHeading.textContent = statusText(target.status);
  elements.statusDot.className = `status-dot ${String(target.status || "unknown").toLowerCase()}`;
  const hasTraffic = Number.isFinite(Number(target.traffic_gb));
  elements.trafficHeading.textContent = hasTraffic ? `${Number(target.traffic_gb).toFixed(2)} GB` : "不可用";
  elements.trafficError.textContent = target.traffic_error || "";
  elements.trafficError.hidden = !target.traffic_error;
  elements.trafficThreshold.textContent = `阈值 ${formatThreshold(target.threshold_gb)} GB`;
  if (document.activeElement !== elements.thresholdInput) {
    elements.thresholdInput.value = String(target.threshold_gb);
  }
  const percent = hasTraffic ? Math.max(0, Math.min(Number(target.usage_percent) || 0, 100)) : 0;
  elements.trafficPercent.textContent = hasTraffic ? `${percent.toFixed(1)}%` : "--";
  elements.trafficProgress.style.width = `${percent}%`;
  elements.trafficProgress.className = `progress-value${percent >= 100 ? " danger" : percent >= 80 ? " warning" : ""}`;
  elements.autoToggle.checked = Boolean(target.auto_enabled);
  elements.autoToggle.disabled = busy || target.status === "Error";
  elements.lastCheck.textContent = formatTime(target.last_check);
  if (activityTargetId !== target.id) {
    activityTargetId = target.id;
    activityOffset = 0;
    activityHasMore = false;
    activityHistory = [];
  }
  renderActivity(activityHistory.length ? activityHistory : target.activity_history);
  elements.stopTargetName.textContent = target.name;
  elements.scheduleToggle.checked = Boolean(target.schedule_enabled);
  elements.scheduleTimezone.value = scheduleOffsetValue(target.schedule_timezone);
  renderScheduleDays(target.schedule_days);
  if (target.schedule_next_at) {
    const action = target.schedule_next_action === "start" ? "启动" : "停止";
    const offset = scheduleOffsetValue(target.schedule_timezone);
    elements.scheduleNext.textContent = `${formatScheduleTime(target.schedule_next_at, offset)} · ${action} · ${offset}`;
  } else {
    elements.scheduleNext.textContent = "计划未启用";
  }
  updateActionAvailability();
}

function render() {
  elements.fleetMeta.textContent = `${targets.length} 台实例 · ${targets.filter((item) => item.status === "Running").length} 台运行中`;
  elements.fleetCount.textContent = String(targets.length);
  renderTargetList();
  renderDetail();
}

function makeButton(label, className, handler) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = `button ${className}`;
  button.textContent = label;
  button.addEventListener("click", handler);
  return button;
}

function managementRow(title, detail, actions) {
  const row = document.createElement("article");
  row.className = "management-row";
  const copy = document.createElement("div");
  copy.className = "management-row-copy";
  const heading = document.createElement("strong");
  heading.textContent = title;
  const meta = document.createElement("span");
  meta.textContent = detail;
  copy.append(heading, meta);
  const actionGroup = document.createElement("div");
  actionGroup.className = "management-row-actions";
  actionGroup.append(...actions);
  row.append(copy, actionGroup);
  return row;
}

function emptyState(message) {
  const state = document.createElement("div");
  state.className = "empty-state";
  state.textContent = message;
  return state;
}

function renderManagement() {
  const accounts = configuration.accounts || [];
  const instances = configuration.instances || [];
  elements.accountList.replaceChildren(...(accounts.length ? accounts.map((account) => managementRow(
    account.name,
    `${account.id} · ${account.access_key_id_masked} · ${account.instance_count} 台实例`,
    [
      makeButton("编辑", "quiet", () => openAccountDialog(account.id)),
      makeButton("删除", "quiet", () => openDeleteDialog("accounts", account.id, account.name)),
    ],
  )) : [emptyState("暂无账号") ]));
  elements.instanceList.replaceChildren(...(instances.length ? instances.map((instance) => {
    const account = accounts.find((item) => item.id === instance.account);
    return managementRow(
      instance.name,
      `${instance.instance_id} · ${instance.region} · ${account?.name || instance.account}`,
      [
        makeButton("编辑", "quiet", () => openInstanceDialog(instance.id)),
        makeButton("删除", "quiet", () => openDeleteDialog("instances", instance.id, instance.name)),
      ],
    );
  }) : [emptyState("暂无实例") ]));
  elements.instanceAdd.disabled = accounts.length === 0;
}

function renderPasskeys(passkeys = []) {
  elements.passkeyList.replaceChildren(...(passkeys.length ? passkeys.map((item) => managementRow(
    item.name || "Passkey",
    `创建于 ${formatTime(item.created_at)} · 最近使用 ${formatTime(item.last_used_at)}`,
    [makeButton("撤销", "quiet", () => revokePasskey(item))],
  )) : [emptyState("尚未注册 Passkey") ]));
}

async function refreshPasskeys() {
  const data = await api("/api/passkeys");
  renderPasskeys(data);
}

async function registerPasskey() {
  const name = window.prompt("给这个 Passkey 起个名字，例如：MacBook Touch ID");
  if (!name) return;
  const totp = window.prompt("请输入当前六位动态验证码");
  if (!totp) return;
  try {
    const options = await api("/api/passkeys/register/options", { method: "POST", body: JSON.stringify({ name, totp }) });
    const credential = await navigator.credentials.create({ publicKey: prepareCreationOptions(options) });
    if (!credential) throw new Error("Passkey 注册被取消");
    await api("/api/passkeys/register/verify", { method: "POST", body: JSON.stringify({ name, credential: serializeCredential(credential) }) });
    await refreshPasskeys();
    showBanner("Passkey 已添加");
  } catch (error) {
    showBanner(error.message, true);
  }
}

async function revokePasskey(item) {
  const totp = window.prompt(`请输入当前动态验证码以撤销“${item.name || "Passkey"}”`);
  if (!totp) return;
  try {
    await api(`/api/passkeys/${encodeURIComponent(item.credential_id)}`, { method: "DELETE", body: JSON.stringify({ totp }) });
    await refreshPasskeys();
    showBanner("Passkey 已撤销");
  } catch (error) {
    showBanner(error.message, true);
  }
}

async function loginWithPasskey() {
  if (!window.PublicKeyCredential || !navigator.credentials) {
    elements.loginError.textContent = "当前浏览器不支持 Passkey，请使用动态验证码登录";
    return;
  }
  const button = elements.passkeyLogin;
  button.disabled = true;
  elements.loginError.textContent = "";
  try {
    const options = await api("/api/passkeys/login/options", { method: "POST", body: "{}" });
    const credential = await navigator.credentials.get({ publicKey: prepareRequestOptions(options) });
    if (!credential) throw new Error("Passkey 登录被取消");
    await api("/api/passkeys/login/verify", { method: "POST", body: JSON.stringify({ credential: serializeCredential(credential) }) });
    showDashboard();
    await refresh();
  } catch (error) {
    elements.loginError.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

async function refreshConfiguration() {
  configuration = await api("/api/configuration");
  renderManagement();
}

async function setManagementView(open) {
  managementOpen = open;
  elements.fleetView.hidden = open;
  elements.managementView.hidden = !open;
  elements.manageButton.textContent = open ? "实例" : "管理";
  if (open) {
    try {
      await refreshConfiguration();
      await refreshPasskeys();
      showBanner("");
    } catch (error) {
      showBanner(error.message, true);
    }
  }
}

function resetAccountForm() {
  elements.accountForm.reset();
  elements.accountError.textContent = "";
  elements.accountError.classList.remove("success");
  elements.accountId.disabled = false;
  elements.accountKeyId.required = true;
  elements.accountKeySecret.required = true;
  editingAccountId = null;
}

function openAccountDialog(accountId = null) {
  resetAccountForm();
  if (accountId) {
    const account = configuration.accounts.find((item) => item.id === accountId);
    if (!account) return;
    editingAccountId = accountId;
    elements.accountDialogTitle.textContent = "编辑账号";
    elements.accountId.value = account.id;
    elements.accountId.disabled = true;
    elements.accountName.value = account.name;
    elements.accountKeyId.required = false;
    elements.accountKeySecret.required = false;
  } else {
    elements.accountDialogTitle.textContent = "添加账号";
  }
  elements.accountDialog.showModal();
  elements.accountName.focus();
}

function populateAccountSelect(selected = "") {
  const options = (configuration.accounts || []).map((account) => {
    const option = document.createElement("option");
    option.value = account.id;
    option.textContent = account.name;
    option.selected = account.id === selected;
    return option;
  });
  elements.instanceAccount.replaceChildren(...options);
}

function resetInstanceForm() {
  elements.instanceForm.reset();
  elements.instanceError.textContent = "";
  elements.instanceKey.disabled = false;
  elements.instanceThreshold.value = "180";
  editingInstanceId = null;
  populateAccountSelect();
}

function openInstanceDialog(instanceId = null) {
  resetInstanceForm();
  if (instanceId) {
    const instance = configuration.instances.find((item) => item.id === instanceId);
    if (!instance) return;
    editingInstanceId = instanceId;
    elements.instanceDialogTitle.textContent = "编辑实例";
    elements.instanceKey.value = instance.id;
    elements.instanceKey.disabled = true;
    elements.instanceName.value = instance.name;
    populateAccountSelect(instance.account);
    elements.instanceRegion.value = instance.region;
    elements.instanceCloudId.value = instance.instance_id;
    elements.instanceThreshold.value = String(instance.traffic_threshold_gb);
  } else {
    elements.instanceDialogTitle.textContent = "添加实例";
  }
  elements.instanceDialog.showModal();
  elements.instanceName.focus();
}

function openDeleteDialog(resource, id, name) {
  pendingDelete = { resource, id };
  elements.deleteForm.reset();
  elements.deleteError.textContent = "";
  elements.deleteTitle.textContent = `删除${resource === "accounts" ? "账号" : "实例"}？`;
  elements.deleteDescription.textContent = name;
  elements.deleteDialog.showModal();
  elements.deleteTotp.focus();
}

async function refresh(preserveBanner = false) {
  setBusy(true, "读取中");
  try {
    const data = await api("/api/overview");
    targets = data.targets || [];
    if (!targets.some((target) => target.id === selectedId)) selectedId = targets[0]?.id || null;
    render();
    await loadActivity(true);
    if (!preserveBanner) showBanner("");
  } catch (error) {
    if (error.status === 401) {
      await api("/api/session").catch(() => null);
      showLogin();
      return;
    }
    showBanner(error.message, true);
  } finally {
    setBusy(false);
  }
}

async function loadActivity(reset = false) {
  const target = selectedTarget();
  if (!target || activityLoading) return;
  if (reset) {
    activityTargetId = target.id;
    activityOffset = 0;
    activityHasMore = false;
    activityHistory = [];
  }
  const targetId = target.id;
  activityLoading = true;
  renderActivity();
  try {
    const query = new URLSearchParams({
      source: elements.activitySourceFilter.value,
      outcome: elements.activityOutcomeFilter.value,
      offset: String(activityOffset),
      limit: "50",
    });
    const data = await api(`/api/targets/${encodeURIComponent(targetId)}/activity?${query}`);
    if (selectedId !== targetId || activityTargetId !== targetId) return;
    activityHistory = reset ? data.items : [...activityHistory, ...data.items];
    activityOffset += data.items.length;
    activityHasMore = Boolean(data.has_more);
  } catch (error) {
    if (selectedId === targetId) showBanner(error.message, true);
  } finally {
    activityLoading = false;
    if (selectedId === targetId) renderActivity();
  }
}

async function runTargetAction(action, body, pendingLabel, successLabel) {
  const target = selectedTarget();
  if (busy || !target) return;
  setBusy(true, pendingLabel);
  showBanner("");
  try {
    await api(`/api/targets/${encodeURIComponent(target.id)}/${action}`, { method: "POST", body: JSON.stringify(body || {}) });
    showBanner(`${target.name}：${successLabel}`);
    await refresh(true);
  } catch (error) {
    showBanner(error.message, true);
    await refresh(true);
  } finally {
    setBusy(false);
  }
}

elements.loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  elements.loginError.textContent = "";
  const submit = elements.loginForm.querySelector("button[type='submit']");
  submit.disabled = true;
  try {
    await api("/api/login", { method: "POST", body: JSON.stringify({ totp: elements.totp.value }) });
    elements.totp.value = "";
    showDashboard();
    await refresh();
  } catch (error) {
    elements.loginError.textContent = error.message;
  } finally {
    submit.disabled = false;
  }
});

elements.refreshButton.addEventListener("click", () => managementOpen ? refreshConfiguration() : refresh());
elements.manageButton.addEventListener("click", () => setManagementView(!managementOpen));
elements.managementClose.addEventListener("click", () => setManagementView(false));
elements.accountAdd.addEventListener("click", () => openAccountDialog());
elements.instanceAdd.addEventListener("click", () => openInstanceDialog());
elements.passkeyAdd.addEventListener("click", registerPasskey);
elements.passkeyLogin.addEventListener("click", loginWithPasskey);
for (const button of document.querySelectorAll("[data-close-dialog]")) {
  button.addEventListener("click", () => document.querySelector(`#${button.dataset.closeDialog}`).close());
}
elements.startButton.addEventListener("click", () => runTargetAction("start", {}, "启动中", "启动请求已提交"));
elements.stopButton.addEventListener("click", () => elements.stopDialog.showModal());
elements.confirmStop.addEventListener("click", (event) => {
  event.preventDefault();
  elements.stopDialog.close();
  runTargetAction("stop", { confirm: "STOP" }, "停止中", "正常关机请求已提交");
});
elements.autoToggle.addEventListener("change", () => {
  const enabled = elements.autoToggle.checked;
  runTargetAction("auto", { enabled }, "保存中", enabled ? "自动策略已启用" : "自动策略已暂停");
});
elements.checkButton.addEventListener("click", () => runTargetAction("check", {}, "检查中", "策略检查已完成"));
elements.thresholdForm.addEventListener("submit", (event) => {
  event.preventDefault();
  if (!elements.thresholdInput.reportValidity()) return;
  runTargetAction(
    "threshold",
    { threshold_gb: Number(elements.thresholdInput.value) },
    "保存中",
    `流量阈值已调整为 ${Number(elements.thresholdInput.value)} GB`,
  );
});
elements.scheduleForm.addEventListener("submit", (event) => {
  event.preventDefault();
  runTargetAction(
    "schedule",
    {
      enabled: elements.scheduleToggle.checked,
      days: scheduleDaysPayload(),
      timezone: elements.scheduleTimezone.value.trim(),
    },
    "保存中",
    elements.scheduleToggle.checked ? "定时计划已启用" : "定时计划已保存并暂停",
  );
});

for (const button of document.querySelectorAll("[data-schedule-select]")) {
  button.addEventListener("click", () => setBulkSelection(button.dataset.scheduleSelect));
}
elements.scheduleBulkApply.addEventListener("click", applyBulkScheduleTimes);

elements.activitySourceFilter.addEventListener("change", () => void loadActivity(true));
elements.activityOutcomeFilter.addEventListener("change", () => void loadActivity(true));
elements.activityMore.addEventListener("click", () => void loadActivity());

elements.accountTest.addEventListener("click", async () => {
  elements.accountError.textContent = "";
  elements.accountError.classList.remove("success");
  if (!elements.accountTotp.reportValidity()) return;
  if (!editingAccountId && (!elements.accountKeyId.reportValidity() || !elements.accountKeySecret.reportValidity())) return;
  elements.accountTest.disabled = true;
  try {
    await api("/api/credentials/test", {
      method: "POST",
      body: JSON.stringify({
        account_id: editingAccountId,
        access_key_id: elements.accountKeyId.value.trim(),
        access_key_secret: elements.accountKeySecret.value,
        totp: elements.accountTotp.value.trim(),
      }),
    });
    elements.accountError.textContent = "凭据验证成功";
    elements.accountError.classList.add("success");
  } catch (error) {
    elements.accountError.classList.remove("success");
    elements.accountError.textContent = error.message;
  } finally {
    elements.accountTest.disabled = false;
  }
});

elements.accountForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  elements.accountError.textContent = "";
  elements.accountSave.disabled = true;
  const payload = {
    id: elements.accountId.value.trim(),
    name: elements.accountName.value.trim(),
    access_key_id: elements.accountKeyId.value.trim(),
    access_key_secret: elements.accountKeySecret.value,
    totp: elements.accountTotp.value.trim(),
  };
  try {
    const path = editingAccountId ? `/api/accounts/${encodeURIComponent(editingAccountId)}` : "/api/accounts";
    await api(path, { method: "POST", body: JSON.stringify(payload) });
    elements.accountKeyId.value = "";
    elements.accountKeySecret.value = "";
    elements.accountTotp.value = "";
    elements.accountDialog.close();
    await refreshConfiguration();
    await refresh(true);
    showBanner(editingAccountId ? "账号已更新" : "账号已添加");
  } catch (error) {
    elements.accountError.textContent = error.message;
  } finally {
    elements.accountSave.disabled = false;
  }
});

elements.instanceForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  elements.instanceError.textContent = "";
  elements.instanceSave.disabled = true;
  const payload = {
    id: elements.instanceKey.value.trim(),
    name: elements.instanceName.value.trim(),
    account: elements.instanceAccount.value,
    region: elements.instanceRegion.value.trim(),
    instance_id: elements.instanceCloudId.value.trim(),
    traffic_threshold_gb: Number(elements.instanceThreshold.value),
    totp: elements.instanceTotp.value.trim(),
  };
  try {
    const path = editingInstanceId ? `/api/instances/${encodeURIComponent(editingInstanceId)}` : "/api/instances";
    await api(path, { method: "POST", body: JSON.stringify(payload) });
    elements.instanceTotp.value = "";
    elements.instanceDialog.close();
    await refreshConfiguration();
    await refresh(true);
    showBanner(editingInstanceId ? "实例已更新" : "实例已添加");
  } catch (error) {
    elements.instanceError.textContent = error.message;
  } finally {
    elements.instanceSave.disabled = false;
  }
});

elements.deleteForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!pendingDelete) return;
  elements.deleteError.textContent = "";
  elements.deleteConfirm.disabled = true;
  try {
    await api(`/api/${pendingDelete.resource}/${encodeURIComponent(pendingDelete.id)}`, {
      method: "DELETE",
      body: JSON.stringify({ totp: elements.deleteTotp.value.trim() }),
    });
    elements.deleteTotp.value = "";
    elements.deleteDialog.close();
    pendingDelete = null;
    await refreshConfiguration();
    await refresh(true);
    showBanner("配置已删除");
  } catch (error) {
    elements.deleteError.textContent = error.message;
  } finally {
    elements.deleteConfirm.disabled = false;
  }
});

elements.logoutButton.addEventListener("click", async () => {
  await api("/api/logout", { method: "POST", body: "{}" }).catch(() => null);
  managementOpen = false;
  elements.managementView.hidden = true;
  elements.fleetView.hidden = false;
  elements.manageButton.textContent = "管理";
  await api("/api/session").catch(() => null);
  showLogin();
});

(async function initialize() {
  try {
    const session = await api("/api/session");
    if (session.authenticated) {
      showDashboard();
      await refresh();
    } else {
      showLogin();
    }
  } catch (error) {
    showLogin();
    elements.loginError.textContent = error.message;
  }
})();
