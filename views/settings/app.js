const bridge = window.AstrBotPluginView || window.AstrBotPluginPage;
const $ = (sel) => document.querySelector(sel);
const NL = String.fromCharCode(10);

/* ---------------------------------------------------------------- 统一线性 SVG 图标 */

const SVG_ICONS = {
  sliders: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><line x1="4" x2="4" y1="21" y2="14"/><line x1="4" x2="4" y1="10" y2="3"/><line x1="12" x2="12" y1="21" y2="12"/><line x1="12" x2="12" y1="8" y2="3"/><line x1="20" x2="20" y1="21" y2="16"/><line x1="20" x2="20" y1="12" y2="3"/><line x1="2" x2="6" y1="14" y2="14"/><line x1="10" x2="14" y1="8" y2="8"/><line x1="18" x2="22" y1="16" y2="16"/></svg>`,
  tv: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect width="20" height="15" x="2" y="7" rx="2" ry="2"/><polyline points="17 2 12 7 7 2"/></svg>`,
  music: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>`,
  zap: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>`,
  flame: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.072-2.143-.224-4.054 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.153.433-2.294 1-3a2.5 2.5 0 0 0 2.5 2.5z"/></svg>`,
  book: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5v-15A2.5 2.5 0 0 1 6.5 2H20v20H6.5a2.5 2.5 0 0 1 0-5H20"/></svg>`,
  gamepad: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><line x1="6" x2="10" y1="12" y2="12"/><line x1="8" x2="8" y1="10" y2="14"/><line x1="15" x2="15.01" y1="13" y2="13"/><line x1="18" x2="18.01" y1="11" y2="11"/><rect width="20" height="12" x="2" y="6" rx="2"/></svg>`,
  film: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect width="18" height="18" x="3" y="3" rx="2"/><path d="M7 3v18M17 3v18M3 7.5h4M3 12h18M3 16.5h4M17 7.5h4M17 16.5h4"/></svg>`,
  play: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polygon points="10 8 16 12 10 16 10 8"/></svg>`,
  video: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m22 8-6 4 6 4V8Z"/><rect width="14" height="12" x="2" y="6" rx="2" ry="2"/></svg>`,
  globe: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M2 12h20M12 2a15.3 15.3 0 0 1 4 10 15.3 15.3 0 0 1-4 10 15.3 15.3 0 0 1-4-10 15.3 15.3 0 0 1 4-10z"/></svg>`,
  download: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" x2="12" y1="15" y2="3"/></svg>`,
  folder: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M20 20a2 2 0 0 0 2-2V8a2 2 0 0 0-2-2h-7.9a2 2 0 0 1-1.69-.9L9.6 3.9A2 2 0 0 0 7.93 3H4a2 2 0 0 0-2 2v13a2 2 0 0 0 2 2Z"/></svg>`,
  layers: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/></svg>`,
  sparkles: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="m12 3-1.912 5.813a2 2 0 0 1-1.275 1.275L3 12l5.813 1.912a2 2 0 0 1 1.275 1.275L12 21l1.912-5.813a2 2 0 0 1 1.275-1.275L21 12l-5.813-1.912a2 2 0 0 1-1.275-1.275L12 3Z"/></svg>`,
  search: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>`,
  palette: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="13.5" cy="6.5" r=".5" fill="currentColor"/><circle cx="17.5" cy="10.5" r=".5" fill="currentColor"/><circle cx="8.5" cy="7.5" r=".5" fill="currentColor"/><circle cx="6.5" cy="12.5" r=".5" fill="currentColor"/><path d="M12 2C6.5 2 2 6.5 2 12s4.5 10 10 10c.926 0 1.648-.746 1.648-1.688 0-.437-.18-.835-.437-1.125-.29-.289-.438-.652-.438-1.125a1.64 1.64 0 0 1 1.668-1.668h1.996c3.051 0 5.555-2.503 5.555-5.554C21.965 6.012 17.461 2 12 2z"/></svg>`,
  edit: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 20h9"/><path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4L16.5 3.5z"/></svg>`,
  wrench: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/></svg>`,
  key: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><circle cx="7.5" cy="15.5" r="5.5"/><path d="m21 2-9.6 9.6"/><path d="m15.5 7.5 3 3L22 7l-3-3"/></svg>`,
  cpu: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect width="16" height="16" x="4" y="4" rx="2"/><rect width="6" height="6" x="9" y="9" rx="1"/><path d="M15 2v2M15 20v2M2 15h2M2 9h2M20 15h2M20 9h2M9 2v2M9 20v2"/></svg>`,
  eye: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/></svg>`,
  eyeOff: `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M9.88 9.88a3 3 0 1 0 4.24 4.24"/><path d="M10.73 5.08A10.43 10.43 0 0 1 12 5c7 0 10 7 10 7a13.16 13.16 0 0 1-1.67 2.68"/><path d="M6.61 6.61A13.526 13.526 0 0 0 2 12s3 7 10 7a9.74 9.74 0 0 0 5.39-1.61"/><line x1="2" x2="22" y1="2" y2="22"/></svg>`,
};

const GROUP_ICON_MAP = {
  全局: "sliders",
  哔哩哔哩: "tv",
  抖音: "music",
  快手: "zap",
  微博: "flame",
  AcFun: "play",
  小红书: "book",
  米游社: "gamepad",
  小黑盒: "gamepad",
  微视: "film",
  YouTube: "play",
  微信视频号: "video",
  代理: "globe",
  下载: "download",
  下载与代理: "download",
  暂存目录: "folder",
  卡片解析: "layers",
  AI总结: "sparkles",
  解析: "search",
  展示: "palette",
};

const state = {
  groups: [],
  active: "",
  lastGroup: "",
  query: "",
  pending: new Map(),
  edited: new Set(),
  providers: [],
  providersLoaded: false,
  platforms: [],
  platformBusy: new Set(),
};

/* ---------------------------------------------------------------- 工具函数 */

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

function iconFor(name) {
  if (GROUP_ICON_MAP[name]) return SVG_ICONS[GROUP_ICON_MAP[name]];
  const hit = Object.keys(GROUP_ICON_MAP).find((key) => name.includes(key));
  return hit ? SVG_ICONS[GROUP_ICON_MAP[hit]] : SVG_ICONS.wrench;
}

function fieldValue(field) {
  return state.pending.has(field.key) ? state.pending.get(field.key) : field.value;
}

function allFields() {
  return state.groups.flatMap((group) => group.fields);
}

function copyText(text) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(
      () => toast(`已复制配置键：${text}`),
      () => fallbackCopy(text)
    );
  } else {
    fallbackCopy(text);
  }
}

function fallbackCopy(text) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try {
    document.execCommand("copy");
    toast(`已复制配置键：${text}`);
  } catch (_) {}
  document.body.removeChild(ta);
}

/* ---------------------------------------------------------------- 主题切换 */

// 顶栏 logo：图能加载就显示图，加载不出来（离线/文件缺失）退回流光底 + R
function initBrandLogo() {
  const box = $("#brandLogo");
  const img = $("#brandLogoImg");
  if (!box || !img) return;
  const fallback = () => box.classList.remove("has-img");
  if (img.complete && !img.naturalWidth) {
    fallback();
    return;
  }
  img.addEventListener("error", fallback, { once: true });
}

function initTheme() {
  try {
    const saved = localStorage.getItem("r_plugin_theme");
    if (saved === "light" || saved === "dark") {
      document.documentElement.setAttribute("data-theme", saved);
    }
  } catch (_) {}

  const btn = $("#themeToggle");
  if (!btn) return;
  btn.addEventListener("click", () => {
    const cur = document.documentElement.getAttribute("data-theme");
    const sysDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
    const isDark = cur ? cur === "dark" : sysDark;
    const next = isDark ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try {
      localStorage.setItem("r_plugin_theme", next);
    } catch (_) {}
  });
}

/* ---------------------------------------------------------------- 状态同步与保存栏 */

function updateSavebar() {
  const dock = $("#savebarDock");
  const hint = $("#saveHint");
  const count = state.edited.size;
  if (count > 0) {
    hint.textContent = `有 ${count} 项未保存的修改`;
    dock.classList.add("visible");
  } else {
    hint.textContent = "";
    dock.classList.remove("visible");
  }
}

function setPending(field, value) {
  state.pending.set(field.key, value);
  if (!state.edited.has(field.key)) {
    state.edited.add(field.key);
    paintRail();
  }
  markChanged(field.key, true);
  updateSavebar();
}

function clearPendingKey(key) {
  state.pending.delete(key);
  state.edited.delete(key);
  markChanged(key, false);
  updateSavebar();
  paintRail();
}

function markChanged(key, changed = true) {
  document.querySelectorAll(`.field[data-key="${CSS.escape(key)}"]`).forEach((el) => {
    el.classList.toggle("changed", changed);
  });
}

/* ---------------------------------------------------------------- 平台开关 */

// 平台开关不是「改了再点保存」的那类配置：点一下就直接写盘 + 热应用，
// 因为运行时入口每次都现读 DISABLED_PLATFORMS，改完立刻生效。

function renderPlatformChips(host) {
  if (!host) return;
  host.innerHTML = "";
  if (!state.platforms.length) {
    const empty = document.createElement("span");
    empty.className = "ps-empty";
    empty.textContent = "正在读取平台列表…";
    host.appendChild(empty);
    return;
  }
  state.platforms.forEach((item) => {
    const on = Boolean(item.enabled);
    const busy = state.platformBusy.has(item.key);
    const button = document.createElement("button");
    button.type = "button";
    button.className = `ps-chip ${on ? "on" : "off"}${busy ? " busy" : ""}`;
    button.dataset.platform = item.key;
    button.setAttribute("role", "switch");
    button.setAttribute("aria-checked", on ? "true" : "false");
    button.title = `${on ? "点击关闭" : "点击开启"}「${item.label}」的解析`;
    button.disabled = busy;
    button.innerHTML = `<span class="ps-dot" aria-hidden="true"></span><span class="ps-name">${escapeHtml(
      item.label
    )}</span>`;
    host.appendChild(button);
  });
}

function renderPlatformBar() {
  const host = $("#platformBar");
  if (!host) return;
  host.innerHTML = `
    <div class="ps-head">
      <span class="ps-title">平台开关</span>
      <span class="ps-hint">点一下即可开启 / 关闭该平台的解析，立即生效，无需保存</span>
    </div>
    <div class="ps-list" id="platformChips"></div>
  `;
  renderPlatformChips($("#platformChips"));
}

function paintPlatforms() {
  renderPlatformChips($("#platformChips"));
  renderPlatformChips($("#statusPlatformList"));
}

// 「禁用的解析平台」列表和平台开关是同一份数据的两种呈现，点完开关顺手同步
function syncDisabledPlatformsField(disabled) {
  if (!Array.isArray(disabled)) return;
  const field = allFields().find((entry) => entry.key === "DISABLED_PLATFORMS");
  if (field) field.value = disabled;
  const area = document.querySelector('.field[data-key="DISABLED_PLATFORMS"] textarea');
  if (!area) return;
  // 用户正在这个框里打字（或已有未保存的手改）时不要覆盖 DOM：
  // 开关 POST 的响应回来会把正在输入的内容抹掉，显示与 pending 就对不上了
  if (state.pending.has("DISABLED_PLATFORMS") || document.activeElement === area) return;
  area.value = disabled.join(NL);
  const meta = area.parentElement && area.parentElement.querySelector(".list-meta");
  if (meta) meta.textContent = `${disabled.length} 项`;
}

async function togglePlatform(key) {
  const item = state.platforms.find((entry) => entry.key === key);
  if (!item || state.platformBusy.has(key)) return;

  // 「禁用的解析平台」列表正好是这个开关的落盘形式：有未保存的手改就先别动，
  // 否则一保存就把刚点的开关状态盖掉了
  if (state.pending.has("DISABLED_PLATFORMS")) {
    toast("「禁用的解析平台」列表有未保存的修改，先保存或放弃再点平台开关", true);
    return;
  }

  const previous = item.enabled;
  const next = !previous;
  item.enabled = next; // 乐观更新：先给反馈，失败再回滚
  state.platformBusy.add(key);
  paintPlatforms();

  try {
    const result = await bridge.apiPost("view/platform", { key, enabled: next });
    if (result && Array.isArray(result.platforms) && result.platforms.length) {
      state.platforms = result.platforms;
    }
    // 「禁用的解析平台」那个列表控件显示的就是这份数据，顺手同步，
    // 不然面板上还留着点击前的旧值
    syncDisabledPlatformsField(result && result.disabled);
    toast(`已${next ? "开启" : "关闭"}「${item.label}」解析`);
    // 状态页的统计数字跟着变，不然刚关掉一个平台还显示「已启用 3 / 3」
    const on = state.platforms.filter((entry) => entry.enabled).length;
    const total = state.platforms.length;
    const badge = $("#statusPlatformCount");
    if (badge) badge.textContent = `已启用 ${on} / ${total}`;
    const kpi = $("#statusPlatformKpi");
    if (kpi) kpi.textContent = `${on} / ${total}`;
  } catch (error) {
    item.enabled = previous;
    toast(error.message || "平台开关切换失败", true);
  } finally {
    state.platformBusy.delete(key);
    paintPlatforms();
  }
}

/* ---------------------------------------------------------------- 渲染表单项 */

function renderField(field) {
  const wrap = document.createElement("div");
  wrap.className = "field";
  wrap.dataset.key = field.key;
  wrap.dataset.search = [field.key, field.label, field.hint, field.group]
    .join(" ")
    .toLowerCase();
  if (state.edited.has(field.key)) wrap.classList.add("changed");

  const labelBox = document.createElement("div");
  labelBox.className = "field-label";
  labelBox.innerHTML = `
    <div class="field-title-row">
      <span class="k">${escapeHtml(field.label)}</span>
      <span class="key" title="点击复制配置键">${escapeHtml(field.key)}</span>
    </div>
    ${field.hint ? `<span class="hint">${escapeHtml(field.hint)}</span>` : ""}
  `;
  const keyBadge = labelBox.querySelector(".key");
  keyBadge.addEventListener("click", () => copyText(field.key));

  const control = document.createElement("div");
  control.className = "field-control";
  const value = fieldValue(field);

  if (field.type === "bool") {
    const on = value === true;
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
      setPending(field, input.checked);
    });
  } else if (field.type === "int" || field.type === "float") {
    const input = document.createElement("input");
    input.type = "number";
    input.step = field.type === "float" ? "0.1" : "1";
    input.value = value ?? field.default ?? 0;
    input.addEventListener("input", () => {
      const raw = input.value.trim();
      setPending(field, raw === "" ? null : Number(raw));
    });
    control.appendChild(input);
  } else if (field.type === "list") {
    const box = document.createElement("div");
    box.className = "list-box";
    const area = document.createElement("textarea");
    area.value = Array.isArray(value) ? value.join(NL) : value || "";
    area.placeholder = "每行填写一项";
    const meta = document.createElement("div");
    meta.className = "list-meta";
    const syncLines = () => {
      const lines = area.value
        .split(/[\r\n]+/)
        .map((line) => line.trim())
        .filter(Boolean);
      meta.textContent = `${lines.length} 项`;
      return lines;
    };
    syncLines();
    area.addEventListener("input", () => {
      const lines = syncLines();
      setPending(field, lines);
    });
    box.appendChild(area);
    box.appendChild(meta);
    control.appendChild(box);
  } else if (field.dynamic_options === "providers") {
    const row = document.createElement("div");
    row.className = "dyn-row";
    const select = document.createElement("select");
    const fill = () => {
      const cur = String(fieldValue(field) ?? "");
      select.innerHTML = "";
      const blank = document.createElement("option");
      blank.value = "";
      blank.textContent = "（跟随当前会话默认模型）";
      select.appendChild(blank);
      const list = state.providers.slice();
      if (cur && !list.some((p) => p.id === cur)) {
        list.unshift({ id: cur, label: `${cur}（不在当前列表）` });
      }
      list.forEach((p) => {
        const item = document.createElement("option");
        item.value = p.id;
        item.textContent = p.label + (p.is_default ? " ★默认" : "");
        if (cur === p.id) item.selected = true;
        select.appendChild(item);
      });
      select.value = cur;
    };
    fill();
    select.addEventListener("change", () => setPending(field, select.value));

    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "mini-btn";
    btn.textContent = "刷新";
    btn.title = "重新从 AstrBot 读取模型列表";
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      btn.textContent = "刷新中…";
      try {
        await loadProviders(true);
        fill();
        toast(`已刷新，共 ${state.providers.length} 个模型`);
      } catch (err) {
        toast(`刷新失败：${err.message}`, true);
      } finally {
        btn.disabled = false;
        btn.textContent = "刷新";
      }
    });
    row.appendChild(select);
    row.appendChild(btn);
    control.appendChild(row);
  } else if (field.options && field.options.length) {
    const select = document.createElement("select");
    const labels = Array.isArray(field.labels) ? field.labels : [];
    field.options.forEach((option, index) => {
      const item = document.createElement("option");
      item.value = option;
      item.textContent = labels[index] || option;
      if (String(value) === String(option)) item.selected = true;
      select.appendChild(item);
    });
    select.addEventListener("change", () => setPending(field, select.value));
    control.appendChild(select);
  } else if (field.is_secret) {
    const row = document.createElement("div");
    row.className = "secret-row";
    const inputWrap = document.createElement("div");
    inputWrap.className = "secret-input-wrap";

    const input = document.createElement("input");
    input.type = "password";
    input.autocomplete = "new-password";
    input.placeholder = field.has_value ? "已设置，留空则不修改" : "未设置，粘贴到这里";
    if (state.pending.has(field.key)) input.value = state.pending.get(field.key);
    input.addEventListener("input", () => {
      if (input.value.trim() === "") {
        clearPendingKey(field.key);
      } else {
        setPending(field, input.value.trim());
      }
    });

    const eye = document.createElement("button");
    eye.type = "button";
    eye.className = "eye-btn";
    eye.title = "显示或隐藏内容";
    eye.innerHTML = SVG_ICONS.eye;
    eye.addEventListener("click", () => {
      const hidden = input.type === "password";
      input.type = hidden ? "text" : "password";
      eye.innerHTML = hidden ? SVG_ICONS.eyeOff : SVG_ICONS.eye;
    });

    inputWrap.appendChild(input);
    inputWrap.appendChild(eye);
    row.appendChild(inputWrap);

    if (field.has_value) {
      const badge = document.createElement("span");
      badge.className = "badge-set";
      badge.textContent = "已配置";
      row.appendChild(badge);
    }
    control.appendChild(row);
  } else if (field.type === "text") {
    wrap.classList.add("field-wide");
    const box = document.createElement("div");
    box.className = "prompt-box";
    const area = document.createElement("textarea");
    area.className = "prompt-area";
    area.spellcheck = false;
    area.rows = 8;
    area.value = value === null || value === undefined || value === "" ? field.default ?? "" : value;

    const counter = document.createElement("span");
    counter.className = "prompt-count";
    const stateTag = document.createElement("span");
    stateTag.className = "prompt-state";
    const refreshCount = () => {
      counter.textContent = `${area.value.length} 字符`;
      const blank =
        area.value.trim() === "" ||
        (typeof field.default === "string" && area.value === field.default);
      stateTag.textContent = blank ? "使用内置默认" : "已自定义";
      stateTag.classList.toggle("is-default", blank);
    };
    refreshCount();
    area.addEventListener("input", () => {
      refreshCount();
      setPending(field, area.value);
    });
    box.appendChild(area);

    const bar = document.createElement("div");
    bar.className = "prompt-bar";
    const meta = document.createElement("span");
    meta.className = "prompt-meta";
    meta.appendChild(stateTag);
    meta.appendChild(counter);
    bar.appendChild(meta);

    if (field.default) {
      const reset = document.createElement("button");
      reset.type = "button";
      reset.className = "mini-btn";
      reset.textContent = "恢复默认";
      reset.addEventListener("click", () => {
        area.value = field.default;
        refreshCount();
        setPending(field, field.default);
        toast("已填入默认提示词，记得点击保存");
      });
      bar.appendChild(reset);
    }
    box.appendChild(bar);
    control.appendChild(box);
  } else {
    const input = document.createElement("input");
    input.type = "text";
    input.value = value ?? "";
    input.placeholder = field.default ?? "";
    input.addEventListener("input", () => setPending(field, input.value));
    control.appendChild(input);
  }

  wrap.appendChild(labelBox);
  wrap.appendChild(control);
  return wrap;
}

/* ---------------------------------------------------------------- 侧边栏与主面板 */

function paintRail() {
  const host = $("#rail");
  if (!host) return;
  host.innerHTML = "";

  state.groups.forEach((group) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "rail-item";
    button.dataset.group = group.name;
    if (!state.query && state.active === group.name) button.classList.add("active");
    const dirty = group.fields.some((field) => state.edited.has(field.key));
    button.innerHTML = `
      <span class="ico">${iconFor(group.name)}</span>
      <span class="name">${escapeHtml(group.name)}</span>
      <span class="dirty ${dirty ? "" : "hidden"}"></span>
      <span class="num">${group.fields.length}</span>
    `;
    button.addEventListener("click", () => {
      state.query = "";
      $("#search").value = "";
      state.active = group.name;
      state.lastGroup = group.name;
      renderPanel();
      const scroller = $("#contentScroll");
      if (scroller) scroller.scrollTop = 0;
    });
    host.appendChild(button);
  });

  if (state.edited.size) {
    const div = document.createElement("div");
    div.className = "rail-divider";
    host.appendChild(div);

    const button = document.createElement("button");
    button.type = "button";
    button.className = `rail-item ${state.query === "__edited__" ? "active" : ""}`;
    button.innerHTML = `
      <span class="ico">${SVG_ICONS.edit}</span>
      <span class="name">待保存修改</span>
      <span class="num">${state.edited.size}</span>
    `;
    button.addEventListener("click", () => {
      state.query = "__edited__";
      $("#search").value = "";
      renderPanel();
    });
    host.appendChild(button);
  }

  const total = allFields().length;
  $("#railFoot").innerHTML = `
    <span>共 ${total} 项配置</span>
    <span>${state.pending.size ? `已改 ${state.pending.size} 项` : "已同步"}</span>
  `;
}

function groupOf(field) {
  return state.groups.find((group) => group.fields.includes(field));
}

function appendGroupedFields(host, fields) {
  let lastGroup = "";
  let card = null;
  fields.forEach((field) => {
    const group = groupOf(field);
    const gName = group ? group.name : "其他";
    if (gName !== lastGroup) {
      const sub = document.createElement("div");
      sub.className = "subhead";
      sub.textContent = gName;
      host.appendChild(sub);
      card = document.createElement("div");
      card.className = "field-group-card";
      host.appendChild(card);
      lastGroup = gName;
    }
    card.appendChild(renderField(field));
  });
}

function renderPanel() {
  const host = $("#fields");
  const title = $("#groupTitle");
  const desc = $("#groupDesc");
  const iconBox = $("#groupIcon");
  const count = $("#fieldCount");
  host.innerHTML = "";

  if (state.query === "__edited__") {
    const fields = allFields().filter((field) => state.edited.has(field.key));
    title.textContent = "待保存修改";
    desc.textContent = "以下配置已改动，点击底部「保存并生效」后写入插件配置";
    iconBox.innerHTML = SVG_ICONS.edit;
    count.textContent = `${fields.length} 项`;
    if (!fields.length) {
      host.innerHTML = '<div class="skeleton">当前没有未保存的改动，可在左侧选择分组继续配置。</div>';
      paintRail();
      return;
    }
    appendGroupedFields(host, fields);
    paintRail();
    return;
  }

  if (state.query) {
    const keyword = state.query.toLowerCase();
    const hits = allFields().filter((field) =>
      [field.key, field.label, field.hint, field.group]
        .join(" ")
        .toLowerCase()
        .includes(keyword)
    );
    title.textContent = `搜索「${state.query}」`;
    desc.textContent = "按名称、键名或说明实时筛选全部配置项";
    iconBox.innerHTML = SVG_ICONS.search;
    count.textContent = `${hits.length} 项匹配`;
    if (!hits.length) {
      host.innerHTML = `<div class="skeleton">没有找到匹配「${escapeHtml(state.query)}」的配置项。</div>`;
      paintRail();
      return;
    }
    appendGroupedFields(host, hits);
    paintRail();
    return;
  }

  const group =
    state.groups.find((item) => item.name === state.active) ||
    state.groups.find((item) => item.name === state.lastGroup) ||
    state.groups[0];
  if (!group) {
    host.innerHTML = '<div class="skeleton">当前没有可显示的配置项。</div>';
    paintRail();
    return;
  }

  state.active = group.name;
  state.lastGroup = group.name;
  title.textContent = group.name;
  desc.textContent = `管理「${group.name}」相关的解析参数与行为偏好`;
  iconBox.innerHTML = iconFor(group.name);
  count.textContent = `${group.fields.length} 项`;

  const bar = document.createElement("div");
  bar.className = "platform-switchbar";
  bar.id = "platformBar";
  host.appendChild(bar);
  renderPlatformBar();

  const card = document.createElement("div");
  card.className = "field-group-card";
  group.fields.forEach((field) => card.appendChild(renderField(field)));
  host.appendChild(card);
  paintRail();
}

/* ---------------------------------------------------------------- 数据加载 */

async function loadProviders(force = false) {
  if (state.providersLoaded && !force) return state.providers;
  try {
    const data = await bridge.apiGet("view/providers");
    state.providers = (data && data.items) || [];
    state.providersLoaded = true;
  } catch (err) {
    state.providers = [];
  }
  return state.providers;
}

async function loadSchema() {
  const data = await bridge.apiGet("view/schema");
  state.groups = data.groups || [];
  state.providersLoaded = false;
  if (state.groups.some((group) => group.fields.some((field) => field.dynamic_options))) {
    await loadProviders(true);
  }
  if (data.version) $("#versionTag").textContent = `v${data.version}`;
  if (!state.active || !state.groups.some((group) => group.name === state.active)) {
    const preferred = state.groups.find((group) => group.name === "全局");
    state.active = (preferred || state.groups[0] || {}).name || "";
  }
  state.pending.clear();
  state.edited.clear();
  if (state.query === "__edited__") state.query = "";
  updateSavebar();
  renderPanel();
}

function chip(text, kind) {
  const span = document.createElement("span");
  span.className = `chip ${kind || ""}`.trim();
  span.textContent = text;
  return span;
}

async function loadStatus(probeProxy = false) {
  const grid = $("#statusGrid");
  grid.innerHTML =
    '<div class="skeleton"><span class="spinner" aria-hidden="true"></span><span>正在读取状态…</span></div>';
  const data = await bridge.apiGet("view/status", probeProxy ? { probe: 1 } : {});

  const proxy = data.proxy || {};
  const temp = data.temp || {};
  const tools = data.tools || {};
  const platforms = data.platforms || [];
  const secrets = data.secrets || {};

  // 顶栏状态徽章
  const chips = $("#heroChips");
  chips.innerHTML = "";
  chips.appendChild(
    chip(
      proxy.url ? (proxy.ok === true ? "代理可用" : proxy.ok === false ? "代理异常" : "已配代理") : "直连",
      proxy.url ? (proxy.ok === false ? "warn" : "ok") : ""
    )
  );
  chips.appendChild(chip(temp.writable ? "暂存可写" : "暂存异常", temp.writable ? "ok" : "warn"));
  chips.appendChild(chip(`并发 ${data.download?.concurrency ?? "-"}`, ""));

  // 统计摘要
  const toolEntries = [
    ["ffmpeg", tools.ffmpeg],
    ["aria2c", tools.aria2c],
    ["BBDown", tools.bbdown],
    ["yt-dlp", tools.yt_dlp],
  ];
  const toolsReady = toolEntries.filter(([, v]) => Boolean(v)).length;

  const secretNames = {
    bili_sessdata: "B站 SESSDATA",
    douyin_ck: "抖音 Cookie",
    weibo_ck: "微博 Cookie",
    xhs_ck: "小红书 Cookie",
    xhh_ck: "小黑盒 Cookie",
    wxchannel_cookie: "视频号元宝 Cookie",
    youtube_cookies: "YouTube cookies.txt",
  };
  const secretEntries = Object.entries(secretNames);
  const secretsReady = secretEntries.filter(([k]) => Boolean(secrets[k])).length;
  const enabledPlatforms = platforms.filter((p) => p.enabled).length;

  const usedGb = typeof temp.used_mb === "number" ? temp.used_mb / 1024 : 0;
  const freeGb = typeof temp.free_gb === "number" ? temp.free_gb : 0;
  const totalGb = usedGb + freeGb;
  const usedPct = totalGb > 0 ? Math.min(100, Math.max(2, Math.round((usedGb / totalGb) * 100))) : 0;

  const proxyStateText =
    proxy.ok === true ? "可达 YouTube" : proxy.ok === false ? "连接失败" : proxy.url ? "已配置（未检测）" : "直连模式";
  const proxyStateClass =
    proxy.ok === true ? "ok-text" : proxy.ok === false ? "bad-text" : "";

  grid.innerHTML = `
    <div class="kpi-strip">
      <div class="kpi">
        <div class="kpi-top"><span>代理网络</span></div>
        <div class="kpi-val ${proxyStateClass}">${escapeHtml(proxy.ok === true ? "正常" : proxy.ok === false ? "异常" : proxy.url ? "已配" : "直连")}</div>
        <div class="kpi-sub">${escapeHtml(proxy.url || "当前未配置代理")}</div>
      </div>
      <div class="kpi">
        <div class="kpi-top"><span>暂存剩余空间</span></div>
        <div class="kpi-val ${temp.writable ? "" : "bad-text"}">${escapeHtml(temp.free_gb ?? "-")} GB</div>
        <div class="kpi-sub">${temp.writable ? `目录可写 · 已占 ${escapeHtml(temp.used_mb ?? "-")} MB` : "目录不可写，请检查权限"}</div>
      </div>
      <div class="kpi">
        <div class="kpi-top"><span>依赖组件</span></div>
        <div class="kpi-val">${toolsReady} / ${toolEntries.length}</div>
        <div class="kpi-sub">${toolsReady === toolEntries.length ? "全部组件已就绪" : "部分可选组件未安装"}</div>
      </div>
      <div class="kpi">
        <div class="kpi-top"><span>平台与凭据</span></div>
        <div class="kpi-val" id="statusPlatformKpi">${enabledPlatforms} / ${platforms.length}</div>
        <div class="kpi-sub">已配置 ${secretsReady} / ${secretEntries.length} 项凭据</div>
      </div>
    </div>

    <div class="bento-grid">
      <div class="card">
        <div class="card-head">
          <h3 class="card-title">${SVG_ICONS.sliders}<span>基础信息</span></h3>
          <span class="card-badge">v${escapeHtml(data.version || "-")}</span>
        </div>
        <div class="kv"><span>插件版本</span><code>${escapeHtml(data.version || "-")}</code></div>
        <div class="kv"><span>配置文件</span><code>${escapeHtml(data.config_path || "-")}</code></div>
        <div class="kv"><span>下载队列并发</span><span>${escapeHtml(data.download?.concurrency ?? "-")} 任务</span></div>
        <div class="kv"><span>单文件分段</span><span>${escapeHtml(data.download?.threads ?? "-")} 线程</span></div>
        <div class="kv"><span>aria2 加速</span><span class="${data.download?.aria2 ? "ok-text" : ""}">${data.download?.aria2 ? "已启用" : "未启用"}</span></div>
      </div>

      <div class="card">
        <div class="card-head">
          <h3 class="card-title">${SVG_ICONS.cpu}<span>依赖组件</span></h3>
          <span class="card-badge">${toolsReady} / ${toolEntries.length} 就绪</span>
        </div>
        <div class="tool-grid">
          ${toolEntries
            .map(([label, val]) => {
              const ok = Boolean(val);
              return `
                <div class="tool-tile">
                  <div class="tool-tile-head">
                    <span>${escapeHtml(label)}</span>
                    <span class="status-dot ${ok ? "ok" : "bad"}" title="${ok ? "已安装" : "未安装"}"></span>
                  </div>
                  <div class="tool-tile-ver ${ok ? "" : "bad-text"}">${escapeHtml(ok ? val : "未安装")}</div>
                </div>
              `;
            })
            .join("")}
        </div>
      </div>

      <div class="card">
        <div class="card-head">
          <h3 class="card-title">${SVG_ICONS.globe}<span>代理设置</span></h3>
          <span class="card-badge ${proxyStateClass}">${escapeHtml(proxyStateText)}</span>
        </div>
        <div class="kv"><span>代理地址</span><code>${escapeHtml(proxy.url || "（直连）")}</code></div>
        <div class="kv"><span>连通性自检</span><span class="${proxyStateClass}">${escapeHtml(proxyStateText)}</span></div>
        ${proxy.error ? `<div class="kv"><span>错误信息</span><code class="bad-text">${escapeHtml(proxy.error)}</code></div>` : ""}
      </div>

      <div class="card">
        <div class="card-head">
          <h3 class="card-title">${SVG_ICONS.folder}<span>暂存目录</span></h3>
          <span class="card-badge ${temp.writable ? "ok-text" : "bad-text"}">${temp.writable ? "可写" : "不可写"}</span>
        </div>
        <div class="meter" title="已占用占比约 ${usedPct}%">
          <div class="meter-fill ${temp.writable ? "ok" : ""}" style="width:${usedPct}%"></div>
        </div>
        <div class="kv"><span>目录路径</span><code>${escapeHtml(temp.path || "-")}</code></div>
        <div class="kv"><span>剩余空间</span><span>${escapeHtml(temp.free_gb ?? "-")} GB</span></div>
        <div class="kv"><span>已占用空间</span><span>${escapeHtml(temp.used_mb ?? "-")} MB</span></div>
      </div>

      <div class="card">
        <div class="card-head">
          <h3 class="card-title">${SVG_ICONS.layers}<span>平台开关</span></h3>
          <span class="card-badge" id="statusPlatformCount">已启用 ${enabledPlatforms} / ${platforms.length}</span>
        </div>
        <div class="pill-list" id="statusPlatformList"></div>
        <div class="card-note">点一下平台就能直接开关它的解析，立即生效</div>
      </div>

      <div class="card">
        <div class="card-head">
          <h3 class="card-title">${SVG_ICONS.key}<span>凭据状态</span></h3>
          <span class="card-badge">${secretsReady} / ${secretEntries.length} 已配置</span>
        </div>
        ${secretEntries
          .map(
            ([key, label]) =>
              `<div class="kv"><span>${escapeHtml(label)}</span><span class="${secrets[key] ? "ok-text" : "bad-text"}">${secrets[key] ? "已配置" : "未配置"}</span></div>`
          )
          .join("")}
      </div>
    </div>
  `;

  // 平台开关：状态接口返回的列表就是开关的权威状态；
  // 配置项页面的开关条也要跟着重绘（那里比状态页更早渲染）
  state.platforms = platforms;
  paintPlatforms();
}

/* ---------------------------------------------------------------- 交互绑定 */

async function triggerSave() {
  if (!state.pending.size) {
    toast("没有需要保存的修改");
    return;
  }
  const button = $("#saveBtn");
  if (button.disabled) return;
  button.disabled = true;
  const labelSpan = button.querySelector("span") || button;
  const oldLabel = labelSpan.textContent;
  labelSpan.textContent = "保存中…";
  try {
    const values = Object.fromEntries(state.pending);
    const result = await bridge.apiPost("view/save", { values });
    const saved = result.saved || [];
    const applied = result.applied || [];
    toast(`已保存 ${saved.length} 项${applied.length ? `，已热应用 ${applied.length} 项` : ""}`);
    state.pending.clear();
    state.edited.clear();
    await loadSchema();
    loadStatus().catch(() => {});
  } catch (error) {
    toast(error.message || "保存失败", true);
  } finally {
    button.disabled = false;
    labelSpan.textContent = oldLabel;
  }
}

function bind() {
  // 标签页切换
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((item) => {
        item.classList.remove("active");
        item.setAttribute("aria-selected", "false");
      });
      tab.classList.add("active");
      tab.setAttribute("aria-selected", "true");
      const name = tab.dataset.tab;
      $("#tab-settings").classList.toggle("hidden", name !== "settings");
      $("#tab-status").classList.toggle("hidden", name !== "status");
      if (name === "status") loadStatus().catch((error) => toast(error.message, true));
    });
  });

  // 搜索框
  let searchTimer = 0;
  $("#search").addEventListener("input", (event) => {
    clearTimeout(searchTimer);
    const value = event.target.value.trim();
    searchTimer = setTimeout(() => {
      state.query = value === "__edited__" ? "" : value;
      renderPanel();
    }, 90);
  });

  // 平台开关：事件委托，列表重绘后不用重新绑
  document.addEventListener("click", (event) => {
    const target = event.target;
    const chip = target && target.closest ? target.closest(".ps-chip") : null;
    if (!chip) return;
    event.preventDefault();
    togglePlatform(chip.dataset.platform);
  });

  // 键盘快捷键：/ 聚焦搜索，Ctrl+S / Cmd+S 保存
  document.addEventListener("keydown", (event) => {
    const tag = (event.target && event.target.tagName) || "";
    const inInput = tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT";

    if (event.key === "/" && !inInput && !event.ctrlKey && !event.metaKey && !event.altKey) {
      const searchEl = $("#search");
      if (searchEl && !$("#tab-settings").classList.contains("hidden")) {
        event.preventDefault();
        searchEl.focus();
      }
    } else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      if (!$("#tab-settings").classList.contains("hidden")) {
        event.preventDefault();
        triggerSave();
      }
    }
  });

  // 保存与放弃修改
  $("#saveBtn").addEventListener("click", triggerSave);

  $("#resetBtn").addEventListener("click", async () => {
    state.pending.clear();
    state.edited.clear();
    state.query = "";
    $("#search").value = "";
    await loadSchema();
    toast("已还原为当前已保存的配置");
  });

  // 状态刷新与自检
  $("#refreshStatus").addEventListener("click", () => {
    loadStatus(true).catch((error) => toast(error.message, true));
  });

  document.querySelectorAll("[data-probe]").forEach((button) => {
    button.addEventListener("click", async () => {
      const target = button.dataset.probe;
      button.disabled = true;
      const span = button.querySelector("span") || button;
      const old = span.textContent;
      span.textContent = "检测中…";
      try {
        const result = await bridge.apiPost("view/probe", { target });
        toast(result.message || (result.ok ? "通过" : "失败"), result.ok === false);
        await loadStatus();
      } catch (error) {
        toast(error.message || "检测失败", true);
      } finally {
        button.disabled = false;
        span.textContent = old;
      }
    });
  });
}

/* ---------------------------------------------------------------- 启动 */

(async function main() {
  initTheme();
  initBrandLogo();
  if (!bridge) {
    document.body.innerHTML =
      '<div class="skeleton" style="margin:32px auto;max-width:460px;">bridge 未注入，请在 AstrBot WebUI 中打开本视图。</div>';
    return;
  }
  await bridge.ready();
  bind();
  try {
    await loadSchema();
  } catch (error) {
    $("#fields").innerHTML = `<div class="skeleton">加载失败：${escapeHtml(error.message)}</div>`;
    return;
  }
  loadStatus().catch(() => {});
})();
