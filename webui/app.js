/* Android AI 测试台前端（零依赖 vanilla JS + echarts） */
"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => Array.from(el.querySelectorAll(sel));
const main = $("#main");
let kbCurrent = null;    // 当前编辑的知识卡名
let caseFilter = "all";  // 用例库当前模块过滤
let showDiag = false;    // 测试记录是否显示验证/调试会话
let recQuery = "";       // 测试记录搜索词（标题 / 结论 模糊匹配，当前会话内保留）
let recSearchTimer = null; // 输入防抖：停止输入 1s 后自动搜索
let recStatusFilter = []; // 测试记录结论筛选（多选）：PASS/FAIL/BLOCKED/WARN/ERROR；空=不过滤

// ── 工具 ──
async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${r.status}`);
  return r.json();
}
const esc = s => String(s ?? "").replace(/[&<>"]/g,
  c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const statusChip = s => s ? `<span class="chip ${esc(s)}">${esc(s)}</span>` : "";

// ── 主题（浅色 / 深色）──
// 切换方式：在 <html> 上写 data-theme="dark"，CSS 里 html[data-theme="dark"] 覆盖变量。
// 持久化到 localStorage；首次进入若无记录则跟随系统 prefers-color-scheme。
const THEME_KEY = "dsh-theme";
function systemPrefersDark() {
  try { return window.matchMedia("(prefers-color-scheme: dark)").matches; } catch { return false; }
}
function getTheme() {
  let saved = null;
  try { saved = localStorage.getItem(THEME_KEY); } catch {}
  return saved === "dark" || saved === "light" ? saved : (systemPrefersDark() ? "dark" : "light");
}
function applyTheme(mode) {
  const root = document.documentElement;
  if (mode === "dark") root.setAttribute("data-theme", "dark");
  else root.removeAttribute("data-theme");
  syncThemeButtons(mode);
}
// 把按钮外观同步为当前主题（每次视图重渲染后都要调，因为按钮是重新生成的）
function syncThemeButtons(mode) {
  const dark = (mode || getTheme()) === "dark";
  $$("[data-theme-toggle]").forEach(b => {
    b.textContent = dark ? "☀ 浅色" : "🌙 深色";
    b.title = dark ? "切换到浅色主题" : "切换到深色主题";
    b.setAttribute("aria-label", b.title);
    b.setAttribute("aria-pressed", String(dark));
  });
}
function setTheme(mode) {
  try { localStorage.setItem(THEME_KEY, mode); } catch {}
  applyTheme(mode);
}
function toggleTheme() {
  setTheme(getTheme() === "dark" ? "light" : "dark");
}
// 尽早应用，避免首屏闪白（脚本在 body 末尾也会再调一次幂等应用）
applyTheme(getTheme());


/* 标准时间展示：ISO(含毫秒/无时区) → YYYY-MM-DD HH:MM:SS
   后端存的是本地时间 isoformat（如 2026-09-21T18:57:09.517），
   直接 slice 即可，避免 Date 解析无时区字符串产生偏移。 */
function fmtTime(v, withDate = true) {
  if (!v) return "—";
  const s = String(v).replace("T", " ");
  const date = s.slice(0, 10);
  const time = s.slice(11, 19);
  if (!time) return withDate ? date : "—";
  return withDate ? `${date} ${time}` : time;
}
const fmtDate = v => v ? String(v).replace("T", " ").slice(0, 10) : "—";

/* 耗时：秒 → 时/分/秒可读形式
   0s→"0秒"，45s→"45秒"，90s→"1分30秒"，3725s→"1时2分5秒"
   只保留有意义的高位单位，避免 "0时0分45秒" 这种噪音。 */
function fmtDuration(sec) {
  if (sec === null || sec === undefined || isNaN(sec)) return "—";
  const n = Math.round(Number(sec));
  if (n < 0) return "—";
  const h = Math.floor(n / 3600);
  const m = Math.floor((n % 3600) / 60);
  const s = n % 60;
  if (h) return m ? `${h}时${m}分${s}秒` : `${h}时${s}秒`;
  if (m) return `${m}分${s}秒`;
  return `${s}秒`;
}

// 详情接口只给 started_at/finished_at（没有 duration_seconds），现场算一个。
// 未收尾的会话（finished_at 为空）算到"此刻"。
function durationOf(s) {
  if (s.duration_seconds != null) return s.duration_seconds;
  const a = Date.parse(s.started_at);
  if (isNaN(a)) return null;
  const b = s.finished_at ? Date.parse(s.finished_at) : Date.now();
  if (isNaN(b)) return null;
  return (b - a) / 1000;
}

/* 状态中文标签（判定规则见 SKILL.md） */
const statusCN = {
  PASS: "通过", FAIL: "失败", WARN: "警告",
  BLOCKED: "阻断", ERROR: "错误",
  running: "执行中", paused: "已暂停", waiting: "等待答复",
};
const statusText = s => s ? (statusCN[s] || s) : "—";

/* 模块名（包名）友好化 */
const pkgShort = p => {
  if (!p || p === "未识别") return "未识别";
  const parts = String(p).split(".");
  return parts.length > 2 ? parts.slice(-2).join(".") : p;
};

// 成功率 4 段配色：≥90 优秀(绿) / ≥70 良好(蓝绿) / ≥50 偏低(橙) / <50 差(红)
// total=0（还没有用例）时不评级，走默认色。
const rateLevel = (pct, total) => {
  if (!total) return "";
  if (pct >= 90) return "lv-a";
  if (pct >= 70) return "lv-b";
  if (pct >= 50) return "lv-c";
  return "lv-d";
};

// ── 通用弹窗 ──
function modal(title, bodyHTML, footerHTML = "") {
  const el = document.createElement("div");
  el.className = "modal-mask";
  el.innerHTML = `
    <div class="modal">
      <div class="modal-head"><b>${esc(title)}</b><span style="flex:1"></span>
        <button class="ghost modal-x" title="关闭">✕</button></div>
      <div class="modal-body">${bodyHTML}</div>
      ${footerHTML ? `<div class="modal-foot">${footerHTML}</div>` : ""}
    </div>`;
  document.body.appendChild(el);
  const close = () => el.remove();
  $(".modal-x", el).addEventListener("click", close);
  el.addEventListener("click", ev => { if (ev.target === el) close(); });
  return { el, close };
}

// ── 拖动模态框（用于轨迹回放：图较大，常要挪开遮挡）──
// 用 transform: translate() 位移，而不是改 left/top：
//   · 模态框仍由 .modal-mask 的 flex 居中，不必关心初始位置；
//   · 不参与布局，不会把宽版回放窗口的宽度算式（92vw / 按高度反推）撑坏；
//   · 复位只要把 translate 清零。
// 边界：位移量限制在"不把标题栏拖出视口"的范围内，拖飞了也能拖回来。
function makeDraggable(panel, handle) {
  const h = handle || panel.querySelector(".modal-head");
  if (!h) return;
  let dx = 0, dy = 0, sx = 0, sy = 0, dragging = false;
  // ⚠️ 必须在**面板未被位移时**量一次基准位置并缓存：
  //    getBoundingClientRect() 是含 transform 的，若在 pointermove 里边移边量，
  //    每次量到的 rect 都已经含了新的 dx，反推 base 就会错，
  //    结果 clamp 形同虚设（实测能把窗口拖到 left=100334 飞出视口）。
  let base = null;
  const measure = () => {
    const t = panel.style.transform;
    panel.style.transform = "none";                 // 临时归零，量真实基准
    const r = panel.getBoundingClientRect();
    base = { l: r.left, t: r.top, w: r.width, h: r.height };
    panel.style.transform = t;
  };

  const clamp = () => {
    if (!base) measure();
    // 至少留 120px 在视口内，避免拖到抓不回来
    const keep = Math.min(120, base.w);
    const minX = -base.l - (base.w - keep);
    const maxX = window.innerWidth - keep - base.l;
    const minY = -base.t;
    const maxY = window.innerHeight - 48 - base.t;
    dx = Math.max(minX, Math.min(maxX, dx));
    dy = Math.max(minY, Math.min(maxY, dy));
  };
  const apply = () => { panel.style.transform = `translate(${dx}px, ${dy}px)`; };

  h.style.cursor = "move";
  h.style.touchAction = "none";
  h.style.userSelect = "none";
  h.title = "按住可拖动窗口（双击标题栏 / 点 ⌖ 复位）";

  h.addEventListener("pointerdown", ev => {
    if (ev.target.closest("button")) return;   // 别抢关闭按钮的点击
    dragging = true;
    base = null;                                // 每次起拖前重新量基准（窗口尺寸可能变过）
    sx = ev.clientX - dx; sy = ev.clientY - dy;
    h.setPointerCapture(ev.pointerId);
    panel.classList.add("dragging");
  });
  h.addEventListener("pointermove", ev => {
    if (!dragging) return;
    dx = ev.clientX - sx; dy = ev.clientY - sy;
    clamp(); apply();
  });
  const end = ev => {
    if (!dragging) return;
    dragging = false;
    panel.classList.remove("dragging");
    try { h.releasePointerCapture(ev.pointerId); } catch {}
  };
  h.addEventListener("pointerup", end);
  h.addEventListener("pointercancel", end);

  // 双击标题栏复位
  const reset = () => { dx = 0; dy = 0; base = null; apply(); };
  h.addEventListener("dblclick", reset);
  // 视口变化时重新钳制，避免缩小窗口后弹窗留在视口外
  window.addEventListener("resize", () => {
    if (dx || dy) { base = null; clamp(); apply(); }
  });

  return { reset };
}

// ── 视图路由（点哪个哪个高亮）──
const VIEWS = {
  dashboard: () => viewDashboard(),
  sessions: () => viewSessions(),
  cases: () => viewCaseLib(),
  knowledge: () => viewKnowledge(),
  vision: () => viewVision(),
};
function activateNav(view) {
  $$(".nav-item").forEach(x => x.classList.toggle("active", x.dataset.view === view));
}
$$(".nav-item").forEach(el =>
  el.addEventListener("click", () => {
    activateNav(el.dataset.view);
    // 切走时取消待触发的搜索防抖，避免 1s 后把列表视图盖回来
    if (el.dataset.view !== "sessions" && recSearchTimer) {
      clearTimeout(recSearchTimer); recSearchTimer = null;
    }
    (VIEWS[el.dataset.view] || viewDashboard)();
  }));

// ══════════════════ 仪表盘 ══════════════════
let dashCharts = [];
async function viewDashboard() {
  dashCharts.forEach(c => { try { c.dispose(); } catch {} });
  dashCharts = [];
  activateNav("dashboard");
  main.innerHTML = "<div class='sub'>加载中…</div>";
  const d = await api("/api/dashboard");
  const ov = d.overview || {};
  const total = ov.total || 0, pass = ov.pass || 0, fail = ov.fail || 0;
  const blocked = ov.blocked || 0, neverRun = ov.never_run || 0;
  const runPct = total ? Math.round(pass / total * 100) : 0;

  // 最近 3 天：横轴只显示日期（MM-DD），缺失的天补 0
  const labels = ["今天", "昨天", "前天"];
  const today = new Date();
  const threeDays = [];
  for (let i = 0; i < 3; i++) {
    const dt = new Date(today.getTime() - i * 86400000);
    const key = `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, "0")}-${String(dt.getDate()).padStart(2, "0")}`;
    const r = (d.recent || []).find(x => x.day === key) || {};
    const tot = r.total || 0;
    threeDays.push({
      label: labels[i], date: key,
      total: tot, pass: r.pass || 0, fail: r.fail || 0, blocked: r.blocked || 0,
      passRate: tot ? Math.round((r.pass || 0) / tot * 100) : 0,
      failRate: tot ? Math.round((r.fail || 0) / tot * 100) : 0,
    });
  }
  // 柱状图按时间正序（前天→今天）
  const chartDays = [...threeDays].reverse();

  // 模块：按成功率从低到高排（最需要关注的排最前）
  const mods = (d.modules || []).map(m => {
    const ran = (m.pass || 0) + (m.fail || 0) + (m.blocked || 0);
    return {
      ...m,
      ran,
      passRate: ran ? Math.round((m.pass || 0) / ran * 100) : null,
    };
  }).sort((a, b) => {
    // 无执行记录的排最后；其余按成功率升序
    if (a.passRate === null && b.passRate === null) return b.total - a.total;
    if (a.passRate === null) return 1;
    if (b.passRate === null) return -1;
    return a.passRate - b.passRate;
  });

  main.innerHTML = `
    <h1>仪表盘</h1>

    <div class="panel">
      <div class="dash-top">
        <div class="dash-seg seg-count">
          <div class="seg-label">用例总数</div>
          <div class="big-num">${total}</div>
          <div class="big-rate ${rateLevel(runPct, total)}">整体成功率 <b>${runPct}%</b></div>
        </div>
        <div class="dash-seg seg-pie">
          <div class="seg-label">用例整体成功情况</div>
          <div id="chart-cases" class="chart-mid"></div>
          <div class="chart-legend-note" id="chart-note"></div>
        </div>
        <div class="dash-seg seg-bar">
          <div class="seg-label">最近三天成功 / 失败</div>
          <div id="chart-rate" class="chart-mid"></div>
        </div>
      </div>
    </div>

    <div class="panel">
      <div class="panel-title">按模块 · 按成功率由低到高</div>
      <div class="mod-list">
        ${mods.map(m => `
          <div class="mod-row">
            <div class="mod-head">
              <span class="mod-name" title="${esc(m.package)}">${esc(pkgShort(m.package))}</span>
              <span class="mod-meta">
                ${m.total} 个用例${m.ran ? ` · 已执行 ${m.ran}` : " · 未执行"}
              </span>
              <span class="mod-pct ${m.passRate === null ? "none"
                    : m.passRate >= 80 ? "good" : m.passRate >= 50 ? "mid" : "bad"}">
                ${m.passRate === null ? "—" : m.passRate + "%"}
              </span>
            </div>
            <div class="progress">
              <div class="progress-fill pass" style="width:${m.ran ? (m.pass || 0) / m.ran * 100 : 0}%"></div>
              <div class="progress-fill fail" style="width:${m.ran ? (m.fail || 0) / m.ran * 100 : 0}%"></div>
              <div class="progress-fill blocked" style="width:${m.ran ? (m.blocked || 0) / m.ran * 100 : 0}%"></div>
            </div>
            <div class="mod-legend">
              <span class="lg"><i style="background:${"#178a50"}"></i>成功 ${m.pass || 0}</span>
              <span class="lg"><i style="background:${"#d64545"}"></i>失败 ${m.fail || 0}</span>
              <span class="lg"><i style="background:${"#8250df"}"></i>阻断 ${m.blocked || 0}</span>
            </div>
          </div>`).join("") || "<div class='muted'>暂无用例</div>"}
      </div>
    </div>`;

  if (!window.echarts) {
    $("#chart-cases").innerHTML = "<div class='muted'>echarts.min.js 未加载</div>";
    return;
  }
  const C = { pass: "#178a50", fail: "#d64545", blocked: "#8250df", other: "#b9c0cf" };

  // ① 环形图：用例整体成功情况
  const pie = [
    { name: "成功", value: pass, itemStyle: { color: C.pass } },
    { name: "失败", value: fail, itemStyle: { color: C.fail } },
    { name: "阻断", value: blocked, itemStyle: { color: C.blocked } },
    { name: "未执行", value: neverRun, itemStyle: { color: C.other } },
  ].filter(x => x.value > 0);
  const c1 = echarts.init($("#chart-cases"));
  c1.setOption({
    tooltip: { trigger: "item", formatter: "{b}: {c} ({d}%)" },
    series: [{
      type: "pie", radius: ["52%", "76%"], center: ["50%", "50%"],
      label: { show: true, formatter: "{b}\n{c}", fontSize: 13, lineHeight: 17 },
      labelLine: { length: 8, length2: 8 },
      data: pie.length ? pie : [{ name: "无用例", value: 1, itemStyle: { color: C.other } }],
    }],
  });
  dashCharts.push(c1);
  $("#chart-note").innerHTML = `
    <span class="lg"><i style="background:${C.pass}"></i>成功 ${pass}</span>
    <span class="lg"><i style="background:${C.fail}"></i>失败 ${fail}</span>
    <span class="lg"><i style="background:${C.blocked}"></i>阻断 ${blocked}</span>
    <span class="lg"><i style="background:${C.other}"></i>未执行 ${neverRun}</span>`;

  // ② 柱状图：最近三天成功 / 失败次数（横轴=日期，悬停显示成功/失败/成功率）
  const c2 = echarts.init($("#chart-rate"));
  // 纵坐标只标 3 档：0 / 平均值 / 平均值×2。
  // 平均值 = 近三天「成功+失败」次数的均值。
  // ⚠️ 但取 max(平均值×2, 各天最大值)：否则某天柱子会顶出坐标轴外面（截断）。
  //    例：数据 6/0/0 → 平均值 2，严格按 0/2/4 会截断，故抬到 0/3/6。
  const dayTotals = chartDays.map(x => (x.pass || 0) + (x.fail || 0));
  const avg = dayTotals.length
    ? dayTotals.reduce((a, b) => a + b, 0) / dayTotals.length : 0;
  const avgTick = Math.max(1, Math.round(avg));
  const yMax = Math.max(avgTick * 2, ...dayTotals, 1);
  c2.setOption({
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "shadow" },
      formatter: ps => {
        const x = chartDays[ps[0].dataIndex];
        const tot = x.total;
        const rate = tot ? Math.round(x.pass / tot * 100) : 0;
        const line = (label, val, color) =>
          `<div style="display:flex;align-items:center;gap:6px">
             <i style="width:9px;height:9px;border-radius:2px;background:${color};display:inline-block"></i>
             <span style="flex:1">${label}</span><b>${val}</b></div>`;
        return `<div style="min-width:150px">
            <div style="font-weight:600;margin-bottom:6px">${x.date}　${x.label}</div>
            ${line("成功", x.pass, C.pass)}
            ${line("失败", x.fail, C.fail)}
            ${x.blocked ? line("阻断", x.blocked, C.blocked) : ""}
            <div style="border-top:1px solid #e6e9ef;margin:6px 0 4px"></div>
            <div style="display:flex;gap:6px"><span style="flex:1">执行总数</span><b>${tot}</b></div>
            <div style="display:flex;gap:6px"><span style="flex:1">成功率</span><b>${rate}%</b></div>
          </div>`;
      },
    },
    grid: { left: 46, right: 18, top: 24, bottom: 30 },
    xAxis: {
      type: "category",
      data: chartDays.map(x => x.date),
      axisLabel: { fontSize: 12, color: "#737b90" },
      axisTick: { show: false },
    },
    yAxis: {
      type: "value",
      max: yMax,
      interval: yMax / 2,          // 只出 0 / 平均值 / 平均值×2 三档
      axisLabel: { fontSize: 12, color: "#737b90" },
      splitLine: { lineStyle: { color: "#eef0f4" } },
    },
    series: [
      { name: "成功", type: "bar", barMaxWidth: 46, barGap: "10%",
        itemStyle: { color: C.pass, borderRadius: [4, 4, 0, 0] },
        label: { show: true, position: "top", fontSize: 14, color: "#737b90" },
        data: chartDays.map(x => x.pass) },
      { name: "失败", type: "bar", barMaxWidth: 46,
        itemStyle: { color: C.fail, borderRadius: [4, 4, 0, 0] },
        label: { show: true, position: "top", fontSize: 14, color: "#737b90" },
        data: chartDays.map(x => x.fail) },
    ],
  });
  dashCharts.push(c2);

  if (!viewDashboard._resizeBound) {
    viewDashboard._resizeBound = true;
    window.addEventListener("resize", () =>
      dashCharts.forEach(c => { try { c.resize(); } catch {} }));
  }
}

// ══════════════════ 测试记录 ══════════════════
// （recQuery / recSearchTimer 已在文件头部声明）

// 结论的本地化别名：输入中文也能命中对应状态（不区分大小写）
const STATUS_FILTERS = ["PASS", "FAIL", "BLOCKED", "WARN", "ERROR"];
const STATUS_ALIASES = {
  PASS: ["pass", "通过", "成功", "合格", "已通过"],
  FAIL: ["fail", "失败", "不通过", "未通过", "错误"],
  BLOCKED: ["blocked", "阻断", "阻塞", "受阻", "无法判定"],
  WARN: ["warn", "警告", "告警"],
  RUNNING: ["running", "执行中", "运行中", "进行中"],
};

// 一条记录是否命中搜索词：标题 或 结论（含中文别名），均不区分大小写
function matchSession(s, q) {
  const title = String(s.title || `会话 #${s.id}`).toLowerCase();
  if (title.includes(q)) return true;
  const st = String(s.status || "").toUpperCase();
  if (st.toLowerCase().includes(q)) return true;
  const alias = STATUS_ALIASES[st];
  return !!(alias && alias.some(a => a.includes(q)));
}

async function viewSessions() {
  activateNav("sessions");
  main.innerHTML = "<div class='sub'>加载中…</div>";
  let url = "/api/sessions" + (showDiag ? "?all=1" : "");
  if (recStatusFilter.length) {
    const p = recStatusFilter.map(s => "status=" + encodeURIComponent(s)).join("&");
    url += (url.includes("?") ? "&" : "?") + p;
  }
  const { sessions, kinds } = await api(url);
  const diagN = (kinds && kinds.diag) || 0;
  const testN = (kinds && kinds.test) || 0;

  // 标题 / 结论 模糊匹配（不区分大小写）
  const q = recQuery.trim().toLowerCase();
  let matched = q ? sessions.filter(s => matchSession(s, q)) : sessions;
  // 结论多选筛选（后端已按 URL 过滤；这里再兜底一次，避免前端缓存态不一致）
  if (recStatusFilter.length) {
    const allow = recStatusFilter.map(s => s.toUpperCase());
    matched = matched.filter(s => allow.includes(String(s.status || "").toUpperCase()));
  }

  const fcount = recStatusFilter.length
    ? ` · 结论筛选 [${recStatusFilter.join("/")}] 命中 <b>${matched.length}</b> 条` : "";

  main.innerHTML = `
    <h1>测试记录</h1>
    <div class="lib-toolbar rec-toolbar">
      <input type="search" id="rec-search" class="rec-search" placeholder="按标题或结论搜索…"
        value="${esc(recQuery)}" title="按标题或结论模糊匹配（不区分大小写），停止输入 1 秒自动搜索">
      <div class="sub rec-stats">正式测试 <b>${testN}</b> 条${diagN ? ` · 验证/调试 <b>${diagN}</b> 条（默认不显示）` : ""}${
        q ? ` · 搜索“${esc(recQuery.trim())}”命中 <b>${matched.length}</b> 条` : ""}${fcount}</div>
      ${diagN ? `<label class="chk"><input type="checkbox" id="show-diag"${showDiag ? " checked" : ""}>显示验证记录</label>` : ""}
      <button class="ghost sm theme-toggle" data-theme-toggle
        title="切换浅色/深色主题">🌙 深色</button>
    </div>
    <div class="lib-toolbar rec-toolbar2">
      <div class="rec-filters">
        <span class="sub rec-flt-label">按结论筛选：</span>
        ${STATUS_FILTERS.map(st => `<button class="stab${recStatusFilter.includes(st) ? " on" : ""}"
          data-st="${st}" title="筛选结论为 ${st} 的记录（可多选）">${statusChip(st)}</button>`).join("")}
        ${recStatusFilter.length ? `<button class="ghost sm" id="rec-flt-clear" title="清除结论筛选">清除筛选</button>` : ""}
      </div>
      <button class="ghost sm danger rec-batch-del" id="rec-batch-del" disabled
        title="删除勾选的记录及其截图/日志证据，不可恢复">🗑 删除选中</button>
    </div>
    <div class="list-table recs">
      <div class="lt-head">
        <span class="c-chk"><input type="checkbox" id="rec-sel-all" title="全选 / 全不选（仅当前列表）"></span>
        <span class="c-id">#</span>
        <span class="c-title">标题</span>
        <span class="c-status">结论</span>
        <span class="c-pkg">模块</span>
        <span class="c-start">开始时间</span>
        <span class="c-dur">耗时</span>
        <span class="c-cnt">事件/断言</span>
        <span class="c-act">操作</span>
      </div>
      ${matched.map(s => `
        <div class="lt-row${s.kind === "diag" ? " diag" : ""}" data-id="${s.id}">
          <span class="c-chk"><input type="checkbox" class="rec-check" data-id="${s.id}"
            title="勾选后可批量删除"></span>
          <span class="c-id">${s.id}</span>
          <span class="c-title">
            ${s.kind === "diag" ? '<span class="tag-diag">验证</span>' : ""}
            <a class="row-link" data-id="${s.id}">${esc(s.title || `会话 #${s.id}`)}</a>
          </span>
          <span class="c-status">${statusChip(s.status)}</span>
          <span class="c-pkg" title="${esc(s.package || "")}">${esc(pkgShort(s.package))}</span>
          <span class="c-start">${esc(fmtTime(s.started_at))}</span>
          <span class="c-dur">${fmtDuration(s.duration_seconds)}</span>
          <span class="c-cnt">${s.event_count} / ${s.finding_count}</span>
          <span class="c-act">
            <button class="ghost sm" data-replay="${s.id}"
              ${s.has_evidence ? "" : "disabled"}
              title="${s.has_evidence ? "回放本次执行的轨迹（截图串播）" : "该记录没有可用的截图，无法回放"}">▶ 回放</button>
            <button class="ghost sm danger" data-del="${s.id}" title="删除该记录及其截图/日志">删除</button>
          </span>
        </div>`).join("") || (q
          ? `<div class='lt-empty'>没有标题或结论匹配“${esc(recQuery.trim())}”的记录。</div>`
          : "<div class='lt-empty'>还没有记录。用 <code>session.py start</code> 开启会话后由 AI 执行用例。</div>")}
    </div>`;

  // 搜索框：停止输入 1 秒自动搜索；清空后立即回到全部列表
  const searchEl = $("#rec-search");
  if (searchEl) {
    searchEl.addEventListener("input", () => {
      recQuery = searchEl.value;
      if (recSearchTimer) { clearTimeout(recSearchTimer); recSearchTimer = null; }
      // 清空：无需等待，立刻恢复全部列表
      if (!recQuery.trim()) { viewSessions(); return; }
      recSearchTimer = setTimeout(() => { recSearchTimer = null; viewSessions(); }, 1000);
    });
    // 回车仍可立即搜索（不等待防抖）
    searchEl.addEventListener("keydown", ev => {
      if (ev.key === "Enter") {
        if (recSearchTimer) { clearTimeout(recSearchTimer); recSearchTimer = null; }
        recQuery = searchEl.value;
        viewSessions();
      }
    });
    // 重渲染后把光标放回输入框末尾，避免连续输入被打断
    if (recQuery) {
      searchEl.focus();
      try { searchEl.setSelectionRange(searchEl.value.length, searchEl.value.length); } catch {}
    }
  }

  const cb = $("#show-diag");
  if (cb) cb.addEventListener("change", () => { showDiag = cb.checked; viewSessions(); });
  // 结论筛选：点击切换该状态（多选），再次点击取消；空=显示全部
  $$(".stab", main).forEach(btn =>
    btn.addEventListener("click", () => {
      const st = btn.dataset.st;
      const i = recStatusFilter.indexOf(st);
      if (i >= 0) recStatusFilter.splice(i, 1);
      else recStatusFilter.push(st);
      viewSessions();
    }));
  const clr = $("#rec-flt-clear");
  if (clr) clr.addEventListener("click", () => { recStatusFilter = []; viewSessions(); });
  // 主题切换按钮（与「正式测试」同一行）
  $$("[data-theme-toggle]").forEach(b =>
    b.addEventListener("click", () => { toggleTheme(); syncThemeButtons(); }));
  syncThemeButtons();
  $$(".row-link", main).forEach(el =>
    el.addEventListener("click", () => viewSession(el.dataset.id)));
  $$("[data-del]", main).forEach(el =>
    el.addEventListener("click", ev => {
      ev.stopPropagation();          // 不能触发"点击查看"
      confirmDeleteSession(el.dataset.del);
    }));
  $$("[data-replay]", main).forEach(el =>
    el.addEventListener("click", ev => {
      ev.stopPropagation();
      replaySession(el.dataset.replay);
    }));

  // 批量删除：勾选框 → 「删除选中」按钮
  const selAll = $("#rec-sel-all");
  const batchBtn = $("#rec-batch-del");
  const checkedIds = () => $$(".rec-check:checked", main).map(el => el.dataset.id);
  const syncBatch = () => {
    const ids = checkedIds();
    if (batchBtn) {
      batchBtn.disabled = !ids.length;
      batchBtn.textContent = ids.length ? `🗑 删除选中 (${ids.length})` : "🗑 删除选中";
    }
    if (selAll) selAll.checked = ids.length > 0 &&
      ids.length === $$(".rec-check", main).length;
  };
  $$(".rec-check", main).forEach(el => el.addEventListener("change", syncBatch));
  if (selAll) selAll.addEventListener("change", () => {
    $$(".rec-check", main).forEach(el => { el.checked = selAll.checked; });
    syncBatch();
  });
  if (batchBtn) batchBtn.addEventListener("click", () => {
    const ids = checkedIds();
    if (ids.length) confirmDeleteSessions(ids);
  });
}

function confirmDeleteSession(id) {
  const m = modal("删除测试记录", `
    <div class="warn-text">确定删除记录 <b>#${esc(id)}</b> 吗？</div>
    <div class="meta">将同时删除该记录的事件流、断言，以及<b>磁盘上的截图与日志证据</b>，<b>不可恢复</b>。
    被测 App 本身不受影响。</div>
    <label class="chk" style="margin-top:10px">
      <input type="checkbox" id="del-ev" checked> 同时删除截图 / 日志文件
    </label>`,
    `<button class="ghost" data-cancel>取消</button>
     <button class="danger" data-ok>确认删除</button>`);
  $("[data-cancel]", m.el).addEventListener("click", m.close);
  $("[data-ok]", m.el).addEventListener("click", async () => {
    const withEv = $("#del-ev", m.el).checked;
    const r = await api(`/api/sessions/${id}?evidence=${withEv ? 1 : 0}`,
                        { method: "DELETE" });
    m.close();
    await viewSessions();
    if (r && r.files != null) {
      toast(`已删除记录 #${id}${withEv ? `，清理 ${r.files} 个证据文件` : ""}`);
    }
  });
}

function confirmDeleteSessions(ids) {
  const m = modal("批量删除测试记录", `
    <div class="warn-text">确定删除 <b>${ids.length}</b> 条记录吗？</div>
    <div class="meta">将同时删除这些记录的事件流、断言，以及<b>磁盘上的截图与日志证据</b>，<b>不可恢复</b>。
    被测 App 本身不受影响。</div>
    <label class="chk" style="margin-top:10px">
      <input type="checkbox" id="del-ev-b" checked> 同时删除截图 / 日志文件
    </label>`,
    `<button class="ghost" data-cancel>取消</button>
     <button class="danger" data-ok>确认删除</button>`);
  $("[data-cancel]", m.el).addEventListener("click", m.close);
  $("[data-ok]", m.el).addEventListener("click", async () => {
    const withEv = $("#del-ev-b", m.el).checked;
    const okBtn = $("[data-ok]", m.el);
    okBtn.disabled = true; okBtn.textContent = "删除中…";
    let okN = 0, failN = 0, files = 0;
    for (const id of ids) {
      try {
        const r = await api(`/api/sessions/${id}?evidence=${withEv ? 1 : 0}`,
                            { method: "DELETE" });
        if (r && r.ok) { okN++; if (r.files != null) files += r.files; }
        else failN++;
      } catch { failN++; }
    }
    m.close();
    await viewSessions();
    toast(`批量删除完成：成功 ${okN} 条${failN ? `，失败 ${failN} 条` : ""}` +
          (withEv && files ? `，清理 ${files} 个证据文件` : ""));
  });
}

/* 轻量提示条（操作反馈） */
function toast(msg) {
  const el = document.createElement("div");
  el.className = "toast";
  el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), 2600);
}

// ── 轨迹回放：把会话的截图串起来播，点击位置画圆点 ──────────────
// 不用录屏、不合成视频 —— 直接用已有截图当"简易录屏"。
// ⚠️ 坐标换算：x/y 是**原图坐标系**（size 是原图尺寸），
//    用百分比 `x/size_w*100%` 定位 → 图片被 CSS 缩成多大都不会偏。
async function replaySession(id) {
  let data;
  try {
    data = await api(`/api/sessions/${id}/replay`);
  } catch (e) {
    toast(`回放数据加载失败：${e.message}`);
    return;
  }
  if (!data || !data.total) {
    toast("这条记录没有可回放的截图");
    return;
  }

  const m = modal(`轨迹回放 · ${data.title}`, `
    <div class="rp">
      <div class="rp-stage">
        <img class="rp-img" alt="帧">
        <div class="rp-dot hidden"></div>
        <div class="rp-gone hidden">🖼 该帧的截图已清理</div>
      </div>
      <div class="rp-sub"></div>
      <div class="rp-bar">
        <button class="ghost" data-rp="prev" title="上一帧">⏮</button>
        <button class="ghost rp-play" data-rp="play" title="播放/暂停">▶</button>
        <button class="ghost" data-rp="next" title="下一帧">⏭</button>
        <input class="rp-range" type="range" min="1" max="${data.total}" value="1">
        <span class="rp-count">1 / ${data.total}</span>
        <select class="rp-speed">
          <option value="2000">0.5×</option>
          <option value="1000" selected>1×</option>
          <option value="500">2×</option>
          <option value="250">4×</option>
        </select>
        <button class="ghost rp-recenter" data-rp="recenter" title="把窗口移回屏幕中央">⌖</button>
      </div>
      ${data.missing ? `<div class="rp-warn">⚠️ ${data.missing} 帧的截图已被清理，播放时显示占位</div>` : ""}
    </div>`);
  m.el.querySelector(".modal").classList.add("rp-modal");   // 宽版，图才看得清
  const drag = makeDraggable(m.el.querySelector(".modal"));
  $("[data-rp='recenter']", m.el).addEventListener("click", () => drag.reset());

  const img = $(".rp-img", m.el);
  const dot = $(".rp-dot", m.el);
  const sub = $(".rp-sub", m.el);
  const gone = $(".rp-gone", m.el);
  const range = $(".rp-range", m.el);
  const count = $(".rp-count", m.el);
  const playBtn = $(".rp-play", m.el);
  const speedSel = $(".rp-speed", m.el);

  let i = 0;
  let timer = null;
  let rewindTimer = null;          // 播完后"回到起点"的延时器
  const END_HOLD_MS = 900;         // 末帧停留时长，让最后一帧看得清再回起点

  // 播完后若用户已手动拖拽/翻帧，就不要再把它拉回起点
  function cancelRewind() {
    if (rewindTimer) { clearTimeout(rewindTimer); rewindTimer = null; }
  }

  function show(n) {
    i = Math.max(0, Math.min(data.total - 1, n));
    const f = data.frames[i];
    range.value = i + 1;
    count.textContent = `${i + 1} / ${data.total}`;

    // 图片缺失时不留破图；**舞台尺寸保持不变**（靠 CSS 的 aspect-ratio），
    // 否则整块会塌成一条，看着像页面坏了。
    // ⚠️ 用 `.hidden` class（项目约定，带 !important）而不是 `el.hidden` 属性 ——
    // 属性会被作者样式里的 display 盖掉（真实缺陷：图在却显示"已清理"占位）。
    gone.classList.toggle("hidden", !!f.ok);
    // ⚠️ 用 visibility 而非 display：display:none 会让 img 不占位、舞台塌掉
    img.style.visibility = f.ok ? "visible" : "hidden";
    if (f.ok) img.src = `/files/${f.evidence}`;
    if (f.size) $(".rp-stage", m.el).style.aspectRatio = `${f.size[0]} / ${f.size[1]}`;

    // 点击位置圆点：用**百分比**换算，图片缩放多少都准
    const [w, h] = f.size || [];
    const hasDot = f.ok && w && h && f.x != null && f.y != null;
    dot.classList.toggle("hidden", !hasDot);
    if (hasDot) {
      dot.style.left = `${(f.x / w) * 100}%`;
      dot.style.top = `${(f.y / h) * 100}%`;
    }

    // 字幕条（在图下方，**不遮挡画面**）
    const parts = [`<span class="rp-sub-n">第 ${f.seq} 步</span>`];
    // 真实时间戳：与「执行录屏」同一时间轴，便于核对两者是否对齐
    if (f.ts) parts.push(`<code>${esc(fmtTime(f.ts))}</code>`);
    if (f.kind === "act") {
      parts.push(`<b>${esc(f.action || "操作")}</b>`);
      if (f.x != null) parts.push(`<code>${f.x}, ${f.y}</code>`);
      if (f.via) parts.push(`<code>${esc(f.via)}</code>`);
      if (f.why) parts.push(esc(f.why));
    } else {
      parts.push(`<b>${esc(f.kind)}</b>`);
      if (f.detail) parts.push(esc(f.detail));
    }
    sub.innerHTML = parts.join('<span class="rp-sep">·</span>');
  }

  function stop() {
    if (timer) { clearInterval(timer); timer = null; }
    playBtn.textContent = "▶";
  }
  function play() {
    if (timer) { stop(); return; }
    // 已停在末帧时再点播放：先回到起点，否则一点就立刻"播完"
    if (i >= data.total - 1) show(0);
    playBtn.textContent = "⏸";
    const tick = () => {
      if (i >= data.total - 1) {
        // 播到最后一帧：先停，稍作停留让末帧看得清，再**自动回到起点**
        // （进度条回到起点 → 可直接再次点播放重看，不用手动拖回去）
        stop();
        rewindTimer = setTimeout(() => { rewindTimer = null; show(0); }, END_HOLD_MS);
        return;
      }
      show(i + 1);
    };
    timer = setInterval(tick, Number(speedSel.value));
  }

  $("[data-rp='prev']", m.el).addEventListener("click", () => { stop(); cancelRewind(); show(i - 1); });
  $("[data-rp='next']", m.el).addEventListener("click", () => { stop(); cancelRewind(); show(i + 1); });
  playBtn.addEventListener("click", play);
  range.addEventListener("input", () => { stop(); cancelRewind(); show(Number(range.value) - 1); });
  speedSel.addEventListener("change", () => { if (timer) { stop(); play(); } });

  // 键盘：← → 翻帧，空格播放/暂停
  const onKey = ev => {
    if (!document.body.contains(m.el)) { document.removeEventListener("keydown", onKey); return; }
    if (ev.key === "ArrowLeft") { stop(); cancelRewind(); show(i - 1); }
    else if (ev.key === "ArrowRight") { stop(); cancelRewind(); show(i + 1); }
    else if (ev.key === " ") { ev.preventDefault(); play(); }
  };
  document.addEventListener("keydown", onKey);

  const origClose = $(".modal-x", m.el);
  origClose.addEventListener("click", () => { stop(); cancelRewind(); });
  show(0);
}

async function viewSession(id) {
  main.innerHTML = "<div class='sub'>加载中…</div>";
  const s = await api(`/api/sessions/${id}`);  const kindLabel = { observe: "👁 观察", act: "✋ 操作", read: "🔍 读取",
                      ask: "❓ 问人", pause: "⏸ 暂停", note: "📝 备注",
                      state: "⚙️ 状态", logcat: "📎 取证", crash: "💥 崩溃" };

  // 相邻事件时间差 = AI 决策耗时；事件自身 duration_ms = 工具执行耗时
  const ts = e => Date.parse(e.created_at);
  let prev = null;
  const gaps = {};
  for (const e of s.events) {
    if (prev !== null && !isNaN(ts(e)) && !isNaN(ts(prev)))
      gaps[e.seq] = Math.max(0, (ts(e) - ts(prev)) / 1000);
    prev = e;
  }
  const getAct = e => { try { return (JSON.parse(e.data_json || "{}").action) || {}; } catch { return {}; } };
  const getCrash = e => {
    try {
      const c = JSON.parse(e.data_json || "{}").crash;
      return c && c.scope ? c : null;
    } catch { return null; }
  };
  const scopeLabel = { target: "被测包", related: "关联包", other: "其他包" };
  const fmtGap = sec => sec >= 60 ? `${Math.floor(sec / 60)}m${Math.round(sec % 60)}s` : `${sec.toFixed(1)}s`;
  // 有无可回放的截图（图可能被 cleanup 轮转掉）→ 决定回放按钮是否可点
  const hasEv = (s.events || []).some(e => e.evidence);

  main.innerHTML = `
    <button class="ghost back">← 返回列表</button>
    <button class="ghost" data-replay="${s.id}" ${hasEv ? "" : "disabled"}
      title="${hasEv ? "回放本次执行的轨迹（截图串播）" : "该记录没有可用的截图，无法回放"}">▶ 回放轨迹</button>
    <div class="detail-head">
      <h1>${esc(s.title || `会话 #${s.id}`)} ${statusChip(s.status)}</h1>
      <div class="meta">模块 ${esc(s.package || "—")} · 设备 ${esc(s.device || "—")} ·
        开始 ${esc(fmtTime(s.started_at))} → 结束 ${esc(fmtTime(s.finished_at))} · 耗时 ${fmtDuration(durationOf(s))}</div>
      ${s.user_input ? `<div class="quote">用例口述：${esc(s.user_input)}</div>` : ""}
      <div class="findings">
        ${(() => {
          // 期望校验模块：每个用例有多少条「期望」，就展示多少块。
          // 将每个 finding 的 expect 按 ；/;/换行 拆成单条期望，逐条渲染为一个模块；
          // 复跑时若已按单条预期分别建 finding，则天然 1 条预期 = 1 块。
          const items = [];
          for (const f of (s.findings || [])) {
            let exps = (f.expect || "").split(/[；;\n]+/).map(x => x.trim()).filter(Boolean);
            if (exps.length === 0) exps = [""];
            exps.forEach((txt, k) => items.push({
              idx: k + 1, text: txt, status: f.status,
              actual: f.actual, note: f.note, event_ids: f.event_ids || []
            }));
          }
          if (!items.length) return "<span class='muted'>无断言记录</span>";
          return `<div class="exp-title">期望校验（共 ${items.length} 项）</div>` + items.map(e => `
            <div class="finding">${statusChip(e.status)}
              <span class="exp-idx">期望 ${e.idx}</span>${e.text ? esc(e.text) : '<span class="muted">（无明确期望文本）</span>'}
              ${e.actual ? `<div class="small">实际：${esc(e.actual)}</div>` : ""}
              ${e.note ? `<div class="small">${esc(e.note)}</div>` : ""}
              ${e.event_ids.length ? `<div class="small">证据：${e.event_ids.map(id => `<a href="#event-${s.id}-${id}">事件 ${esc(id)}</a>`).join(" · ")}</div>` : ""}
            </div>`).join("");
        })()}
      </div>
    </div>
    <div class="final-result">
      <span class="final-label">最终结果</span>${statusChip(s.status)}
      <span class="final-summary">${esc(s.summary || (s.status === "running" ? "执行中…" : "未写结论"))}</span>
    </div>
    ${s.recording ? `
    <h1 style="font-size:16px;margin:18px 0 12px">执行录屏 <span class="rec-dur"></span></h1>
    <div class="rec-wrap">
      <video class="rec-video" controls preload="metadata"
        src="/files/${esc(s.recording)}"></video>
    </div>` : ""}
    <h1 style="font-size:16px;margin:18px 0 12px">执行时间线</h1>
    <div class="timeline">
      ${s.events.map(e => {
        const act = getAct(e);
        const why = act.why || null;
        return `
        <div class="event ${esc(e.kind)}" id="event-${s.id}-${e.id}">
          <div class="event-head">
            <span class="event-tool">${kindLabel[e.kind] || esc(e.kind)} · ${esc(e.tool || "")}</span>
            ${e.duration_ms != null ? `<span class="metric">执行 ${fmtDuration(e.duration_ms / 1000)}</span>` : ""}
            ${gaps[e.seq] != null ? `<span class="metric">决策间隔 ${fmtGap(gaps[e.seq])}</span>` : ""}
            <span class="event-ts">#${e.seq} ${esc(fmtTime(e.started_at || e.created_at))}</span>
          </div>
          ${e.detail ? `<div class="event-detail">${esc(e.detail)}</div>` : ""}
          ${(() => {
            const c = getCrash(e);
            if (!c) return "";
            return `<div class="crash-box ${esc(c.scope)}">
              ⚠️ ${scopeLabel[c.scope] || c.scope} ${esc(c.type || "CRASH")} · ${esc(c.process || "?")}
              ${c.time ? `· ${esc(c.time)}` : ""}
              ${c.log_path ? ` · <a href="/files/${esc(c.log_path)}" target="_blank">崩溃前20s日志</a>` : "（无日志）"}
            </div>`;
          })()}
          ${why ? `<div class="why">理由：${esc(why)}</div>` : ""}
          ${(() => {
            const ev = e.evidence;
            if (!ev) return "";
            if (ev.endsWith(".png") || ev.endsWith(".jpg") || ev.endsWith(".jpeg"))
              // ⚠️ 图可能已被清理（cleanup.py 轮转 / 手工删除）——那时别留个破图，
              // 给个"证据已清理"的占位，记录与文本结果照常保留。
              return `<img class="shot" loading="lazy" src="/files/${esc(ev)}"
                onerror="this.outerHTML='<div class=&quot;ev-gone&quot;>🖼 证据已清理（${esc(ev.split("/").pop())}）</div>'">`;
            const name = ev.split("/").pop();
            const dl = e.kind === "logcat" || ev.endsWith(".log");
            return `<div class="ev-file">📎 <a href="/files/${esc(ev)}"
              target="_blank"${dl ? " download" : ""}>${esc(name)}</a></div>`;
          })()}
        </div>`;
      }).join("") || "<span class='muted'>无事件</span>"}
    </div>`;
  $(".back", main).addEventListener("click", viewSessions);
  const rpBtn = $("[data-replay]", main);
  if (rpBtn) rpBtn.addEventListener("click", () => replaySession(s.id));
  // 执行录屏：视频元数据就绪后显示真实时长，便于与「耗时 / 轨迹回放」核对
  const recVid = $(".rec-video", main);
  if (recVid) recVid.addEventListener("loadedmetadata", () => {
    const span = $(".rec-dur", main);
    if (span && recVid.duration) span.textContent = "· 时长 " + fmtDuration(recVid.duration);
  });
  $$("img.shot", main).forEach(img =>
    img.addEventListener("click", () => {
      $("#lightbox-img").src = img.src;
      $("#lightbox").classList.remove("hidden");
    }));
}

$("#lightbox").addEventListener("click", () => $("#lightbox").classList.add("hidden"));

// ══════════════════ 用例库 ══════════════════
async function viewCaseLib() {
  activateNav("cases");
  main.innerHTML = "<div class='sub'>加载中…</div>";
  const [{ items }, { modules }] = await Promise.all([
    api("/api/cases"), api("/api/modules"),
  ]);

  // 按模块（包名）分类；用例自身的 package 优先，缺失归「未识别」
  const grpOf = c => c.package || "未识别";
  const groups = new Map();
  for (const c of items) {
    const g = grpOf(c);
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(c);
  }
  const groupNames = [...groups.keys()].sort();
  if (caseFilter !== "all" && !groups.has(caseFilter)) caseFilter = "all";
  const shown = caseFilter === "all" ? items : (groups.get(caseFilter) || []);

  const tabs = `
    <div class="tabs">
      <span class="tab ${caseFilter === "all" ? "on" : ""}" data-g="all">全部 ${items.length}</span>
      ${groupNames.map(g => `
        <span class="tab ${caseFilter === g ? "on" : ""}" data-g="${esc(g)}">
          ${esc(pkgShort(g))} ${groups.get(g).length}</span>`).join("")}
    </div>`;

  main.innerHTML = `
    <h1>用例库</h1>
    <div class="lib-toolbar">
      ${tabs}
      <span style="flex:1"></span>
      <button id="case-add">+ 添加用例</button>
    </div>
    <div class="list-table">
      <div class="lt-head">
        <span class="c-id">#</span>
        <span class="c-title">用例标题</span>
        <span class="c-pkg">模块</span>
        <span class="c-status">最近结论</span>
        <span class="c-run">跑过</span>
        <span class="c-start">最近运行</span>
        <span class="c-act">操作</span>
      </div>
      ${shown.map(c => `
        <div class="lt-row" data-id="${c.id}">
          <span class="c-id">${c.id}</span>
          <span class="c-title"><a class="row-link" data-id="${c.id}">${esc(c.title || "(无标题)")}</a></span>
          <span class="c-pkg" title="${esc(c.package || "")}">${esc(pkgShort(grpOf(c)))}</span>
          <span class="c-status">${statusChip(c.last_status) || "<span class='muted'>未执行</span>"}</span>
          <span class="c-run">${c.run_count ?? 0} 次</span>
          <span class="c-start">${esc(fmtTime(c.last_run_at))}</span>
          <span class="c-act">
            <button class="ghost sm" data-edit="${c.id}">修改</button>
            <button class="ghost sm danger" data-del="${c.id}">删除</button>
          </span>
        </div>`).join("") || "<div class='lt-empty'>该分类下暂无用例。跑完一次会话后用 <code>session.py case save --session &lt;id&gt;</code> 提升。</div>"}
    </div>`;

  $$(".tab", main).forEach(el =>
    el.addEventListener("click", () => { caseFilter = el.dataset.g; viewCaseLib(); }));
  $$(".row-link", main).forEach(el =>
    el.addEventListener("click", () => openCaseView(el.dataset.id)));
  $$("[data-edit]", main).forEach(el =>
    el.addEventListener("click", () => openCaseEdit(el.dataset.edit)));
  $$("[data-del]", main).forEach(el =>
    el.addEventListener("click", () => confirmDeleteCase(el.dataset.del)));
  $("#case-add").addEventListener("click", openCaseAdd);
}

function openCaseAdd() {
  const m = modal("添加用例", `
    <label class="fld">标题
      <input id="ca-title" placeholder="如 联想日历_168_图库导入入口"></label>
    <label class="fld">模块（包名）
      <input id="ca-pkg" placeholder="如 com.zui.calendar"></label>
    <label class="fld">用例原文（口述的步骤 + 预期）
      <textarea id="ca-input" spellcheck="false"
        placeholder="前提：...&#10;操作步骤：&#10;1. ...&#10;预期结果：&#10;1. ..."></textarea></label>
    <div class="meta" id="ca-state"></div>`,
    `<button class="ghost" data-cancel>取消</button>
     <button data-ok>添加</button>`);
  $("[data-cancel]", m.el).addEventListener("click", m.close);
  $("[data-ok]", m.el).addEventListener("click", async () => {
    const st = $("#ca-state", m.el);
    st.textContent = "保存中…";
    try {
      await api("/api/cases", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: $("#ca-title", m.el).value,
          package: $("#ca-pkg", m.el).value,
          input: $("#ca-input", m.el).value,
        }),
      });
      m.close();
      viewCaseLib();
    } catch (e) {
      st.textContent = "保存失败：" + e.message;
    }
  });
}

async function openCaseView(id) {
  const c = await api(`/api/cases/${id}`);
  const m = modal(`用例 #${c.id} · ${c.title || ""}`, `
    <div class="meta" style="margin-bottom:8px">
      模块 ${esc(c.package || "未识别")} · 跑过 ${c.run_count ?? 0} 次 ·
      最近 ${esc(fmtTime(c.last_run_at))} ${statusChip(c.last_status)} ·
      关联记录 ${c.last_session_id ? "#" + c.last_session_id : "—"}
    </div>
    <div class="panel-title">用例原文</div>
    <pre class="case-pre">${esc(c.input || "(空)")}</pre>`,
    `<button class="ghost" data-cancel>关闭</button>
     <button data-edit>修改</button>`);
  $("[data-cancel]", m.el).addEventListener("click", m.close);
  $("[data-edit]", m.el).addEventListener("click", () => {
    m.close();
    openCaseEdit(id);
  });
}

async function openCaseEdit(id) {
  const c = await api(`/api/cases/${id}`);
  const m = modal(`修改用例 #${c.id}`, `
    <label class="fld">标题
      <input id="ce-title" value="${esc(c.title || "")}"></label>
    <label class="fld">模块（包名）
      <input id="ce-pkg" placeholder="如 com.zui.calendar"
             value="${esc(c.package || "")}"></label>
    <label class="fld">用例原文
      <textarea id="ce-input" spellcheck="false">${esc(c.input || "")}</textarea></label>
    <div class="meta" id="ce-state"></div>`,
    `<button class="ghost" data-cancel>取消</button>
     <button data-ok>保存</button>`);
  $("[data-cancel]", m.el).addEventListener("click", m.close);
  $("[data-ok]", m.el).addEventListener("click", async () => {
    $("#ce-state", m.el).textContent = "保存中…";
    await api(`/api/cases/${id}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title: $("#ce-title", m.el).value,
        package: $("#ce-pkg", m.el).value,
        input: $("#ce-input", m.el).value,
      }),
    });
    m.close();
    viewCaseLib();
  });
}

function confirmDeleteCase(id) {
  const m = modal("删除用例", `
    <div class="warn-text">确定删除用例 <b>#${esc(id)}</b> 吗？</div>
    <div class="meta">历史测试记录会保留（仅解除关联），<b>不可恢复</b>。</div>`,
    `<button class="ghost" data-cancel>取消</button>
     <button class="danger" data-ok>确认删除</button>`);
  $("[data-cancel]", m.el).addEventListener("click", m.close);
  $("[data-ok]", m.el).addEventListener("click", async () => {
    await api(`/api/cases/${id}`, { method: "DELETE" });
    m.close();
    viewCaseLib();
  });
}

// ══════════════════ 视觉模型 ══════════════════
async function viewVision() {
  activateNav("vision");
  const c = await api("/api/vision");
  main.innerHTML = `
    <h1>视觉模型</h1>
    <div class="card" style="max-width:620px;cursor:default">
      <div class="kb-bar">
        <b>OpenAI 兼容配置</b>
        <span style="flex:1"></span>
        <span class="muted" id="vs-state"></span>
        <button class="ghost" id="vs-test">测试连接</button>
        <button id="vs-save">保存</button>
      </div>
      <div class="form">
        <label>Base URL<input id="vs-url" placeholder="https://api.example.com/v1" value="${esc(c.base_url || "")}"></label>
        <label>Model<input id="vs-model" placeholder="gpt-4o / qwen-vl-max / glm-4v ..." value="${esc(c.model || "")}"></label>
        <label>API Key<input id="vs-key" type="password" placeholder="${esc(c.api_key_masked || "未配置")}" value=""></label>
        <div class="meta">Key 只显示掩码；留空保存则保留原值。配置文件：storage/vision.json（不入库）。</div>
      </div>
      <div id="vs-result"></div>
    </div>`;

  $("#vs-save").addEventListener("click", async () => {
    const btn = $("#vs-save");
    btn.disabled = true;
    $("#vs-state").textContent = "保存中…";
    await api("/api/vision", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        base_url: $("#vs-url").value,
        model: $("#vs-model").value,
        api_key: $("#vs-key").value,
      }),
    });
    $("#vs-key").value = "";
    $("#vs-state").textContent = "已保存 " + fmtTime(new Date().toISOString());
    btn.disabled = false;
  });

  // 测试连接：先保存当前表单（否则测的是旧配置），再用后端的真实请求验证
  $("#vs-test").addEventListener("click", async () => {
    const btn = $("#vs-test");
    btn.disabled = true;
    const box = $("#vs-result");
    box.innerHTML = `<div class="test-box pending">⏳ 正在用当前配置发起一次真实请求（最多 30s）…</div>`;
    try {
      await api("/api/vision", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          base_url: $("#vs-url").value,
          model: $("#vs-model").value,
          api_key: $("#vs-key").value,
        }),
      });
      const r = await api("/api/vision/test", { method: "POST" });
      if (r.ok) {
        box.innerHTML = `<div class="test-box ok">
          ✅ <b>连接成功</b> · 耗时 ${r.latency_ms}ms · 模型 <code>${esc(r.model)}</code>
          <div class="meta">模型回复：${esc(r.reply || "(空)")}</div>
          ${r.usage ? `<div class="meta">tokens: ${esc(JSON.stringify(r.usage))}</div>` : ""}
        </div>`;
      } else {
        box.innerHTML = `<div class="test-box err">
          ❌ <b>连接失败</b>：${esc(r.error || "未知错误")}
          ${r.detail ? `<div class="meta">响应：${esc(r.detail)}</div>` : ""}
          ${r.hint ? `<div class="meta">💡 ${esc(r.hint)}</div>` : ""}
        </div>`;
      }
    } catch (e) {
      box.innerHTML = `<div class="test-box err">❌ 请求异常：${esc(e.message)}</div>`;
    }
    btn.disabled = false;
  });
}

// ══════════════════ 知识库 ══════════════════
async function viewKnowledge(name) {
  activateNav("knowledge");
  main.innerHTML = "<div class='sub'>加载中…</div>";
  const { items } = await api("/api/knowledge");
  if (!kbCurrent && items.length) kbCurrent = name || items[0].name;
  let editor = "";
  if (kbCurrent) {
    const { content } = await api(`/api/knowledge/${encodeURIComponent(kbCurrent)}`);
    editor = `
      <div class="kb-editor">
        <div class="kb-bar">
          <b>${esc(kbCurrent)}</b>
          <span style="flex:1"></span>
          <span class="muted" id="kb-state"></span>
          <button id="kb-save">保存</button>
        </div>
        <textarea id="kb-text" spellcheck="false">${esc(content)}</textarea>
      </div>`;
  }
  main.innerHTML = `
    <h1>知识库</h1>
    <div class="kb">
      <div class="kb-list">
        ${items.map(i => `
          <div class="card" data-name="${esc(i.name)}" style="${i.name === kbCurrent ? "border-color:var(--accent)" : ""}">
            <div class="title" style="font-size:13.5px">${esc(i.name)}</div>
            <div class="meta">${i.size} 字节 · ${esc(fmtTime(i.mtime))}</div>
          </div>`).join("")}
      </div>
      ${editor}
    </div>`;
  $$(".kb-list .card", main).forEach(el =>
    el.addEventListener("click", () => { kbCurrent = el.dataset.name; viewKnowledge(); }));
  const ta = $("#kb-text");
  if (ta) {
    $("#kb-save").addEventListener("click", async () => {
      const btn = $("#kb-save");
      btn.disabled = true;
      $("#kb-state").textContent = "保存中…";
      await api(`/api/knowledge/${encodeURIComponent(kbCurrent)}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ content: ta.value }),
      });
      $("#kb-state").textContent = "已保存 " + fmtTime(new Date().toISOString());
      btn.disabled = false;
    });
  }
}

activateNav("dashboard");
viewDashboard();
