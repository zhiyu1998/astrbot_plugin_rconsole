const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const $ = (sel) => document.querySelector(sel);

const state = {
  groups: [],
  dirty: new Set(),
  original: new Map(),
};

/* ---------------------------------------------------------------- 工具 */

function toast(message, isError = false) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.toggle("error", isError);
  el.classList.add("show");
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => el.classList.remove("show"), 2600);
}

function escapeHtml(text) {
  return String(text ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/* ---------------------------------------------------------------- 渲染 */

function renderField(field) {
  const wrap = document.createElement("div");
  wrap.className = "field";
  wrap.dataset.key = field.key;
  wrap.dataset.search = [field.key, field.label, field.hint, field.group]
    .join(" ")
    .toLowerCase();

  const labelBox = document.createElement("div");
  labelBox.className = "field-label";
  labelBox.innerHTML = `
    <span class="k">${escapeHtml(field.label)}</span>
    <span class="key">${escapeHtml(field.key)}</span>
    ${field.hint ? `<span class="hint">${escapeHtml(field.hint)}</span>` : ""}
  `;

  const control = document.createElement("div");
  control.className = "field-control";

  if (field.type === "bool") {
    const on = field.value === true;
    control.innerHTML = `
      <label class="switch">
        <input type="checkbox" ${on ? "checked" : ""}>
        <span class="track"></span>
        <span class="state">${on ? "已开启" : "已关闭"}</span>
      </label>
    `;
    const input = control.querySelector("input");
    input.addEventListener("change", () => {
      control.querySelector(".state").textContent = input.checked ? "已开启" : "已关闭";
      markDirty(field.key);
    });
  } else if (field.type === "int" || field.type === "float") {
    const input = document.createElement("input");
    input.type = "number";
    input.step = field.type === "float" ? "0.1" : "1";
    input.value = field.value ?? field.default ?? 0;
    input.addEventListener("input", () => markDirty(field.key));
    control.appendChild(input);
  } else if (field.type === "list") {
    const area = document.createElement("textarea");
    area.value = Array.isArray(field.value) ? field.value.join("\n") : field.value || "";
    area.placeholder = "一行一个";
    area.addEventListener("input", () => markDirty(field.key));
    control.appendChild(area);
  } else if (field.options && field.options.length) {
    const select = document.createElement("select");
    field.options.forEach((option) => {
      const item = document.createElement("option");
      item.value = option;
      item.textContent = option;
      if (String(field.value) === String(option)) item.selected = true;
      select.appendChild(item);
    });
    select.addEventListener("change", () => markDirty(field.key));
    control.appendChild(select);
  } else if (field.is_secret) {
    const row = document.createElement("div");
    row.className = "secret-row";
    const input = document.createElement("input");
    input.type = "password";
    input.autocomplete = "new-password";
    input.placeholder = field.has_value ? "已设置，留空则不修改" : "未设置，粘贴到这里";
    input.addEventListener("input", () => markDirty(field.key));
    row.appendChild(input);
    if (field.has_value) {
      const badge = document.createElement("span");
      badge.className = "badge-set";
      badge.textContent = "已配置";
      row.appendChild(badge);
    }
    control.appendChild(row);
  } else {
    const input = document.createElement("input");
    input.type = "text";
    input.value = field.value ?? "";
    input.placeholder = field.default ?? "";
    input.addEventListener("input", () => markDirty(field.key));
    control.appendChild(input);
  }

  wrap.appendChild(labelBox);
  wrap.appendChild(control);
  return wrap;
}

function renderGroups(groups) {
  const host = $("#groups");
  host.innerHTML = "";
  let total = 0;

  groups.forEach((group) => {
    const card = document.createElement("section");
    card.className = "group";

    const head = document.createElement("div");
    head.className = "group-head";
    head.innerHTML = `
      <span class="dot"></span>
      <h2>${escapeHtml(group.name)}</h2>
      <span class="gcount">${group.fields.length} 项</span>
    `;
    head.addEventListener("click", () => card.classList.toggle("collapsed"));

    const body = document.createElement("div");
    body.className = "group-body";
    group.fields.forEach((field) => {
      body.appendChild(renderField(field));
      total += 1;
    });

    card.appendChild(head);
    card.appendChild(body);
    host.appendChild(card);
  });

  $("#fieldCount").textContent = `共 ${total} 项`;
}

function markDirty(key) {
  state.dirty.add(key);
  $("#saveHint").textContent = `已修改 ${state.dirty.size} 项，未保存`;
}

function collectValues() {
  const values = {};
  document.querySelectorAll(".field").forEach((fieldEl) => {
    const key = fieldEl.dataset.key;
    const control = fieldEl.querySelector(".field-control");
    const boolInput = control.querySelector('.switch input[type="checkbox"]');
    if (boolInput) {
      values[key] = boolInput.checked;
      return;
    }
    const textarea = control.querySelector("textarea");
    if (textarea) {
      values[key] = textarea.value
        .split("\n")
        .map((line) => line.trim())
        .filter(Boolean);
      return;
    }
    const input = control.querySelector("input, select");
    if (!input) return;
    if (input.type === "password") {
      if (input.value.trim() !== "") values[key] = input.value.trim();
      return;
    }
    values[key] = input.value;
  });
  return values;
}

/* ---------------------------------------------------------------- 数据 */

async function loadSchema() {
  const data = await bridge.apiGet("view/schema");
  state.groups = data.groups || [];
  if (data.version) $("#versionTag").textContent = `v${data.version}`;
  renderGroups(state.groups);
  $("#saveHint").textContent = "";
  state.dirty.clear();
}

function chip(text, kind) {
  const span = document.createElement("span");
  span.className = `chip ${kind || ""}`.trim();
  span.textContent = text;
  return span;
}

async function loadStatus(probeProxy = false) {
  const grid = $("#statusGrid");
  grid.innerHTML = '<div class="skeleton">正在读取状态…</div>';
  const data = await bridge.apiGet("view/status", probeProxy ? { probe: 1 } : {});

  const chips = $("#heroChips");
  chips.innerHTML = "";
  const proxy = data.proxy || {};
  chips.appendChild(
    chip(
      proxy.url ? (proxy.ok === true ? "代理可用" : proxy.ok === false ? "代理异常" : "已配代理") : "直连",
      proxy.url ? (proxy.ok === false ? "warn" : "ok") : ""
    )
  );
  const temp = data.temp || {};
  chips.appendChild(chip(temp.writable ? "暂存目录可写" : "暂存目录异常", temp.writable ? "ok" : "warn"));
  chips.appendChild(chip(`并发 ${data.download?.concurrency ?? "-"}`, ""));

  const cards = [];

  cards.push(`
    <div class="card">
      <h3>基础信息</h3>
      <div class="kv"><span>插件版本</span><code>${escapeHtml(data.version || "-")}</code></div>
      <div class="kv"><span>配置文件</span><code>${escapeHtml(data.config_path || "-")}</code></div>
      <div class="kv"><span>下载队列</span><span>${data.download?.concurrency ?? "-"} 任务</span></div>
      <div class="kv"><span>单文件分段</span><span>${data.download?.threads ?? "-"} 线程</span></div>
      <div class="kv"><span>aria2 下载</span><span class="${data.download?.aria2 ? "ok-text" : ""}">${data.download?.aria2 ? "已启用" : "未启用"}</span></div>
    </div>
  `);

  const tools = data.tools || {};
  const toolRow = (label, value, fallback) => {
    const ok = Boolean(value);
    return `<div class="kv"><span>${label}</span><span class="${ok ? "ok-text" : "bad-text"}">${escapeHtml(ok ? value : fallback)}</span></div>`;
  };
  cards.push(`
    <div class="card">
      <h3>依赖组件</h3>
      ${toolRow("ffmpeg", tools.ffmpeg, "未安装")}
      ${toolRow("aria2c", tools.aria2c, "未安装")}
      ${toolRow("BBDown", tools.bbdown, "未安装")}
      ${toolRow("yt-dlp", tools.yt_dlp, "未安装")}
    </div>
  `);

  cards.push(`
    <div class="card">
      <h3>代理</h3>
      <div class="kv"><span>地址</span><code>${escapeHtml(proxy.url || "（直连）")}</code></div>
      <div class="kv"><span>自检</span><span class="${
        proxy.ok === true ? "ok-text" : proxy.ok === false ? "bad-text" : ""
      }">${
        proxy.ok === true ? "可达 YouTube" : proxy.ok === false ? "连接失败" : "未检测"
      }</span></div>
      ${proxy.error ? `<div class="kv"><span>错误</span><code>${escapeHtml(proxy.error)}</code></div>` : ""}
    </div>
  `);

  cards.push(`
    <div class="card">
      <h3>暂存目录</h3>
      <div class="kv"><span>路径</span><code>${escapeHtml(temp.path || "-")}</code></div>
      <div class="kv"><span>可写</span><span class="${temp.writable ? "ok-text" : "bad-text"}">${temp.writable ? "是" : "否"}</span></div>
      <div class="kv"><span>剩余空间</span><span>${temp.free_gb ?? "-"} GB</span></div>
      <div class="kv"><span>已占用</span><span>${temp.used_mb ?? "-"} MB</span></div>
    </div>
  `);

  const platforms = data.platforms || [];
  cards.push(`
    <div class="card">
      <h3>平台开关</h3>
      <div style="display:flex;flex-wrap:wrap;gap:6px">
        ${platforms
          .map(
            (item) =>
              `<span class="pill ${item.enabled ? "on" : "off"}">${escapeHtml(item.label)}</span>`
          )
          .join("")}
      </div>
    </div>
  `);

  const secrets = data.secrets || {};
  const secretNames = {
    bili_sessdata: "B站 SESSDATA",
    douyin_ck: "抖音 Cookie",
    weibo_ck: "微博 Cookie",
    xhs_ck: "小红书 Cookie",
    xhh_ck: "小黑盒 Cookie",
    wxchannel_cookie: "视频号元宝 Cookie",
    youtube_cookies: "YouTube cookies.txt",
  };
  cards.push(`
    <div class="card">
      <h3>凭据状态</h3>
      ${Object.entries(secretNames)
        .map(
          ([key, label]) =>
            `<div class="kv"><span>${label}</span><span class="${secrets[key] ? "ok-text" : "bad-text"}">${secrets[key] ? "已配置" : "未配置"}</span></div>`
        )
        .join("")}
    </div>
  `);

  grid.innerHTML = cards.join("");
}

/* ---------------------------------------------------------------- 交互 */

function bind() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((item) => item.classList.remove("active"));
      tab.classList.add("active");
      const name = tab.dataset.tab;
      $("#tab-settings").classList.toggle("hidden", name !== "settings");
      $("#tab-status").classList.toggle("hidden", name !== "status");
      if (name === "status") loadStatus().catch((error) => toast(error.message, true));
    });
  });

  $("#search").addEventListener("input", (event) => {
    const keyword = event.target.value.trim().toLowerCase();
    document.querySelectorAll(".group").forEach((group) => {
      let visible = 0;
      group.querySelectorAll(".field").forEach((field) => {
        const hit = !keyword || field.dataset.search.includes(keyword);
        field.classList.toggle("hidden", !hit);
        if (hit) visible += 1;
      });
      group.classList.toggle("hidden", visible === 0);
      const counter = group.querySelector(".gcount");
      if (counter) counter.textContent = `${visible} 项`;
    });
  });

  $("#saveBtn").addEventListener("click", async () => {
    const button = $("#saveBtn");
    button.disabled = true;
    button.textContent = "保存中…";
    try {
      const values = collectValues();
      const result = await bridge.apiPost("view/save", { values });
      const saved = result.saved || [];
      const applied = result.applied || [];
      toast(`已保存 ${saved.length} 项${applied.length ? `，已热应用 ${applied.length} 项` : ""}`);
      $("#saveHint").textContent = "";
      state.dirty.clear();
      await loadSchema();
      loadStatus().catch(() => {});
    } catch (error) {
      toast(error.message || "保存失败", true);
    } finally {
      button.disabled = false;
      button.textContent = "保存并生效";
    }
  });

  $("#resetBtn").addEventListener("click", async () => {
    await loadSchema();
    toast("已还原为当前已保存的配置");
  });

  $("#refreshStatus").addEventListener("click", () => {
    loadStatus(true).catch((error) => toast(error.message, true));
  });

  document.querySelectorAll("[data-probe]").forEach((button) => {
    button.addEventListener("click", async () => {
      const target = button.dataset.probe;
      button.disabled = true;
      const old = button.textContent;
      button.textContent = "检测中…";
      try {
        const result = await bridge.apiPost("view/probe", { target });
        toast(result.message || (result.ok ? "通过" : "失败"), result.ok === false);
        await loadStatus();
      } catch (error) {
        toast(error.message || "检测失败", true);
      } finally {
        button.disabled = false;
        button.textContent = old;
      }
    });
  });
}

/* ---------------------------------------------------------------- 启动 */

(async function main() {
  if (!bridge) {
    document.body.innerHTML =
      '<div class="skeleton">bridge 未注入，请在 AstrBot WebUI 中打开本视图。</div>';
    return;
  }
  await bridge.ready();
  bind();
  try {
    await loadSchema();
  } catch (error) {
    $("#groups").innerHTML = `<div class="skeleton">加载失败：${escapeHtml(error.message)}</div>`;
    return;
  }
  loadStatus().catch(() => {});
})();
