const elements = Object.fromEntries([
  "login-view", "dashboard-view", "login-form", "login-error", "token", "totp-field", "totp",
  "fleet-meta", "fleet-count", "target-list", "target-account", "target-name", "target-meta",
  "status-heading", "status-dot", "traffic-heading", "traffic-threshold", "traffic-percent",
  "traffic-progress", "traffic-error", "refresh-button", "manage-button", "logout-button", "start-button", "stop-button",
  "auto-toggle", "check-button", "operation-state", "last-check", "last-action",
  "last-action-time", "banner", "stop-dialog", "stop-target-name", "confirm-stop",
  "schedule-form", "schedule-toggle", "schedule-start", "schedule-stop", "schedule-timezone",
  "schedule-next", "schedule-save", "fleet-view", "target-detail", "management-view", "management-close",
  "account-list", "instance-list", "account-add", "instance-add", "account-dialog", "account-form",
  "account-dialog-title", "account-id", "account-name", "account-key-id", "account-key-secret",
  "account-totp", "account-error", "account-test", "account-save", "instance-dialog", "instance-form",
  "instance-dialog-title", "instance-key", "instance-name", "instance-account", "instance-region",
  "instance-cloud-id", "instance-threshold", "instance-totp", "instance-error", "instance-save",
  "delete-dialog", "delete-form", "delete-title", "delete-description", "delete-totp", "delete-error",
  "delete-confirm",
].map((id) => [id.replace(/-([a-z])/g, (_, letter) => letter.toUpperCase()), document.querySelector(`#${id}`)]));

let targets = [];
let selectedId = null;
let busy = false;
let configuration = { accounts: [], instances: [] };
let managementOpen = false;
let editingAccountId = null;
let editingInstanceId = null;
let pendingDelete = null;

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

function showLogin(totpRequired = false) {
  elements.dashboardView.hidden = true;
  elements.loginView.hidden = false;
  elements.totpField.hidden = !totpRequired;
  elements.totp.required = totpRequired;
  elements.token.focus();
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

function actionText(action, result) {
  if (!action) return "暂无操作";
  const actions = {
    start: "启动实例", stop: "停止实例", auto_enabled: "启用自动策略", auto_disabled: "暂停自动策略",
    schedule_enabled: "启用定时计划", schedule_disabled: "暂停定时计划",
    schedule_start: "定时启动", schedule_stop: "定时停止",
    combined_start: "联合策略启动", combined_stop: "联合策略停止",
  };
  const results = {
    "Start request accepted": "启动请求已提交",
    "Graceful stop request accepted": "正常关机请求已提交",
    "Instance is already running": "实例已在运行",
    "Instance is already stopped": "实例已停止",
    "Automatic traffic policy enabled": "自动策略已启用",
    "Automatic traffic policy paused": "自动策略已暂停",
    "Power schedule enabled": "定时计划已启用",
    "Power schedule paused": "定时计划已暂停",
  };
  return `${actions[action] || action} · ${results[result] || result || "已完成"}`;
}

function setBusy(value, label = "就绪") {
  busy = value;
  elements.operationState.textContent = label;
  for (const control of [
    elements.refreshButton, elements.autoToggle, elements.checkButton, elements.scheduleToggle,
    elements.scheduleStart, elements.scheduleStop, elements.scheduleTimezone, elements.scheduleSave,
    ...document.querySelectorAll('input[name="schedule-weekday"]'),
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
  elements.trafficThreshold.textContent = `阈值 ${Number(target.threshold_gb).toFixed(0)} GB`;
  const percent = hasTraffic ? Math.max(0, Math.min(Number(target.usage_percent) || 0, 100)) : 0;
  elements.trafficPercent.textContent = hasTraffic ? `${percent.toFixed(1)}%` : "--";
  elements.trafficProgress.style.width = `${percent}%`;
  elements.trafficProgress.className = `progress-value${percent >= 100 ? " danger" : percent >= 80 ? " warning" : ""}`;
  elements.autoToggle.checked = Boolean(target.auto_enabled);
  elements.autoToggle.disabled = busy || target.status === "Error";
  elements.lastCheck.textContent = formatTime(target.last_check);
  elements.lastAction.textContent = target.error || actionText(target.last_action, target.last_result);
  elements.lastActionTime.textContent = target.last_check ? formatTime(target.last_check) : "";
  elements.stopTargetName.textContent = target.name;
  elements.scheduleToggle.checked = Boolean(target.schedule_enabled);
  elements.scheduleStart.value = target.schedule_start || "08:00";
  elements.scheduleStop.value = target.schedule_stop || "23:00";
  elements.scheduleTimezone.value = target.schedule_timezone || "Asia/Shanghai";
  const selectedDays = new Set(target.schedule_weekdays || []);
  for (const checkbox of document.querySelectorAll('input[name="schedule-weekday"]')) {
    checkbox.checked = selectedDays.has(Number(checkbox.value));
  }
  if (target.schedule_next_at) {
    const action = target.schedule_next_action === "start" ? "启动" : "停止";
    try {
      const formatted = new Intl.DateTimeFormat("zh-CN", {
        timeZone: target.schedule_timezone,
        month: "2-digit", day: "2-digit", weekday: "short", hour: "2-digit", minute: "2-digit",
        hour12: false,
      }).format(new Date(target.schedule_next_at));
      elements.scheduleNext.textContent = `${formatted} · ${action} · ${target.schedule_timezone}`;
    } catch (_) {
      elements.scheduleNext.textContent = `${target.schedule_next_at} · ${action}`;
    }
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
    if (!preserveBanner) showBanner("");
  } catch (error) {
    if (error.status === 401) {
      const session = await api("/api/session").catch(() => ({ totp_required: false }));
      showLogin(session.totp_required);
      return;
    }
    showBanner(error.message, true);
  } finally {
    setBusy(false);
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
    await api("/api/login", { method: "POST", body: JSON.stringify({ token: elements.token.value, totp: elements.totp.value }) });
    elements.token.value = "";
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
elements.scheduleForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const weekdays = [...document.querySelectorAll('input[name="schedule-weekday"]:checked')]
    .map((checkbox) => Number(checkbox.value));
  runTargetAction(
    "schedule",
    {
      enabled: elements.scheduleToggle.checked,
      start: elements.scheduleStart.value,
      stop: elements.scheduleStop.value,
      weekdays,
      timezone: elements.scheduleTimezone.value.trim(),
    },
    "保存中",
    elements.scheduleToggle.checked ? "定时计划已启用" : "定时计划已保存并暂停",
  );
});

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
  const session = await api("/api/session").catch(() => ({ totp_required: false }));
  showLogin(session.totp_required);
});

(async function initialize() {
  try {
    const session = await api("/api/session");
    if (session.authenticated) {
      showDashboard();
      await refresh();
    } else {
      showLogin(session.totp_required);
    }
  } catch (error) {
    showLogin(false);
    elements.loginError.textContent = error.message;
  }
})();
