/* Qoder Cockpit 前端：四视图路由 + 原生状态管理。
   组织方式：工具函数 → 全局状态 → 路由 → 反馈组件（toast/确认弹窗/抽屉）→
   各视图渲染 → 写操作动作 → 事件绑定与初始化。 */
"use strict";

const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];

/* ============================== 基础工具 ============================== */

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok)
    throw new Error(body.error || `请求失败（${response.status}）`);
  return body;
}

function escapeHtml(value) {
  return String(value ?? "").replace(
    /[&<>'"]/g,
    (char) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[
        char
      ],
  );
}

function money(value) {
  if (value === null || value === undefined || value === "") return "—";
  return String(value);
}

function quotaPercent(account) {
  const direct = Number(account.credits_remaining_percent);
  if (Number.isFinite(direct)) return Math.max(0, Math.min(100, direct));
  const total = Number(account.credits_total);
  const remaining = Number(account.credits_remaining);
  return total > 0 && Number.isFinite(remaining)
    ? Math.max(0, Math.min(100, (remaining / total) * 100))
    : null;
}

function creditsNumber(value) {
  if (value === null || value === undefined || value === "") return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function formatCredits(value) {
  const number = creditsNumber(value);
  return number === null
    ? "—"
    : new Intl.NumberFormat("en-US", { maximumFractionDigits: 2 }).format(number);
}

function quotaUsedPercent(account) {
  const total = creditsNumber(account.credits_total);
  const used = creditsNumber(account.credits_used);
  if (total > 0 && used !== null) {
    return Math.max(0, Math.min(100, (used / total) * 100));
  }
  const remaining = quotaPercent(account);
  return remaining === null ? null : 100 - remaining;
}

function quotaUpdatedLabel(account) {
  const value = account.quota_refreshed_at || account.updated_at;
  return value
    ? new Date(value).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })
    : "—";
}

function accountLabel(account) {
  return account.display_name
    ? `${account.display_name} · ${account.email}`
    : account.email;
}

function chatId(chat) {
  return String(chat.id || chat.sessionId || chat.session_id || "");
}

function chatTime(value) {
  const date = chatDate(value);
  return date
    ? date.toLocaleString("zh-CN", {
        month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
      })
    : "—";
}

function chatTimeFull(value) {
  const date = chatDate(value);
  return date ? date.toLocaleString("zh-CN") : "—";
}

function chatDate(value) {
  if (value === null || value === undefined || value === "") return null;
  let date;
  if (typeof value === "number" || /^\d+(\.\d+)?$/.test(String(value).trim())) {
    const number = Number(value);
    date = new Date(Math.abs(number) < 100000000000 ? number * 1000 : number);
  } else {
    date = new Date(String(value));
  }
  return Number.isNaN(date.getTime()) ? null : date;
}

function chatContextCount(chat) {
  return Array.isArray(chat.context) ? chat.context.length : null;
}

// 按剩余百分比给额度进度条定性：低于 15% 提醒、为 0 告警。
function trackTone(percent) {
  if (percent === null) return "";
  if (percent <= 0) return "is-empty";
  if (percent < 15) return "is-low";
  return "";
}

function initialsOf(account) {
  const base = account.display_name || account.email || "?";
  return escapeHtml(base.slice(0, 1));
}

function shortPath(path) {
  const value = String(path || "").replace(/\\/g, "/");
  const parts = value.split("/").filter(Boolean);
  return parts.slice(-2).join("/");
}

function renderIcons() {
  window.lucide?.createIcons({ attrs: { "stroke-width": 1.8 } });
}

// 请求期间按钮禁用并显示内联转圈，文字保持不变，避免布局跳动。
function busy(button, fn, busyLabel) {
  if (!button || button.disabled) return Promise.resolve();
  const label = button.querySelector(".btn-label");
  const idle = label ? label.textContent : "";
  button.disabled = true;
  if (busyLabel && label) label.textContent = busyLabel;
  const spinner = document.createElement("span");
  spinner.className = "spinner";
  spinner.setAttribute("aria-hidden", "true");
  button.prepend(spinner);
  return Promise.resolve(fn()).finally(() => {
    spinner.remove();
    button.disabled = false;
    if (label) label.textContent = idle;
  });
}

/* ============================== 全局状态 ============================== */

const appState = {
  view: "overview",
  config: null,
  configError: null,
  accounts: [],
  current: null,
  accountsError: null,
  projects: [],
  projectsError: null,
  selectedAccountId: null,
};

const sessionState = {
  workspaces: [],
  workspacesError: null,
  workspaceId: "",
  chats: [],
  chatsError: null,
  kind: "all",
  query: "",
  selectedIds: new Set(),
};

const backupState = { items: [], error: null };

function currentWorkspace() {
  return sessionState.workspaces.find((w) => w.id === sessionState.workspaceId) || null;
}

/* ============================== 路由 ============================== */

const VIEW_IDS = {
  overview: "view-overview",
  accounts: "view-accounts",
  sessions: "view-sessions",
  backups: "view-backups",
};

// 兼容旧锚点：#chat-management 等旧分区链接统一落到会话视图。
function viewFromHash() {
  const raw = location.hash.replace(/^#\/?/, "");
  if (["overview", "accounts", "sessions", "backups"].includes(raw)) return raw;
  if (["chat-management", "chat", "sessions-management"].includes(raw)) return "sessions";
  return "overview";
}

function applyRoute() {
  const view = viewFromHash();
  appState.view = view;
  for (const [name, id] of Object.entries(VIEW_IDS)) {
    document.getElementById(id).hidden = name !== view;
  }
  $$("[data-view-link]").forEach((link) => {
    if (link.dataset.viewLink === view) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
  // 账号数据可能已在总览加载完成；切换视图时立即用缓存渲染，不能等待 15 秒后台轮询。
  if (view === "accounts") renderAccountList();
  window.scrollTo({ top: 0 });
}

/* ============================== Toast 反馈 ============================== */

// 成功/警告 4 秒自动消失；错误不自动消失并提供关闭按钮。
function toast(message, tone = "success") {
  const region = $("#toast-region");
  const node = document.createElement("div");
  node.className = `toast tone-${tone}`;
  node.setAttribute("role", tone === "error" ? "alert" : "status");
  node.innerHTML = `<span class="toast-text">${escapeHtml(message)}</span>`;
  const dismiss = () => node.remove();
  if (tone === "error") {
    const close = document.createElement("button");
    close.type = "button";
    close.className = "toast-close";
    close.setAttribute("aria-label", "关闭提示");
    close.textContent = "✕";
    close.addEventListener("click", dismiss);
    node.append(close);
  } else {
    setTimeout(dismiss, 4000);
  }
  region.append(node);
}

/* ============================== 确认弹窗 ============================== */

// Promise 化的二次确认：确认返回 true，取消/Escape 返回 false。
// requireAck 存在时，确认按钮须先勾选确认框才可点击。
function openConfirm(options) {
  return new Promise((resolve) => {
    const dialog = $("#confirm-dialog");
    const trigger = document.activeElement;
    const ok = $("#confirm-ok");
    const ackWrap = $("#confirm-ack-wrap");
    const ack = $("#confirm-ack");
    $("#confirm-title").textContent = options.title || "确认操作";
    $("#confirm-body").innerHTML = options.bodyHtml || "";
    ok.textContent = options.okLabel || "确认";
    ok.classList.toggle("btn-danger", options.danger !== false);
    if (!options.danger) ok.classList.add("btn-primary");
    ack.checked = false;
    ackWrap.hidden = !options.requireAck;
    $("#confirm-ack-text").textContent = options.requireAck || "";
    ok.disabled = Boolean(options.requireAck);

    let settled = false;
    const finish = (value) => {
      if (settled) return;
      settled = true;
      dialog.close();
      trigger?.focus?.();
      resolve(value);
    };
    const onCancel = () => finish(false);
    const onOk = () => finish(true);
    const onToggle = () => {
      ok.disabled = !ack.checked;
    };
    const onClose = () => finish(false);
    const onDialogClick = (event) => {
      if (event.target === dialog) finish(false);
    };
    const cleanup = () => {
      $("#confirm-cancel").removeEventListener("click", onCancel);
      ok.removeEventListener("click", onOk);
      ack.removeEventListener("change", onToggle);
      dialog.removeEventListener("click", onDialogClick);
    };
    $("#confirm-cancel").addEventListener("click", onCancel);
    ok.addEventListener("click", onOk);
    ack.addEventListener("change", onToggle);
    // 点击卡片以外区域视为取消，关闭时移除监听避免下一次弹窗误触发。
    dialog.addEventListener("click", onDialogClick);
    dialog.addEventListener("close", () => {
      cleanup();
      onClose();
    }, { once: true });
    dialog.showModal();
    (options.requireAck ? ack : ok).focus();
  });
}

/* ============================== 行内更多菜单 ============================== */

function closeRowMenus() {
  $$(".row-menu").forEach((menu) => menu.remove());
}
// 点击菜单外部时关闭。注意：菜单创建后的 renderIcons() 会替换带 data-lucide 的
// 图标节点，导致真实点击的 event.target 脱离 DOM、closest 失效；
// composedPath 是事件派发时的路径快照，不受中途替换影响。
document.addEventListener("click", (event) => {
  const path = typeof event.composedPath === "function" ? event.composedPath() : [];
  const inWrap =
    path.some((node) => node.classList?.contains("row-menu-wrap")) ||
    event.target.closest?.(".row-menu-wrap");
  if (!inWrap) closeRowMenus();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeRowMenus();
});

/* ============================== 会话详情抽屉 ============================== */

let drawerTrigger = null;

function openDrawer(chat) {
  const drawer = $("#session-drawer");
  const workspace = currentWorkspace();
  const contextCount = chatContextCount(chat);
  // 详情只展示元数据与上下文摘要，不内嵌完整 transcript，避免大体积 DOM。
  $("#drawer-title").textContent = chat.title || "无标题";
  $("#drawer-body").innerHTML = `
    <dl class="kv">
      <dt>类型</dt><dd>${chat.kind === "quest" ? "Quest 任务" : "IDE 对话"}</dd>
      ${chat.kind === "quest" ? `<dt>状态</dt><dd>${questStatusBadge(chat.status)}</dd>` : ""}
      <dt>时间</dt><dd>${escapeHtml(chatTimeFull(chat.timestamp))}</dd>
      <dt>会话 ID</dt><dd><code>${escapeHtml(chatId(chat))}</code></dd>
      <dt>工作区</dt><dd>${escapeHtml(workspace?.name || "—")}</dd>
      <dt>工作区路径</dt><dd><code>${escapeHtml(workspace?.path || "—")}</code></dd>
      <dt>上下文条目</dt><dd>${contextCount === null ? "—" : contextCount}</dd>
    </dl>
    ${Array.isArray(chat.context) && chat.context.length ? `
      <div class="detail-block"><h3>上下文摘要</h3><ul class="kv" style="display:grid;gap:var(--space-1);padding-left:0;list-style:none;margin:0;">
        ${chat.context.slice(0, 8).map((item) => `<li style="font:12px var(--font-mono);color:var(--color-text-muted);overflow-wrap:anywhere;">${escapeHtml(typeof item === "string" ? item : JSON.stringify(item))}</li>`).join("")}
        ${chat.context.length > 8 ? `<li style="color:var(--color-text-muted);font-size:12px;">… 共 ${chat.context.length} 条</li>` : ""}
      </ul></div>` : ""}
    <div class="detail-actions">
      <a class="btn btn-secondary" href="/api/chat/export"><i data-lucide="file-down" aria-hidden="true"></i><span class="btn-label">导出 Markdown</span></a>
    </div>`;
  drawerTrigger = document.activeElement;
  drawer.hidden = false;
  requestAnimationFrame(() => drawer.classList.add("is-open"));
  $("#drawer-close").focus();
  renderIcons();
}

function closeDrawer() {
  const drawer = $("#session-drawer");
  drawer.classList.remove("is-open");
  // 等滑出动画结束后再隐藏容器，避免闪烁。
  setTimeout(() => {
    drawer.hidden = true;
  }, 200);
  drawerTrigger?.focus?.();
  drawerTrigger = null;
}

/* ============================== 总览视图 ============================== */

function renderRuntimeBadge() {
  const dot = $("#runtime-dot");
  const text = $("#runtime-text");
  if (appState.configError) {
    dot.className = "status-dot is-bad";
    text.textContent = "状态读取失败";
    return;
  }
  if (!appState.config) {
    dot.className = "status-dot is-checking";
    text.textContent = "正在检查 Qoder";
    return;
  }
  dot.className = `status-dot ${appState.config.qoder_running ? "is-ok" : "is-bad"}`;
  text.textContent = appState.config.qoder_running ? "Qoder 运行中" : "Qoder 未运行";
}

function renderOverview() {
  renderRuntimeBadge();
  const running = appState.config?.qoder_running;

  // 首屏主按钮仅在 Qoder 未运行时出现，避免与快捷入口争抢注意力。
  const actions = $("#overview-actions");
  actions.innerHTML = running === false
    ? `<button class="btn btn-primary" id="start-qoder-btn" type="button"><i data-lucide="play" aria-hidden="true"></i><span class="btn-label">启动 Qoder</span></button>`
    : "";
  $("#start-qoder-btn")?.addEventListener("click", (event) =>
    busy(event.currentTarget, startQoder, "启动中…"),
  );

  // 运行状态条：Qoder 状态、当前账号、最后同步时间。
  const bar = $("#runtime-bar");
  if (appState.configError) {
    bar.innerHTML = `<div class="error-box">
      <span>${escapeHtml(appState.configError)}</span>
      <button class="btn btn-secondary" id="overview-retry" type="button"><span class="btn-label">重试</span></button>
    </div>`;
    $("#overview-retry").addEventListener("click", loadCore);
  } else if (!appState.config) {
    bar.innerHTML = `<span class="rb-item"><span class="status-dot is-checking"></span>正在检查本机状态…</span>`;
  } else {
    bar.innerHTML = `
      <span class="rb-item"><span class="status-dot ${running ? "is-ok" : "is-bad"}"></span><strong>${running ? "Qoder 运行中" : "Qoder 未运行"}</strong>${running ? "<span>· 切号时会自动关闭并重启</span>" : "<span>· 可以执行账号切换</span>"}</span>
      <span class="rb-item">当前账号 <strong>${escapeHtml(appState.current?.email || "未识别")}</strong></span>
      <span class="rb-item">额度同步 <strong>${escapeHtml(appState.current ? quotaUpdatedLabel(appState.current) : "—")}</strong></span>
      <span class="rb-spacer"></span>
      <span class="rb-item"><code>${escapeHtml(appState.config.state_db || "未找到 state.vscdb")}</code></span>`;
  }

  $("#m-accounts").textContent = appState.accountsError ? "—" : appState.accounts.length;
  $("#m-workspaces").textContent = sessionState.workspacesError ? "—" : sessionState.workspaces.length;
  $("#m-sessions").textContent = sessionState.workspacesError
    ? "—"
    : sessionState.workspaces.reduce((sum, w) => sum + (w.chat_count || 0), 0);

  renderAttention();
  renderIcons();
}

// 需要处理：把真正需要用户动作的事项按优先级列出，无事可做为正向反馈。
function renderAttention() {
  const items = [];
  const push = (tone, icon, text, href = null) =>
    items.push({ tone, icon, text, href });

  // 首次加载尚未拿到配置时先展示检查中，避免误报“一切正常”。
  if (!appState.config && !appState.configError) {
    $("#attention-list").innerHTML = '<li class="empty-state">正在检查本机状态…</li>';
    return;
  }

  if (appState.configError) push("danger", "alert-triangle", `运行状态读取失败：${appState.configError}`);
  else if (appState.config && !appState.config.qoder_running)
    push("warning", "power", "Qoder 未运行，可点击右上角“启动 Qoder”。");
  for (const account of appState.accounts) {
    if (account.quota_error) push("danger", "alert-triangle", `账号 ${account.email} 额度同步失败，需要重新授权或刷新额度。`, "#/accounts");
    else {
      const percent = quotaPercent(account);
      if (percent !== null && percent <= 0) push("danger", "battery-low", `账号 ${account.email} 额度已用完。`, "#/accounts");
    }
  }
  if (sessionState.workspacesError) push("danger", "alert-triangle", `工作区读取失败：${sessionState.workspacesError}`);
  else if (appState.config && sessionState.workspaces.length === 0)
    push("warning", "folder-open", "没有可管理的 Qoder 工作区。");
  if (!items.length) push("ok", "circle-check", "一切正常，没有需要处理的事项。");

  $("#attention-list").innerHTML = items
    .map((item) => `<li class="attention-item tone-${item.tone}">
      <i data-lucide="${item.icon}" aria-hidden="true"></i>
      <span class="ai-text">${escapeHtml(item.text)}</span>
      ${item.href ? `<a class="btn btn-ghost" href="${item.href}"><span class="btn-label">去处理</span></a>` : ""}
    </li>`)
    .join("");
}

/* ============================== 账号视图 ============================== */

function renderAccountList() {
  const list = $("#account-list");
  if (appState.accountsError) {
    list.innerHTML = `<div class="error-box"><span>${escapeHtml(appState.accountsError)}</span>
      <button class="btn btn-secondary" id="accounts-retry" type="button"><span class="btn-label">重试</span></button></div>`;
    $("#accounts-retry").addEventListener("click", loadCore);
    $("#account-detail").innerHTML = "";
    return;
  }
  if (!appState.accounts.length) {
    list.innerHTML = `<div class="empty-state">还没有保存的账号。<button class="btn btn-primary" id="accounts-add-inline" type="button"><i data-lucide="user-plus" aria-hidden="true"></i><span class="btn-label">添加账号</span></button></div>`;
    $("#accounts-add-inline").addEventListener("click", startOauth);
    $("#account-detail").innerHTML = "";
    renderIcons();
    return;
  }
  // 额度未知的账号排在末尾；已知额度按剩余 credits 从高到低展示。
  const sortedAccounts = [...appState.accounts].sort((a, b) => {
    const remainingA = creditsNumber(a.credits_remaining);
    const remainingB = creditsNumber(b.credits_remaining);
    if (remainingA === null) return remainingB === null ? 0 : 1;
    if (remainingB === null) return -1;
    return remainingB - remainingA;
  });
  if (!appState.selectedAccountId || !appState.accounts.some((a) => a.id === appState.selectedAccountId)) {
    appState.selectedAccountId = appState.current?.id || sortedAccounts[0].id;
  }
  list.innerHTML = sortedAccounts
    .map((account) => {
      const isCurrent = appState.current?.id === account.id || appState.current?.email === account.email;
      const remainingPercent = quotaPercent(account);
      const usedPercent = quotaUsedPercent(account);
      return `<button type="button" class="account-row ${account.id === appState.selectedAccountId ? "is-selected" : ""}" data-account-id="${escapeHtml(account.id)}">
        <span class="avatar" aria-hidden="true">${initialsOf(account)}</span>
        <span class="ar-main">
          <span class="ar-name">${escapeHtml(account.display_name || account.email)}</span>
          <span class="ar-sub">
            <span class="badge">${escapeHtml(account.plan || "套餐未识别")}</span>
            ${account.quota_error ? '<span class="badge tone-danger">同步失败</span>' : ""}
            ${isCurrent ? '<span class="badge tone-success"><i data-lucide="circle-check"></i>当前</span>' : '<span class="badge tone-primary">可用</span>'}
          </span>
        </span>
        <span class="ar-side">
          <span class="account-quota-track ${trackTone(remainingPercent)}" role="img" aria-label="已使用额度 ${usedPercent === null ? "未知" : Math.round(usedPercent) + "%"}"><span style="width:${usedPercent === null ? 0 : usedPercent}%"></span></span>
          <span class="account-quota-summary">
            <span class="account-quota-used">${escapeHtml(formatCredits(account.credits_used))} / ${escapeHtml(formatCredits(account.credits_total))} <span>（已使用 ${usedPercent === null ? "—" : Math.round(usedPercent) + "%"}）</span></span>
            <span class="account-quota-remaining">剩余 <b>${escapeHtml(formatCredits(account.credits_remaining))}</b></span>
          </span>
        </span>
      </button>`;
    })
    .join("");
  list.querySelectorAll("[data-account-id]").forEach((row) =>
    row.addEventListener("click", () => {
      appState.selectedAccountId = row.dataset.accountId;
      renderAccountList();
    }),
  );
  renderAccountDetail();
  renderIcons();
}

function renderAccountDetail() {
  const detail = $("#account-detail");
  const account = appState.accounts.find((a) => a.id === appState.selectedAccountId);
  if (!account) {
    detail.innerHTML = "";
    return;
  }
  const isCurrent = appState.current?.id === account.id || appState.current?.email === account.email;
  const percent = quotaPercent(account);
  const options = appState.accounts
    .map((a) => `<option value="${escapeHtml(a.id)}">${escapeHtml(accountLabel(a))}</option>`)
    .join("");

  detail.innerHTML = `
    <div class="detail-block">
      <h3>账号信息</h3>
      <dl class="kv">
        <dt>显示名</dt><dd>${escapeHtml(account.display_name || "—")}</dd>
        <dt>邮箱</dt><dd>${escapeHtml(account.email || "—")}</dd>
        <dt>账号 ID</dt><dd><code>${escapeHtml(account.id)}</code></dd>
        <dt>套餐</dt><dd>${escapeHtml(account.plan || "套餐未识别")}</dd>
        <dt>状态</dt><dd>${isCurrent ? '<span class="badge tone-success"><i data-lucide="circle-check"></i>当前登录</span>' : '<span class="badge tone-primary">可用</span>'}</dd>
        <dt>额度同步</dt><dd>${escapeHtml(quotaUpdatedLabel(account))}${account.quota_error ? ` <span class="badge tone-danger">同步失败</span>` : ""}</dd>
      </dl>
    </div>
    <div class="detail-block quota-block">
      <h3>额度</h3>
      <div class="quota-track" aria-label="剩余额度 ${percent === null ? "未知" : percent.toFixed(1) + "%"}"><span style="width:${percent === null ? 0 : percent}%"></span></div>
      <div class="quota-nums">
        <span>剩余 <b>${escapeHtml(money(account.credits_remaining))}</b></span>
        <span>已用 <b>${escapeHtml(money(account.credits_used))}</b></span>
        <span>总量 <b>${escapeHtml(money(account.credits_total))}</b></span>
      </div>
    </div>
    <div class="detail-block">
      <h3>切换与继承</h3>
      <p class="view-desc" style="margin-bottom:var(--space-3);">切换时会自动关闭 Qoder、备份当前数据、写入目标账号后重新启动。</p>
      <div class="field-row">
        <label for="source-account">会话来源账号（保存切换前的项目会话）</label>
        <select id="source-account"><option value="">不保存上一个账号</option>${options}</select>
      </div>
      <label class="toggle-row"><input type="checkbox" id="inherit-sessions" checked /><span>继承上一账号的 IDE 历史会话</span></label>
      <div class="detail-actions">
        ${isCurrent ? "" : `<button class="btn btn-primary" id="switch-account-btn" type="button"><i data-lucide="repeat-2" aria-hidden="true"></i><span class="btn-label">切换到此账号</span></button>`}
        <button class="btn btn-danger" id="delete-account-btn" type="button"><i data-lucide="trash-2" aria-hidden="true"></i><span class="btn-label">删除账号记录</span></button>
      </div>
    </div>
    <div class="detail-block">
      <h3>项目会话快照</h3>
      <p class="view-desc" style="margin-bottom:var(--space-3);">为项目目录保存一份会话快照，切号后可按账号恢复。</p>
      ${renderProjectRows()}
    </div>`;

  const sourceSelect = $("#source-account");
  sourceSelect.value = isCurrent ? account.id : appState.current?.id || "";
  $("#switch-account-btn")?.addEventListener("click", (event) =>
    busy(event.currentTarget, () => switchAccount(account.id, account), "切换中…"),
  );
  $("#delete-account-btn").addEventListener("click", () => deleteAccount(account));
  detail.querySelectorAll("[data-save-project]").forEach((button) =>
    button.addEventListener("click", () => saveProject(button.dataset.saveProject)),
  );
  renderIcons();
}

function renderProjectRows() {
  if (appState.projectsError) {
    return `<div class="error-box"><span>${escapeHtml(appState.projectsError)}</span></div>`;
  }
  if (!appState.projects.length) {
    return `<div class="empty-state" style="padding:var(--space-4);">没有发现 Qoder 项目目录。</div>`;
  }
  return `<div class="table-wrap"><table class="data-table">
    <thead><tr><th>项目</th><th>对话</th><th>执行</th><th class="ops-col"><span class="sr-only">操作</span></th></tr></thead>
    <tbody>${appState.projects
      .map(
        (project) => `<tr>
          <td class="cell-title" title="${escapeHtml(project.path)}">${escapeHtml(project.name)}</td>
          <td data-label="对话">${project.transcript_count}</td>
          <td data-label="执行">${project.execution_count}</td>
          <td class="ops-col" data-label="操作"><button class="btn btn-ghost" data-save-project="${escapeHtml(project.path)}" style="min-height:36px;padding:0 var(--space-3);"><i data-lucide="archive" aria-hidden="true"></i><span class="btn-label">保存快照</span></button></td>
        </tr>`,
      )
      .join("")}</tbody>
  </table></div>`;
}

/* ============================== 会话视图 ============================== */

function filteredChats() {
  const query = sessionState.query.toLowerCase();
  return sessionState.chats.filter((chat) => {
    if (sessionState.kind === "ide" && chat.kind === "quest") return false;
    if (sessionState.kind === "quest" && chat.kind !== "quest") return false;
    return String(chat.title || "无标题").toLowerCase().includes(query);
  });
}

const QUEST_STATUS_LABELS = {
  completed: "已完成",
  running: "进行中",
  pending: "等待中",
  failed: "失败",
  cancelled: "已取消",
};

// Quest 状态：已知状态给中文映射与语义色；未知状态保留原文并标记“未知”。
function questStatusBadge(status) {
  if (!status) return '<span class="badge">—</span>';
  const label = QUEST_STATUS_LABELS[status];
  if (label === "已完成") return `<span class="badge tone-success"><i data-lucide="circle-check" aria-hidden="true"></i>${label}</span>`;
  if (label === "进行中") return `<span class="badge tone-primary"><i data-lucide="loader-circle" aria-hidden="true"></i>${label}</span>`;
  if (label === "失败") return `<span class="badge tone-danger"><i data-lucide="circle-alert" aria-hidden="true"></i>${label}</span>`;
  if (label) return `<span class="badge"><i data-lucide="clock" aria-hidden="true"></i>${label}</span>`;
  return `<span class="badge tone-warning"><i data-lucide="circle-help" aria-hidden="true"></i>未知（${escapeHtml(status)}）</span>`;
}

function kindBadge(chat) {
  return chat.kind === "quest"
    ? '<span class="badge tone-primary"><i data-lucide="sparkles"></i>Quest</span>'
    : '<span class="badge"><i data-lucide="message-square"></i>IDE</span>';
}

function renderWorkspaces() {
  const list = $("#workspace-list");
  const mobile = $("#workspace-select-mobile");
  if (sessionState.workspacesError) {
    list.innerHTML = `<li><div class="error-box"><span>${escapeHtml(sessionState.workspacesError)}</span>
      <button class="btn btn-secondary" id="ws-retry" type="button"><span class="btn-label">重试</span></button></div></li>`;
    $("#ws-retry").addEventListener("click", loadSessions);
    mobile.innerHTML = "";
    return;
  }
  if (!sessionState.workspaces.length) {
    list.innerHTML = `<li><div class="empty-state" style="padding:var(--space-4);">没有可管理的 Qoder 工作区。<button class="btn btn-secondary" id="ws-empty-refresh" type="button"><i data-lucide="refresh-cw" aria-hidden="true"></i><span class="btn-label">刷新</span></button></div></li>`;
    $("#ws-empty-refresh").addEventListener("click", loadSessions);
    mobile.innerHTML = "";
    renderIcons();
    return;
  }
  if (!sessionState.workspaceId || !sessionState.workspaces.some((w) => w.id === sessionState.workspaceId)) {
    sessionState.workspaceId = sessionState.workspaces[0].id;
  }
  list.innerHTML = sessionState.workspaces
    .map((item) => `<li>
      <button type="button" class="workspace-item ${item.id === sessionState.workspaceId ? "is-active" : ""}" data-workspace-id="${escapeHtml(item.id)}" title="${escapeHtml(item.path)}">
        <span class="wi-name">${escapeHtml(item.name || item.id)}</span>
        <span class="wi-count">${item.chat_count || 0}</span>
        <span class="wi-path">${escapeHtml(shortPath(item.path))}</span>
      </button>
    </li>`)
    .join("");
  mobile.innerHTML = sessionState.workspaces
    .map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name || item.id)}（${item.chat_count || 0}）</option>`)
    .join("");
  mobile.value = sessionState.workspaceId;
}

function renderWorkspaceSummary() {
  const box = $("#workspace-summary");
  const workspace = currentWorkspace();
  if (!workspace) {
    box.innerHTML = "";
    return;
  }
  const visibleChats = filteredChats();
  const ideCount = visibleChats.filter((c) => c.kind !== "quest").length;
  const questCount = visibleChats.filter((c) => c.kind === "quest").length;
  const latest = visibleChats.reduce((max, c) => Math.max(max, chatDate(c.timestamp)?.getTime() || 0), 0);
  box.innerHTML = `
    <span class="ws-name">${escapeHtml(workspace.name || workspace.id)}</span>
    <span class="ws-stat"><i data-lucide="message-square" aria-hidden="true"></i>IDE <b>${ideCount}</b></span>
    <span class="ws-stat"><i data-lucide="sparkles" aria-hidden="true"></i>Quest <b>${questCount}</b></span>
    <span class="ws-stat"><i data-lucide="clock" aria-hidden="true"></i>最近 <b>${latest ? chatTime(latest) : "—"}</b></span>
    <span class="ws-path" title="${escapeHtml(workspace.path)}">${escapeHtml(workspace.path || "")}</span>`;
}

function renderSessionTable() {
  const tbody = $("#session-tbody");
  const workspace = currentWorkspace();

  // 工作区相关区域各自表达加载/错误/空状态，互不清空对方。
  if (!workspace) {
    tbody.innerHTML = `<tr><td colspan="7" class="empty-state">${sessionState.workspacesError ? "工作区加载失败，请在左侧重试。" : "请选择一个工作区。"}</td></tr>`;
    $("#workspace-summary").innerHTML = "";
    updateBulkBar();
    return;
  }
  if (sessionState.chatsError) {
    tbody.innerHTML = `<tr><td colspan="7"><div class="error-box"><span>${escapeHtml(sessionState.chatsError)}</span>
      <button class="btn btn-secondary" id="chats-retry" type="button"><span class="btn-label">重试</span></button></div></td></tr>`;
    $("#chats-retry").addEventListener("click", loadChats);
    return;
  }

  const rows = filteredChats();
  if (!rows.length) {
    const kindLabel = { all: "会话", ide: "IDE 对话", quest: "Quest 任务" }[sessionState.kind];
    const reason = sessionState.query
      ? `没有匹配“${sessionState.query}”的${kindLabel}。`
      : `此工作区没有${kindLabel}。`;
    tbody.innerHTML = `<tr><td colspan="7" class="empty-state">${escapeHtml(reason)}</td></tr>`;
  } else {
    tbody.innerHTML = rows
      .map((chat) => {
        const id = chatId(chat);
        const selected = sessionState.selectedIds.has(id);
        const contextCount = chatContextCount(chat);
        return `<tr class="${selected ? "is-selected" : ""}">
          <td class="check-col"><input type="checkbox" data-chat-check="${escapeHtml(id)}" ${selected ? "checked" : ""} aria-label="选择此会话" /></td>
          <td data-label="类型">${kindBadge(chat)}</td>
          <td class="cell-title" data-label="标题"><button type="button" data-open-chat="${escapeHtml(id)}">${escapeHtml(chat.title || "无标题")}</button></td>
          <td class="col-status" data-label="状态">${chat.kind === "quest" ? questStatusBadge(chat.status) : '<span class="badge">—</span>'}</td>
          <td class="cell-time" data-label="时间">${escapeHtml(chatTime(chat.timestamp))}</td>
          <td class="col-context" data-label="上下文">${contextCount === null ? "—" : contextCount}</td>
          <td class="ops-col">
            <span class="row-menu-wrap">
              <button type="button" class="btn-icon" data-row-menu="${escapeHtml(id)}" aria-label="更多操作" aria-haspopup="menu"><i data-lucide="more-vertical"></i></button>
            </span>
          </td>
        </tr>`;
      })
      .join("");
  }

  // 全选状态只统计当前筛选结果，避免隐藏行被误选。
  const selectAll = $("#session-select-all");
  selectAll.checked = rows.length > 0 && rows.every((chat) => sessionState.selectedIds.has(chatId(chat)));
  renderWorkspaceSummary();
  renderMergeRow();
  updateBulkBar();
  renderIcons();
}

function updateBulkBar() {
  $("#bulk-bar").hidden = sessionState.selectedIds.size === 0;
  $("#bulk-count").textContent = `已选 ${sessionState.selectedIds.size} 条`;
}

function renderMergeRow() {
  const select = $("#merge-target");
  const confirmBtn = $("#confirm-merge-btn");
  const targets = sessionState.workspaces.filter((w) => w.id !== sessionState.workspaceId);
  if (!targets.length) {
    select.innerHTML = `<option value="">没有其他可用工作区</option>`;
    select.disabled = true;
    confirmBtn.disabled = true;
    confirmBtn.title = "没有其他工作区可作为合并目标";
    return;
  }
  select.disabled = false;
  confirmBtn.disabled = false;
  confirmBtn.title = "";
  select.innerHTML = targets
    .map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name || item.id)}（${item.chat_count || 0}）</option>`)
    .join("");
}

async function loadWorkspaces() {
  try {
    const result = await api("/api/chat/workspaces");
    sessionState.workspaces = result.workspaces || [];
    sessionState.workspacesError = null;
  } catch (error) {
    sessionState.workspacesError = error.message;
  }
  renderWorkspaces();
  if (appState.view === "overview") renderOverview();
}

async function loadChats() {
  const workspace = currentWorkspace();
  if (!workspace) {
    sessionState.chats = [];
    renderSessionTable();
    return;
  }
  sessionState.chatsError = null;
  try {
    const result = await api(`/api/chat/list?workspace_id=${encodeURIComponent(workspace.id)}`);
    sessionState.chats = result.chats || [];
  } catch (error) {
    sessionState.chatsError = error.message;
  }
  sessionState.selectedIds.clear();
  renderSessionTable();
}

// 会话视图整体刷新：先工作区后会话，两步各自容错。
async function loadSessions() {
  await loadWorkspaces();
  await loadChats();
}

async function selectWorkspace(id) {
  sessionState.workspaceId = id;
  $("#workspace-select-mobile").value = id;
  renderWorkspaces();
  $("#merge-row").hidden = true;
  await loadChats();
}

/* ============================== 备份视图 ============================== */

async function loadBackups() {
  try {
    const result = await api("/api/chat/backups");
    backupState.items = result.backups || [];
    backupState.error = null;
  } catch (error) {
    backupState.error = error.message;
  }
  renderBackups();
}

function formatSize(bytes) {
  const value = Number(bytes);
  if (!Number.isFinite(value)) return "—";
  if (value >= 1048576) return `${(value / 1048576).toFixed(1)} MB`;
  if (value >= 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${value} B`;
}

function renderBackups() {
  const summary = $("#backup-summary");
  const tbody = $("#backup-tbody");
  if (backupState.error) {
    summary.innerHTML = "";
    tbody.innerHTML = `<tr><td colspan="5"><div class="error-box"><span>${escapeHtml(backupState.error)}</span>
      <button class="btn btn-secondary" id="backups-retry" type="button"><span class="btn-label">重试</span></button></div></td></tr>`;
    $("#backups-retry").addEventListener("click", loadBackups);
    return;
  }
  const items = backupState.items;
  const latest = items[0]?.backup_time;
  summary.innerHTML = `
    <span class="bs-item">备份总数 <strong>${items.length}</strong></span>
    <span class="bs-item">最近备份 <strong>${latest ? escapeHtml(new Date(latest).toLocaleString("zh-CN")) : "—"}</strong></span>
    <span class="bs-item">存储目录 <strong>cockpit-local/backups/chat</strong></span>`;
  if (!items.length) {
    tbody.innerHTML = `<tr><td colspan="5" class="empty-state">暂无备份。可在会话视图备份单个工作区，或使用右上角“备份全部”。</td></tr>`;
    return;
  }
  tbody.innerHTML = items
    .map((backup) => `<tr>
      <td class="cell-title" data-label="备份文件"><code style="font:12px var(--font-mono);">${escapeHtml(backup.name)}</code></td>
      <td data-label="覆盖工作区">${escapeHtml(backup.workspace)}${backup.workspace_count > 1 ? `（${backup.workspace_count} 个工作区）` : ""}</td>
      <td class="cell-time" data-label="创建时间">${backup.backup_time ? escapeHtml(new Date(backup.backup_time).toLocaleString("zh-CN")) : "—"}</td>
      <td data-label="大小">${formatSize(backup.size)}</td>
      <td class="ops-col">
        <button class="btn btn-ghost" data-backup-restore="${escapeHtml(backup.name)}" data-backup-workspace-id="${escapeHtml(backup.workspace_id || "")}" data-backup-workspace-count="${escapeHtml(backup.workspace_count || 1)}" style="min-height:36px;padding:0 var(--space-3);"><i data-lucide="history" aria-hidden="true"></i><span class="btn-label">恢复</span></button>
        <span class="row-menu-wrap">
          <button type="button" class="btn-icon" data-backup-menu="${escapeHtml(backup.name)}" aria-label="更多操作" aria-haspopup="menu"><i data-lucide="more-vertical"></i></button>
        </span>
      </td>
    </tr>`)
    .join("");
  renderIcons();
}

// 恢复确认：说明目标工作区、覆盖内容与自动生成的新备份，避免误覆盖。
async function restoreBackup(name, backupMeta = {}) {
  const targetId = backupMeta.workspaceCount > 1 ? "" : (backupMeta.workspaceId || "");
  const workspace = targetId
    ? sessionState.workspaces.find((item) => item.id === targetId) || null
    : null;
  const targetLabel = backupMeta.workspaceCount > 1
    ? "备份中包含的全部工作区"
    : (workspace?.name || backupMeta.workspaceId || "备份原工作区");
  const confirmed = await openConfirm({
    title: "恢复会话备份",
    danger: true,
    okLabel: "恢复",
    bodyHtml: `
      <p>目标工作区：<strong>${escapeHtml(targetLabel)}</strong></p>
      <p>将覆盖${backupMeta.workspaceCount > 1 ? "备份中各工作区" : "该工作区"}现有的 IDE 对话与 Quest 任务；恢复前会自动为当前数据生成一份新备份。</p>
      <p>恢复期间会自动关闭并按原状态重启 Qoder。</p>`,
  });
  if (!confirmed) return;
  try {
    await api("/api/chat/restore", {
      method: "POST",
      body: JSON.stringify({ name, target_workspace_id: targetId }),
    });
    toast("会话备份已恢复。");
    await refreshAllSessions();
  } catch (error) {
    toast(error.message, "error");
  }
}

/* ============================== 写操作动作 ============================== */

// 会话相关的写操作成功后，统一刷新工作区、会话与备份三处数据。
async function refreshAllSessions() {
  await loadWorkspaces();
  await loadChats();
  await loadBackups();
  if (appState.view === "overview") renderOverview();
}

async function startQoder() {
  try {
    const result = await api("/api/qoder/start", { method: "POST", body: "{}" });
    toast(result.message || `Qoder 已启动：${result.path}`);
    setTimeout(loadCore, 1200);
  } catch (error) {
    toast(error.message, "error");
  }
}

async function loadCore() {
  // 三个接口互相独立：单个失败只影响对应区域，不清空其它已加载数据。
  const results = await Promise.allSettled([
    api("/api/config"),
    api("/api/accounts"),
    api("/api/projects"),
  ]);
  const [config, accounts, projects] = results;
  if (config.status === "fulfilled") {
    appState.config = config.value;
    appState.configError = null;
  } else {
    appState.configError = config.reason?.message || "读取失败";
  }
  if (accounts.status === "fulfilled") {
    appState.accounts = accounts.value.accounts || [];
    appState.current = accounts.value.current;
    appState.accountsError = null;
  } else {
    appState.accountsError = accounts.reason?.message || "读取失败";
  }
  if (projects.status === "fulfilled") {
    appState.projects = projects.value.projects || [];
    appState.projectsError = null;
  } else {
    appState.projectsError = projects.reason?.message || "读取失败";
  }
  renderRuntimeBadge();
  renderOverview();
  if (appState.view === "accounts") renderAccountList();
}

async function refreshCredits() {
  try {
    const result = await api("/api/accounts/refresh", { method: "POST", body: "{}" });
    if (result.errors?.length) {
      toast(`额度已更新：${result.updated} 个账号，${result.errors.length} 个账号需要重新授权。`, "error");
    } else {
      toast(`额度已更新：${result.updated} 个账号。`);
    }
    await loadCore();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function importLocal() {
  try {
    await api("/api/accounts/import-local", { method: "POST", body: "{}" });
    toast("已导入当前 Qoder 账号。");
    await loadCore();
  } catch (error) {
    toast(error.message, "error");
  }
}

// 切号：后端负责保存旧账号、注入新账号、按选项继承会话并重启 Qoder；
// 成功后带回总览并用 toast 报告结果，失败保留在账号视图便于重试。
async function switchAccount(targetId, account) {
  const source = $("#source-account")?.value || appState.current?.id || "";
  if (!targetId) return toast("没有识别到目标账号。", "error");
  if (source && source === targetId) return toast("当前已经是这个账号。", "error");
  try {
    const result = await api("/api/switch", {
      method: "POST",
      body: JSON.stringify({
        source_account_id: source,
        target_account_id: targetId,
        save_previous: Boolean(source),
        inherit_sessions: $("#inherit-sessions")?.checked ?? true,
      }),
    });
    const localSessionCount = result.local_sessions?.session_count ?? 0;
    const skippedCount = Object.keys(result.local_sessions?.skipped_tables || {}).length;
    const compatibilityNote = skippedCount ? `；已跳过 ${skippedCount} 个未安装的扩展表` : "";
    location.hash = "#/overview";
    toast(
      `已切换到 ${account.email}：保存 ${result.saved.length} 个项目，恢复 ${result.restored.length} 个项目，迁移 ${localSessionCount} 条 IDE 历史会话${compatibilityNote}。`,
    );
    await loadCore();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function deleteAccount(account) {
  const confirmed = await openConfirm({
    title: "删除账号记录",
    danger: true,
    okLabel: "删除",
    bodyHtml: `<p>将从 Cockpit 删除账号 <strong>${escapeHtml(account.email)}</strong> 的本地记录。</p><p>不会删除 Qoder 原始登录数据，可随时重新导入或授权。</p>`,
  });
  if (!confirmed) return;
  try {
    await api("/api/accounts/delete", {
      method: "POST",
      body: JSON.stringify({ account_id: account.id }),
    });
    toast("账号记录已删除。");
    await loadCore();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function saveProject(path) {
  const account = $("#source-account")?.value || appState.current?.id;
  if (!account) return toast("请先在“切换与继承”中选择账号。", "error");
  try {
    await api("/api/session/save", {
      method: "POST",
      body: JSON.stringify({ account_id: account, project_path: path }),
    });
    toast("项目会话快照已保存。");
  } catch (error) {
    toast(error.message, "error");
  }
}

async function backupWorkspace() {
  const workspace = currentWorkspace();
  if (!workspace) return toast("请先选择工作区。", "error");
  try {
    await api("/api/chat/backup", {
      method: "POST",
      body: JSON.stringify({ workspace_id: workspace.id }),
    });
    toast("工作区会话已备份。");
    await loadBackups();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function backupAll() {
  try {
    await api("/api/chat/backup", { method: "POST", body: JSON.stringify({ all: true }) });
    toast("已备份全部工作区会话。");
    await loadBackups();
  } catch (error) {
    toast(error.message, "error");
  }
}

// 恢复最新：按当前工作区匹配最近一份备份后走统一恢复确认。
async function restoreLatest() {
  const workspace = currentWorkspace();
  if (!workspace) return toast("请先选择工作区。", "error");
  if (!backupState.items.length) await loadBackups();
  const backup = backupState.items.find(
    (item) => Number(item.workspace_count || 1) <= 1 && (
      item.workspace_id === workspace.id ||
      (!item.workspace_id && workspace.path && item.workspace === workspace.path)
    ),
  );
  if (!backup) return toast("没有匹配当前工作区的备份。", "error");
  await restoreBackup(backup.name, {
    workspaceId: backup.workspace_id || workspace.id,
    workspaceCount: Number(backup.workspace_count || 1),
  });
}

async function repairWorkspace() {
  const workspace = currentWorkspace();
  if (!workspace) return toast("请先选择工作区。", "error");
  try {
    await api("/api/chat/repair", {
      method: "POST",
      body: JSON.stringify({ workspace_id: workspace.id }),
    });
    toast("会话显示索引已修复。");
    await loadChats();
  } catch (error) {
    toast(error.message, "error");
  }
}

// 合并确认：显示源/目标名称与会话数量，并说明操作前自动备份。
async function mergeWorkspace() {
  const source = currentWorkspace();
  const target = sessionState.workspaces.find((w) => w.id === $("#merge-target").value);
  if (!source || !target || source.id === target.id) {
    return toast("请选择有效的合并目标。", "error");
  }
  const confirmed = await openConfirm({
    title: "合并工作区",
    danger: true,
    okLabel: "合并",
    bodyHtml: `
      <p>源工作区：<strong>${escapeHtml(source.name || source.id)}</strong>（${source.chat_count || 0} 条会话）</p>
      <p>目标工作区：<strong>${escapeHtml(target.name || target.id)}</strong>（${target.chat_count || 0} 条会话）</p>
      <p>合并后源工作区的会话将移动到目标工作区，源工作区对话会被清空。</p>
      <p>操作前会自动为源和目标各生成一份备份。</p>`,
  });
  if (!confirmed) return;
  try {
    const result = await api("/api/chat/merge", {
      method: "POST",
      body: JSON.stringify({ source_workspace_id: source.id, target_workspace_id: target.id }),
    });
    toast(`已合并 ${result.merged} 条会话到“${target.name || target.id}”。`);
    $("#merge-row").hidden = true;
    // 合并成功后跳到目标工作区，方便核对结果。
    await loadWorkspaces();
    await selectWorkspace(target.id);
    await loadBackups();
  } catch (error) {
    toast(error.message, "error");
  }
}

// 删除工作区：勾选确认框后才可执行；成功后选择下一个可用工作区。
async function deleteWorkspace() {
  const workspace = currentWorkspace();
  if (!workspace) return toast("请先选择工作区。", "error");
  const confirmed = await openConfirm({
    title: "删除工作区会话",
    danger: true,
    okLabel: "删除",
    requireAck: `我已了解：将删除“${workspace.name || workspace.id}”的 IDE 历史、Quest 任务和显示索引，操作前会自动备份。`,
    bodyHtml: `
      <p>删除范围：该工作区的全部 IDE 对话与 Quest 任务记录。</p>
      <p>操作前会自动生成一份备份，可在“备份”视图恢复。</p>`,
  });
  if (!confirmed) return;
  try {
    await api("/api/chat/workspace/delete", {
      method: "POST",
      body: JSON.stringify({ workspace_id: workspace.id }),
    });
    toast(`工作区“${workspace.name || workspace.id}”的会话已删除。`);
    sessionState.workspaceId = "";
    await loadWorkspaces();
    await loadChats();
    await loadBackups();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function deleteSelectedChats() {
  const workspace = currentWorkspace();
  if (!workspace || !sessionState.selectedIds.size) return;
  const ids = [...sessionState.selectedIds];
  const selected = sessionState.chats.filter((chat) => ids.includes(chatId(chat)));
  const ideCount = selected.filter((c) => c.kind !== "quest").length;
  const questCount = selected.filter((c) => c.kind === "quest").length;
  const confirmed = await openConfirm({
    title: "删除选中会话",
    danger: true,
    okLabel: "删除",
    bodyHtml: `
      <p>将从工作区 <strong>${escapeHtml(workspace.name || workspace.id)}</strong> 删除 ${ids.length} 条会话：</p>
      <p>IDE 对话 ${ideCount} 条，Quest 任务 ${questCount} 条。</p>
      <p>删除后不可在列表中恢复，建议先备份当前工作区。</p>`,
  });
  if (!confirmed) return;
  try {
    await api("/api/chat/delete", {
      method: "POST",
      body: JSON.stringify({ workspace_id: workspace.id, chat_ids: ids }),
    });
    sessionState.selectedIds.clear();
    toast("已删除选中的会话。");
    await refreshAllSessions();
  } catch (error) {
    // 失败保留选择，方便重试。
    toast(error.message, "error");
  }
}

async function deleteSingleChat(id) {
  const workspace = currentWorkspace();
  if (!workspace) return;
  const chat = sessionState.chats.find((c) => chatId(c) === id);
  const confirmed = await openConfirm({
    title: "删除会话",
    danger: true,
    okLabel: "删除",
    bodyHtml: `<p>确定删除“${escapeHtml(chat?.title || "无标题")}”吗？删除后不可在列表中恢复。</p>`,
  });
  if (!confirmed) return;
  try {
    await api("/api/chat/delete", {
      method: "POST",
      body: JSON.stringify({ workspace_id: workspace.id, chat_ids: [id] }),
    });
    sessionState.selectedIds.delete(id);
    toast("会话已删除。");
    await refreshAllSessions();
  } catch (error) {
    toast(error.message, "error");
  }
}

async function deleteBackup(name) {
  const confirmed = await openConfirm({
    title: "删除备份",
    danger: true,
    okLabel: "删除",
    bodyHtml: `<p>确定删除备份 <code>${escapeHtml(name)}</code> 吗？删除后无法恢复该份数据。</p>`,
  });
  if (!confirmed) return;
  try {
    await api("/api/chat/backup/delete", {
      method: "POST",
      body: JSON.stringify({ name }),
    });
    toast("备份已删除。");
    await loadBackups();
  } catch (error) {
    toast(error.message, "error");
  }
}

/* ============================== OAuth 添加账号 ============================== */

let oauthLoginId = "";
let oauthTimer = null;
let oauthVerificationUri = "";

async function startOauth() {
  if (oauthTimer) clearInterval(oauthTimer);
  $("#oauth-dialog").showModal();
  $("#oauth-status").textContent = "正在创建授权会话…";
  try {
    const result = await api("/api/oauth/start", { method: "POST", body: "{}" });
    oauthLoginId = result.login_id;
    oauthVerificationUri = result.verification_uri;
    $("#oauth-url").textContent = oauthVerificationUri;
    $("#oauth-status").textContent = "请在授权页完成登录；完成后此处会自动更新。";
    window.open(oauthVerificationUri, "_blank", "noopener");
    // 授权页完成登录后，后端会把新账号写入 SQLite，页面只需轮询完成状态并刷新列表。
    oauthTimer = setInterval(async () => {
      try {
        const status = await api(`/api/oauth/status?login_id=${encodeURIComponent(oauthLoginId)}`);
        if (status.status === "completed") {
          clearInterval(oauthTimer);
          $("#oauth-status").textContent = `已添加：${status.account.email}`;
          toast("新账号授权完成并已加入列表。");
          await loadCore();
          setTimeout(() => $("#oauth-dialog").close(), 1000);
        } else if (status.status === "error") {
          clearInterval(oauthTimer);
          $("#oauth-status").textContent = status.error;
        }
      } catch (error) {
        clearInterval(oauthTimer);
        $("#oauth-status").textContent = error.message;
      }
    }, 1500);
  } catch (error) {
    $("#oauth-status").textContent = error.message;
  }
}

async function cancelOauth() {
  if (oauthTimer) clearInterval(oauthTimer);
  if (oauthLoginId)
    await api("/api/oauth/cancel", {
      method: "POST",
      body: JSON.stringify({ login_id: oauthLoginId }),
    }).catch(() => {});
  oauthLoginId = "";
  oauthVerificationUri = "";
}

/* ============================== 事件绑定 ============================== */

function bindGlobalEvents() {
  window.addEventListener("hashchange", applyRoute);

  $("#theme-toggle").addEventListener("click", () => {
    const next = document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    localStorage.setItem("cockpit-theme", next);
  });

  // 账号视图工具栏
  $("#refresh-credits-btn").addEventListener("click", (event) =>
    busy(event.currentTarget, refreshCredits, "同步中…"),
  );
  $("#import-local-btn").addEventListener("click", (event) =>
    busy(event.currentTarget, importLocal, "导入中…"),
  );
  $("#add-account-btn").addEventListener("click", startOauth);
  $("#oauth-cancel").addEventListener("click", cancelOauth);
  $("#oauth-open").addEventListener("click", (event) => {
    event.preventDefault();
    if (oauthVerificationUri) window.open(oauthVerificationUri, "_blank", "noopener");
  });

  // 会话视图工具栏
  $("#refresh-chats-btn").addEventListener("click", (event) =>
    busy(event.currentTarget, loadSessions, "刷新中…"),
  );
  $("#session-help-btn").addEventListener("click", () => {
    const note = $("#session-help");
    note.hidden = !note.hidden;
  });
  $$("[data-kind]").forEach((button) =>
    button.addEventListener("click", () => {
      sessionState.kind = button.dataset.kind;
      $$("[data-kind]").forEach((item) => item.classList.toggle("is-active", item === button));
      renderSessionTable();
    }),
  );
  // 搜索输入 200ms 防抖，避免每个字符都触发整表重绘。
  let searchTimer = null;
  $("#session-search").addEventListener("input", (event) => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => {
      sessionState.query = event.target.value.trim();
      renderSessionTable();
    }, 200);
  });
  $("#backup-chat-btn").addEventListener("click", (event) =>
    busy(event.currentTarget, backupWorkspace, "备份中…"),
  );
  $("#restore-latest-btn").addEventListener("click", (event) =>
    busy(event.currentTarget, restoreLatest, "查找中…"),
  );

  // 工作区选择：桌面侧栏与移动端下拉共用同一状态。
  $("#workspace-list").addEventListener("click", (event) => {
    const item = event.target.closest("[data-workspace-id]");
    if (item) selectWorkspace(item.dataset.workspaceId);
  });
  $("#workspace-select-mobile").addEventListener("change", (event) =>
    selectWorkspace(event.target.value),
  );

  // 会话表格：勾选、打开详情、行内更多菜单均用事件委托。
  const table = $("#session-table");
  table.addEventListener("change", (event) => {
    const id = event.target.dataset.chatCheck;
    if (id) {
      event.target.checked ? sessionState.selectedIds.add(id) : sessionState.selectedIds.delete(id);
      renderSessionTable();
    }
  });
  $("#session-select-all").addEventListener("change", (event) => {
    filteredChats().forEach((chat) =>
      event.target.checked ? sessionState.selectedIds.add(chatId(chat)) : sessionState.selectedIds.delete(chatId(chat)),
    );
    renderSessionTable();
  });
  table.addEventListener("click", (event) => {
    const open = event.target.closest("[data-open-chat]");
    if (open) {
      const chat = sessionState.chats.find((c) => chatId(c) === open.dataset.openChat);
      if (chat) openDrawer(chat);
      return;
    }
    const menuBtn = event.target.closest("[data-row-menu]");
    if (menuBtn) {
      const wrap = menuBtn.closest(".row-menu-wrap");
      const alreadyOpen = wrap.querySelector(".row-menu");
      closeRowMenus();
      if (!alreadyOpen) {
        const menu = document.createElement("div");
        menu.className = "row-menu";
        menu.setAttribute("role", "menu");
        menu.innerHTML = `<button type="button" class="menu-danger" role="menuitem"><i data-lucide="trash-2" aria-hidden="true"></i>删除此会话</button>`;
        menu.querySelector("button").addEventListener("click", () => {
          closeRowMenus();
          deleteSingleChat(menuBtn.dataset.rowMenu);
        });
        wrap.append(menu);
        renderIcons();
      }
    }
  });

  // 批量操作栏
  $("#bulk-delete-btn").addEventListener("click", deleteSelectedChats);
  $("#bulk-clear-btn").addEventListener("click", () => {
    sessionState.selectedIds.clear();
    renderSessionTable();
  });

  // 危险操作区
  $("#repair-chat-btn").addEventListener("click", (event) =>
    busy(event.currentTarget, repairWorkspace, "修复中…"),
  );
  $("#merge-chat-btn").addEventListener("click", () => {
    const row = $("#merge-row");
    row.hidden = !row.hidden;
    if (!row.hidden) renderMergeRow();
  });
  $("#confirm-merge-btn").addEventListener("click", mergeWorkspace);
  $("#delete-workspace-btn").addEventListener("click", deleteWorkspace);

  // 备份视图
  $("#refresh-backups-btn").addEventListener("click", (event) =>
    busy(event.currentTarget, loadBackups, "刷新中…"),
  );
  $("#backup-all-btn").addEventListener("click", (event) =>
    busy(event.currentTarget, backupAll, "备份中…"),
  );
  $("#backup-table").addEventListener("click", (event) => {
    const restore = event.target.closest("[data-backup-restore]");
    if (restore) {
      restoreBackup(restore.dataset.backupRestore, {
        workspaceId: restore.dataset.backupWorkspaceId,
        workspaceCount: Number(restore.dataset.backupWorkspaceCount || 1),
      });
      return;
    }
    const menuBtn = event.target.closest("[data-backup-menu]");
    if (menuBtn) {
      const wrap = menuBtn.closest(".row-menu-wrap");
      const alreadyOpen = wrap.querySelector(".row-menu");
      closeRowMenus();
      if (!alreadyOpen) {
        const menu = document.createElement("div");
        menu.className = "row-menu";
        menu.setAttribute("role", "menu");
        menu.innerHTML = `<button type="button" class="menu-danger" role="menuitem"><i data-lucide="trash-2" aria-hidden="true"></i>删除备份</button>`;
        menu.querySelector("button").addEventListener("click", () => {
          closeRowMenus();
          deleteBackup(menuBtn.dataset.backupMenu);
        });
        wrap.append(menu);
        renderIcons();
      }
    }
  });

  // 抽屉关闭：遮罩、按钮与 Escape 三种方式。
  $("#drawer-close").addEventListener("click", closeDrawer);
  $$("[data-drawer-close]").forEach((node) => node.addEventListener("click", closeDrawer));
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !$("#session-drawer").hidden) closeDrawer();
  });
}

/* ============================== 初始化 ============================== */

renderIcons();
applyRoute();
bindGlobalEvents();
loadCore();
loadSessions();
loadBackups();
// 后台每 15 秒静默刷新运行状态与账号额度，不重绘会话列表避免打断选择。
setInterval(loadCore, 15000);
