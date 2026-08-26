const $ = (selector) => document.querySelector(selector);
const state = { accounts: [], projects: [], current: null, config: null };

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

function quotaUpdatedLabel(account) {
  const value = account.quota_refreshed_at || account.updated_at;
  return value
    ? new Date(value).toLocaleTimeString([], {
        hour: "2-digit",
        minute: "2-digit",
      })
    : "—";
}

function setNotice(message, tone = "") {
  const node = $("#operation-notice");
  node.textContent = message;
  node.className = `notice ${tone}`;
}

function renderIcons() {
  window.lucide?.createIcons({ attrs: { "stroke-width": 1.8 } });
}

function initPageTabs() {
  const tabs = [...document.querySelectorAll(".page-tab")];
  const sections = tabs
    .map((tab) => document.querySelector(tab.getAttribute("href")))
    .filter(Boolean);
  let scheduled = false;
  // 滚动经过总览、账号和会话区块时，顶部 Tab 始终标识当前阅读位置。
  const update = () => {
    const marker = window.scrollY + (window.innerWidth <= 700 ? 150 : 110);
    let active = sections[0];
    sections.forEach((section) => {
      if (section.offsetTop <= marker) active = section;
    });
    tabs.forEach((tab) =>
      tab.classList.toggle(
        "active",
        tab.getAttribute("href") === `#${active.id}`,
      ),
    );
    scheduled = false;
  };
  window.addEventListener(
    "scroll",
    () => {
      if (!scheduled) {
        scheduled = true;
        requestAnimationFrame(update);
      }
    },
    { passive: true },
  );
  tabs.forEach((tab) =>
    tab.addEventListener("click", () =>
      tabs.forEach((item) => item.classList.toggle("active", item === tab)),
    ),
  );
  update();
}

function accountLabel(account) {
  return account.display_name
    ? `${account.display_name} · ${account.email}`
    : account.email;
}

function renderAccounts() {
  $("#account-count").textContent = state.accounts.length;
  $("#current-account").textContent = state.current?.email || "未识别";
  $("#current-plan").textContent = state.current?.plan || "没有读取到套餐信息";
  const list = $("#account-list");
  if (!state.accounts.length) {
    list.innerHTML =
      '<div class="empty">还没有保存的账号。点击“添加账号”完成官方授权。</div>';
  } else {
    // 每张卡片完整承载账号身份、额度和可执行操作，切号时无需再去会话区选择目标账号。
    list.innerHTML = state.accounts
      .map((account) => {
        const current =
          state.current?.id === account.id ||
          state.current?.email === account.email;
        const percent = quotaPercent(account);
        return `<article class="account-card ${current ? "current" : ""}">
          <header class="account-card-head">
            <div class="account-identity"><div class="account-email">${escapeHtml(accountLabel(account))}</div><div class="account-id">${escapeHtml(account.id)}</div></div>
            ${current ? '<span class="current-badge"><i data-lucide="circle-check"></i>当前账号</span>' : ""}
          </header>
          <div class="account-meta"><span class="plan-pill">${escapeHtml(account.plan || "套餐未识别")}</span><span>额度同步 ${escapeHtml(quotaUpdatedLabel(account))}</span>${account.quota_error ? `<span class="quota-error">同步失败</span>` : ""}</div>
          <div class="quota"><div class="quota-head"><strong>${percent === null ? "—" : `${percent.toFixed(1)}%`}</strong><span>剩余</span></div><div class="quota-track" aria-label="剩余额度 ${percent === null ? "未知" : `${percent.toFixed(1)}%`}"><span style="width:${percent === null ? 0 : percent}%"></span></div><div class="quota-stats"><span>已用 <b>${escapeHtml(money(account.credits_used))}</b></span><span>总量 <b>${escapeHtml(money(account.credits_total))}</b></span></div></div>
          <footer class="account-actions">${current ? "" : `<button class="button primary account-switch" data-switch-account="${escapeHtml(account.id)}"><i data-lucide="repeat-2"></i><span class="button-label">切换账号</span></button>`}<button class="mini-action danger icon-only" data-delete="${escapeHtml(account.id)}" aria-label="删除账号" title="删除账号"><i data-lucide="trash-2"></i></button></footer>
      </article>`;
      })
      .join("");
  }
  const options = state.accounts
    .map(
      (account) =>
        `<option value="${escapeHtml(account.id)}">${escapeHtml(accountLabel(account))}</option>`,
    )
    .join("");
  $("#source-account").innerHTML =
    `<option value="">不保存上一个账号</option>${options}`;
  if (state.current) $("#source-account").value = state.current.id || "";
  list.querySelectorAll("[data-switch-account]").forEach((button) =>
    button.addEventListener("click", () =>
      switchAccount(button.dataset.switchAccount, button),
    ),
  );
  list
    .querySelectorAll("[data-delete]")
    .forEach((button) =>
      button.addEventListener("click", () =>
        deleteAccount(button.dataset.delete),
      ),
    );
  renderIcons();
}

function renderProjects() {
  $("#project-count").textContent = state.projects.length;
  const table = $("#project-table");
  table.innerHTML = state.projects.length
    ? state.projects
        .map(
          (project) => `<tr>
    <td class="project-name">${escapeHtml(project.name)}</td><td class="project-count">${project.transcript_count}</td><td class="project-count">${project.execution_count}</td><td class="path-cell">${escapeHtml(project.path)}</td>
    <td><button class="mini-action action-with-icon" data-save-project="${escapeHtml(project.path)}"><i data-lucide="archive"></i><span>保存快照</span></button></td>
  </tr>`,
        )
        .join("")
    : '<tr><td colspan="5" class="empty">没有发现 Qoder 项目目录。</td></tr>';
  table
    .querySelectorAll("[data-save-project]")
    .forEach((button) =>
      button.addEventListener("click", () =>
        saveProject(button.dataset.saveProject),
      ),
    );
  renderIcons();
}

function renderStatus() {
  const running = state.config?.qoder_running;
  $("#qoder-state").textContent = running ? "运行中" : "已关闭";
  $("#sidebar-status").textContent = running
    ? "Qoder 正在运行"
    : "Qoder 已关闭，可切换";
  $("#state-db-path").textContent =
    state.config?.state_db || "未找到 state.vscdb";
  $("#sidebar-dot").className = `status-dot ${running ? "" : "ok"}`;
  $("#detail-dot").className = `status-dot ${running ? "" : "ok"}`;
  $("#detail-status").textContent = running
    ? "切号时会自动关闭"
    : "可以执行切换";
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

async function refresh() {
  try {
    const [config, accounts, projects] = await Promise.all([
      api("/api/config"),
      api("/api/accounts"),
      api("/api/projects"),
    ]);
    Object.assign(state, {
      config,
      accounts: accounts.accounts,
      current: accounts.current,
      projects: projects.projects,
    });
    renderStatus();
    renderAccounts();
    renderProjects();
    setNotice(
      config.qoder_running
        ? "Qoder 正在运行；切号时会自动关闭并重新启动。"
        : "准备就绪。可以执行账号切换与会话继承。",
      config.qoder_running ? "" : "success",
    );
  } catch (error) {
    setNotice(error.message, "error");
  }
}

async function importLocal() {
  try {
    await api("/api/accounts/import-local", { method: "POST", body: "{}" });
    setNotice("已导入当前 Qoder 账号。", "success");
    await refresh();
  } catch (error) {
    setNotice(error.message, "error");
  }
}
async function refreshCredits() {
  const button = $("#refresh-credits-btn");
  const label = button.querySelector(".button-label");
  button.disabled = true;
  label.textContent = "同步中…";
  try {
    const result = await api("/api/accounts/refresh", {
      method: "POST",
      body: "{}",
    });
    setNotice(
      `额度已更新：${result.updated} 个账号${result.errors?.length ? `，${result.errors.length} 个账号需要重新授权` : ""}。`,
      result.errors?.length ? "error" : "success",
    );
    await refresh();
  } catch (error) {
    setNotice(error.message, "error");
  } finally {
    button.disabled = false;
    label.textContent = "刷新额度";
  }
}

async function startQoder() {
  try {
    const result = await api("/api/qoder/start", {
      method: "POST",
      body: "{}",
    });
    setNotice(result.message || `Qoder 已启动：${result.path}`, "success");
    setTimeout(refresh, 1200);
  } catch (error) {
    setNotice(error.message, "error");
  }
}

let oauthLoginId = "";
let oauthTimer = null;
let oauthVerificationUri = "";
async function startOauth() {
  if (oauthTimer) clearInterval(oauthTimer);
  $("#oauth-dialog").showModal();
  $("#oauth-status").textContent = "正在创建授权会话…";
  try {
    const result = await api("/api/oauth/start", {
      method: "POST",
      body: "{}",
    });
    oauthLoginId = result.login_id;
    oauthVerificationUri = result.verification_uri;
    $("#oauth-url").textContent = oauthVerificationUri;
    $("#oauth-status").textContent =
      "请在授权页完成登录；完成后此处会自动更新。";
    window.open(oauthVerificationUri, "_blank", "noopener");
    // 授权页完成登录后，后端会把新账号写入 SQLite，页面只需等待完成状态并刷新列表。
    oauthTimer = setInterval(async () => {
      try {
        const status = await api(
          `/api/oauth/status?login_id=${encodeURIComponent(oauthLoginId)}`,
        );
        if (status.status === "completed") {
          clearInterval(oauthTimer);
          $("#oauth-status").textContent = `已添加：${status.account.email}`;
          setNotice("新账号授权完成并已加入列表。", "success");
          await refresh();
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

async function deleteAccount(id) {
  if (!confirm("删除本地账号记录？不会删除 Qoder 原始登录数据。")) return;
  try {
    await api("/api/accounts/delete", {
      method: "POST",
      body: JSON.stringify({ account_id: id }),
    });
    await refresh();
  } catch (error) {
    setNotice(error.message, "error");
  }
}

async function saveProject(path) {
  const account = $("#source-account").value || state.current?.id;
  if (!account) return setNotice("请先选择要保存的账号。", "error");
  try {
    await api("/api/session/save", {
      method: "POST",
      body: JSON.stringify({ account_id: account, project_path: path }),
    });
    setNotice("项目会话快照已保存。", "success");
  } catch (error) {
    setNotice(error.message, "error");
  }
}

async function switchAccount(target, button) {
  const source = state.current?.id || $("#source-account").value;
  if (!target) return setNotice("没有识别到目标账号。", "error");
  if (source && source === target)
    return setNotice("当前已经是这个账号。", "error");
  const label = button.querySelector(".button-label");
  const idleLabel = label.textContent;
  button.disabled = true;
  label.textContent = "处理中…";
  try {
    // 卡片确定目标账号，当前登录身份作为会话来源；后端仍负责保存、注入、恢复和重启 Qoder。
    const result = await api("/api/switch", {
      method: "POST",
      body: JSON.stringify({
        source_account_id: source,
        target_account_id: target,
        save_previous: Boolean(source),
        inherit_sessions: $("#inherit-sessions").checked,
      }),
    });
    setNotice(
      `切换完成，Qoder 已自动重启到目标账号；保存 ${result.saved.length} 个项目，恢复 ${result.restored.length} 个项目。`,
      "success",
    );
    await refresh();
  } catch (error) {
    setNotice(error.message, "error");
  } finally {
    button.disabled = false;
    label.textContent = idleLabel;
  }
}

$("#refresh-btn").addEventListener("click", refresh);
$("#refresh-credits-btn").addEventListener("click", refreshCredits);
$("#start-qoder-btn").addEventListener("click", startQoder);
$("#import-local-btn").addEventListener("click", importLocal);
$("#add-account-btn").addEventListener("click", startOauth);
$("#oauth-cancel").addEventListener("click", cancelOauth);
$("#oauth-open").addEventListener("click", (event) => {
  event.preventDefault();
  if (oauthVerificationUri)
    window.open(oauthVerificationUri, "_blank", "noopener");
});
renderIcons();
initPageTabs();
refresh();
setInterval(refresh, 15000);
