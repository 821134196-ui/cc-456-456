/* 商务楼快递柜调度 —— 前端应用（原生 JS，无构建） */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const h = (tag, attrs = {}, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on") && typeof v === "function") e.addEventListener(k.slice(2), v);
    else if (v !== null && v !== undefined && v !== false) e.setAttribute(k, v === true ? "" : v);
  }
  for (const k of kids.flat()) {
    if (k == null || k === false) continue;
    e.append(k.nodeType ? k : document.createTextNode(String(k)));
  }
  return e;
};
const esc = s => String(s ?? "").replace(/[&<>"]/g, c =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const fmt = iso => iso ? iso.replace("T", " ").slice(5, 16) : "-";
const SIZE_CN = { S: "小", M: "中", L: "大" };
const STATUS_CN = {
  intransit: "待入柜", stored: "柜内", on_shelf: "人工货架", in_transfer: "转移中",
  ready_return: "待退回", returned: "已退回", sealed: "异常封存", picked_up: "已取件",
};

const state = {
  me: null, meta: null, view: "dashboard",
  lockerId: null, buildingId: "B1", shelfBuilding: "B1",
  flow: null, transfer: null, detailId: null, selectedCell: null,
};

// ---------------- API ----------------
async function api(path, body) {
  const opt = body !== undefined
    ? { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body) }
    : {};
  const r = await fetch(path, opt);
  const data = await r.json().catch(() => ({}));
  if (!r.ok) {
    const e = new Error(data?.error?.message || `HTTP ${r.status}`);
    e.code = data?.error?.code; e.extra = data?.error?.extra;
    throw e;
  }
  return data;
}
function toast(msg, kind = "info", ms = 3600) {
  const t = $("#toast");
  t.className = `toast ${kind}`;
  t.textContent = msg;
  t.classList.remove("hidden");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), ms);
}
const onErr = e => toast(e.message, "err", 5000);

// ---------------- 启动 / 登录 ----------------
async function boot() {
  const me = await api("/api/me").catch(() => null);
  if (me?.actor) { state.me = me.actor; enterApp(); }
  else showLogin();
}
async function showLogin() {
  $("#app").classList.add("hidden");
  $("#login").classList.remove("hidden");
  // 演示身份为内置虚构账号，直接渲染选择列表
  const ids = [
    ["U-SF", "程凯", "顺丰速运 · 快递员", false],
    ["U-YT", "高磊", "圆通速递 · 快递员", false],
    ["U-JD", "韩松", "京东物流 · 快递员", false],
    ["U-FD", "周敏", "物业服务 · 管理员", true],
    ["U-PM", "梁峥", "物业服务 · 审批员", true],
  ];
  const box = $("#login-ids");
  box.innerHTML = "";
  ids.forEach(([id, name, desc, isP]) => {
    box.append(h("button", {
      class: "login-item" + (isP ? " property" : ""),
      onclick: async () => {
        const r = await api("/api/login", { courier_id: id }).catch(onErr);
        if (!r) return;
        state.me = r.actor; enterApp();
        toast(`已登录：${r.actor.name}（${r.actor.company}）`, "ok");
      },
    }, h("b", {}, name), h("span", { class: "tag" }, desc)));
  });
}
async function enterApp() {
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");
  $("#side-who").textContent = `${state.me.name} · ${state.me.company}${
    state.me.role === "property" ? "（物业）" : ""}`;
  await refreshMeta();
  buildNav();
  route();
  tick();
}
async function refreshMeta() {
  state.meta = await api("/api/meta");
}
$("#btn-logout").onclick = async () => {
  await api("/api/logout"); location.reload();
};
$("#btn-refresh").onclick = () => { refreshMeta().then(render).catch(onErr); };

// ---------------- 导航 ----------------
const NAV = [
  ["dashboard", "总览", "▦"],
  ["deliver", "投递入柜", "📦"],
  ["lockers", "柜机格口图", "🗄"],
  ["shelves", "人工货架", "🗂"],
  ["pickup", "取件核验", "✅"],
  ["overdue", "超时队列", "⏳"],
  ["returns", "退回交接", "↩"],
  ["approvals", "强开审批", "🔐"],
  ["packages", "包裹/事件", "📋"],
  ["notifications", "通知记录", "💬"],
];
function buildNav() {
  const nav = $("#nav");
  nav.innerHTML = "";
  const s = state.meta.stats;
  const badges = {
    overdue: s.ready_return, approvals: s.pending_approvals,
  };
  NAV.forEach(([v, label, icon]) => {
    nav.append(h("button", {
      class: "nav-item" + (state.view === v ? " active" : ""),
      onclick: () => { state.view = v; state.flow = null; state.transfer = null;
        state.detailId = null; route(); },
    }, h("span", {}, icon), label,
      badges[v] ? h("span", { class: "badge" }, badges[v]) : null));
  });
  $("#mini-stats").innerHTML =
    `柜内 <b>${s.stored}</b> · 货架 <b>${s.on_shelf}</b><br>` +
    `待退回 <b style="color:#ff8f8f">${s.ready_return}</b> · ` +
    `故障门 <b style="color:#ff8f8f">${s.fault_cells}</b>`;
}
function route() {
  buildNav();
  ({
    dashboard: viewDashboard, deliver: viewDeliver, lockers: viewLockers,
    shelves: viewShelves, pickup: viewPickup, overdue: viewOverdue,
    returns: viewReturns, approvals: viewApprovals, packages: viewPackages,
    notifications: viewNotifications, detail: viewDetail,
  })[state.view]?.();
}
function setCrumb(title, sub = "") {
  $("#crumb").innerHTML = `${esc(title)}<span class="sub">${esc(sub)}</span>`;
}
function render() { route(); }
function tick() {
  const el = $("#clock");
  const update = () => {
    const d = new Date();
    el.textContent = d.toLocaleString("zh-CN", { hour12: false });
  };
  update(); setInterval(update, 1000);
}

// ---------------- 总览 ----------------
async function viewDashboard() {
  setCrumb("调度总览", "楼栋 / 柜机 / 异常一屏掌握");
  const v = $("#view");
  await refreshMeta(); buildNav();
  const s = state.meta.stats;
  const kpis = [
    ["柜内占用", s.stored, ""], ["人工货架", s.on_shelf, "orange"],
    ["待退回", s.ready_return, "red"], ["故障格口门", s.fault_cells, "red"],
    ["异常封存", s.sealed, ""], ["待审批强开", s.pending_approvals, "red"],
    ["今日已取件", s.picked_up, ""], ["开门未关", s.open_cells, "orange"],
  ];
  const lockerCards = state.meta.lockers.map(l => {
    const b = state.meta.buildings.find(x => x.id === l.building_id);
    const cls = l.rate >= 1 ? "full" : l.rate >= 0.8 ? "high" : "mid";
    const card = h("div", { class: "card", style: "cursor:pointer;margin:0" },
      h("div", { class: "flex-between" },
        h("div", {}, h("b", {}, `${l.name} `), h("small", {}, `${b.name} · ${l.id}`)),
        h("span", { class: `rate-pill ${cls}` },
          `满仓率 ${(l.rate * 100).toFixed(0)}%`)),
      h("div", { class: "rate-bar mt8" }, h("i", {
        class: l.rate >= 1 ? "full" : "", style: `width:${l.rate * 100}%` })),
      h("div", { class: "muted mt8", style: "font-size:12px" },
        `${l.used}/${l.n_total} 占用` +
        (l.n_fault ? ` · ${l.n_fault} 门故障` : "") +
        (l.reserved ? ` · ${l.reserved} 格口锁定中` : "")));
    card.onclick = () => { state.lockerId = l.id; state.view = "lockers"; route(); };
    return h("div", { style: "min-width:260px" }, card);
  });
  v.innerHTML = "";
  v.append(
    h("div", { class: "grid cols-4" },
      ...kpis.map(([l, n, c]) => h("div", { class: `kpi ${c}` },
        h("div", { class: "n" }, n), h("div", { class: "l" }, l)))),
    h("div", { class: "card" },
      h("h3", {}, "柜机组（点击查看格口图）"),
      h("div", { style: "display:flex;gap:14px;flex-wrap:wrap" }, ...lockerCards)),
    h("div", { class: "grid cols-2" },
      quickCard("满仓处置演示",
        "B1 海辰国际A座 M1/M3 已满仓。在「投递入柜」扫描 SF90000001，" +
        "走连续路径：本楼货架或邻楼 B2 柜机，每次转移旧码作废、新码短信通知。",
        "去投递", () => { state.view = "deliver"; route(); }),
      quickCard("故障 / 强开演示",
        "M2 的 L01 柜门电机故障、件在柜内，顺丰已提交强开申请；" +
        "切换到物业身份审批后，快递员可凭审批强开取出并转移或封存。",
        "去审批", () => { state.view = "approvals"; route(); })));
}
function quickCard(title, text, btn, fn) {
  return h("div", { class: "card" },
    h("h3", {}, title), h("p", { class: "muted", style: "line-height:1.8" }, text),
    h("button", { class: "btn mt12", onclick: fn }, btn));
}

// ---------------- 柜机格口图（严格比例） ----------------
// 显示尺寸由物理 mm 宽高比换算，同档同尺寸，严格保持大/中/小比例
const CELL_STYLE = {
  // [显示高 px]，宽 = 物理宽高比 × 高
  L: { h: 46 }, M: { h: 32 }, S: { h: 17 },
};
function cellPx(cell) {
  const ht = CELL_STYLE[cell.size].h;
  const w = Math.round(ht * cell.width_mm / cell.height_mm);
  return { width: w + "px", height: ht + "px" };
}
async function viewLockers() {
  setCrumb("柜机格口图", "按物理尺寸等比排布 · 标题旁为满仓率");
  const v = $("#view");
  v.innerHTML = "";
  await refreshMeta();
  const lockers = state.meta.lockers;
  state.lockerId ||= "M1";
  const tabs = h("div", { class: "tabs" }, ...lockers.map(l => {
    const b = state.meta.buildings.find(x => x.id === l.building_id);
    return h("button", {
      class: "tab" + (state.lockerId === l.id ? " active" : ""),
      onclick: () => { state.lockerId = l.id; state.selectedCell = null; route(); },
    }, `${l.id} ${b.name} · ${(l.rate * 100).toFixed(0)}%`);
  }));
  v.append(tabs);
  const wrap = h("div");
  v.append(wrap, h("div", { id: "cell-detail" }));
  await renderLocker(wrap);
}
async function renderLocker(wrap) {
  const l = state.meta.lockers.find(x => x.id === state.lockerId);
  const b = state.meta.buildings.find(x => x.id === l.building_id);
  const cells = await api(`/api/lockers/${l.id}/cells`);
  const cls = l.rate >= 1 ? "full" : l.rate >= 0.8 ? "high" : "mid";
  wrap.innerHTML = "";
  wrap.append(
    h("div", { class: "card" },
      h("div", { class: "locker-head" },
        h("h2", {}, `${l.id} · ${l.name}`),
        h("small", {}, `${b.name}`),
        h("span", { class: `rate-pill ${cls}` },
          `满仓率 ${(l.rate * 100).toFixed(0)}%（${l.used}/${l.n_total}）`),
        l.n_fault ? h("span", { class: "chip fault" }, `${l.n_fault} 门故障`) : null,
        h("span", { class: "muted", style: "font-size:12px" },
          "大格 550×320 · 中格 400×220 · 小格 300×110 mm，图中严格等比")),
      h("div", { class: "rate-bar" }, h("i", {
        class: l.rate >= 1 ? "full" : "", style: `width:${l.rate * 100}%` })),
      buildLockerFrame(cells)),
    h("div", { class: "card" },
      h("h3", {}, "操作提示",
        h("span", { class: "hint" },
          "点击格口查看占用包裹、上报故障 / 申请强开；开门未关闭的格口不可分配")),
      h("p", { class: "muted", style: "line-height:1.8;font-size:13px" },
        "橙色＝本公司包裹；灰色＝占用中；斜纹＝柜门故障；黄虚线＝开门未关闭；" +
        "橙内框＝格口已被快递员锁定（争用时仅一人持锁）。")));
  if (state.selectedCell) renderCellDetail(cells.find(c => c.id === state.selectedCell) || cells[0]);
}
function buildLockerFrame(cells) {
  const bands = ["L", "M", "S"].map(size => {
    const list = cells.filter(c => c.size === size);
    const row = h("div", { class: "cell-row" }, ...list.map(c => buildCell(c)));
    return h("div", { class: "size-band" },
      h("div", { class: "band-label" },
        h("span", {}, `${SIZE_CN[size]}格口区（${c_dims(size)}）`),
        h("span", {}, `${list.filter(x => x.pkg_id).length}/${list.length} 占用`)),
      row);
  });
  function c_dims(s) { return `物理 ${list0(s).width_mm}×${list0(s).height_mm}mm`; }
  function list0(s) { return cells.find(x => x.size === s) || { width_mm: 0, height_mm: 0 }; }
  return h("div", { class: "locker-frame mt12" },
    h("div", { class: "locker-screen" },
      `SMART LOCKER ${state.lockerId} · DOOR LOCK v3 · 关门回执生效`),
    ...bands,
    h("div", { class: "legend" },
      h("span", { class: "lg-free" }, "空闲"),
      h("span", { class: "lg-occ" }, "占用"),
      h("span", { class: "lg-owned" }, "本公司件"),
      h("span", { class: "lg-fault" }, "柜门故障"),
      h("span", { class: "lg-open" }, "开门未关")));
}
function buildCell(c) {
  const owned = c.pkg_id && state.me.role === "courier" && c.company === state.me.company;
  const cls = ["cell"];
  if (c.door_status === "fault") cls.push("fault");
  else if (c.door_status === "open") cls.push("open");
  else if (c.pkg_id) cls.push(owned ? "owned" : "occupied");
  if (c.locked_by) cls.push("locked");
  const icon = c.door_status === "fault" ? "⛔" : c.door_status === "open" ? "🔓"
    : c.locked_by ? "🔒" : c.pkg_id ? "📦" : "";
  const el = h("div", { class: cls.join(" "), style: cellPx(c),
    title: cellTitle(c) },
    h("span", { class: "cicon" }, icon),
    h("div", { class: "ccode" }, c.code),
    h("div", { class: "cmeta" },
      c.door_status === "fault" ? "故障"
        : c.door_status === "open" ? "门开着"
        : c.pkg_id ? (owned ? c.company : "占用")
        : c.locked_by ? "锁定" : "空闲"));
  el.onclick = () => { state.selectedCell = c.id;
    renderCellDetail(c); };
  return el;
}
function cellTitle(c) {
  return `${c.code}（${SIZE_CN[c.size]}）${c.width_mm}×${c.height_mm}×${c.depth_mm}mm` +
    (c.waybill ? `｜${c.company} ${c.waybill}` : "") +
    (c.locked_by_name ? `｜锁定：${c.locked_by_name}` : "");
}
function renderCellDetail(c) {
  if (!c) return;
  const box = $("#cell-detail");
  box.innerHTML = "";
  const card = h("div", { class: "card" },
    h("h3", {}, `格口 ${c.code}`,
      h("span", { class: "hint" },
        `${SIZE_CN[c.size]}格 · ${c.width_mm}×${c.height_mm}×${c.depth_mm}mm · ` +
        `门状态：${{ closed: "已关闭", open: "打开未关闭", fault: "故障" }[c.door_status]}`)));
  if (c.pkg_id) {
    const mine = state.me.role === "property" || c.company === state.me.company;
    card.append(h("div", { class: "detail-grid mt8" },
      h("dt", {}, "运单"), h("dd", { class: "mono" }, mine ? c.waybill : "（他司包裹，已隐藏）"),
      h("dt", {}, "公司"), h("dd", {}, mine ? c.company : "—"),
      h("dt", {}, "时限"), h("dd", {}, mine ? fmt(c.extension_until || c.deadline_at) : "—")));
    if (mine) {
      card.append(h("div", { class: "row mt12" },
        h("button", { class: "btn navy sm", onclick: () => openDetail(c.pkg_id) },
          "包裹详情 / 转移"),
        c.door_status === "fault"
          ? h("button", {
              class: "btn sm",
              onclick: () => api("/api/force-open", { cell_id: c.id,
                reason: "柜门故障，件在柜内，申请物业强制开柜" })
                .then(r => { toast(`强开申请 #${r.approval_id} 已提交，等待物业审批`); refreshMeta().then(render); })
                .catch(onErr),
            }, "申请强开")
          : h("button", { class: "btn ghost sm",
            onclick: () => api("/api/cells/report-fault", { cell_id: c.id, note: "人工上报柜门故障" })
              .then(() => { toast("已上报故障"); state.selectedCell = c.id;
                refreshMeta().then(route); })
              .catch(onErr) }, "上报故障")));
    } else {
      card.append(h("div", { class: "warn-box" }, "公司隔离：普通快递员不可查看其他公司包裹详情。"));
    }
  } else {
    card.append(h("p", { class: "muted" }, "空闲格口。"));
    const actions = [h("button", {
      class: "btn ghost sm",
      onclick: () => api("/api/cells/report-fault", { cell_id: c.id, note: "巡检发现柜门异常" })
        .then(() => { toast("已上报故障"); refreshMeta().then(route); }).catch(onErr),
    }, "上报柜门故障")];
    if (state.me.role === "property") {
      actions.push(h("button", {
        class: "btn navy sm", disabled: c.door_status !== "fault",
        onclick: () => api(`/api/cells/${c.id}/repair`, {})
          .then(() => { toast("维修完成，门已复位关闭", "ok"); refreshMeta().then(route); })
          .catch(onErr),
      }, "维修结案（物业）"));
    }
    card.append(h("div", { class: "row mt12" }, ...actions));
  }
  box.append(card);
}

// ---------------- 投递入柜（连续路径，无多层弹窗） ----------------
async function viewDeliver() {
  setCrumb("投递入柜", "扫描 → 去向建议 → 开门投递 → 关门回执");
  const v = $("#view");
  v.innerHTML = "";
  if (state.flow) return renderFlow(v);
  const buildings = state.meta.buildings;
  v.append(h("div", { class: "card" },
    h("h3", {}, "① 扫描运单（虚构演示运单）"),
    h("p", { class: "muted", style: "font-size:12px" },
      "演示：SF90000001（顺丰大件，B1 满仓）、YT66000099（货架件）、" +
      "YT77000011 / JD77000022（超时件）。重复扫码会被拦截。"),
    h("div", { class: "row" },
      h("input", { id: "f-waybill", placeholder: "扫描 / 输入运单号，如 SF90000001",
        style: "max-width:340px", onkeydown: e => e.key === "Enter" && doScan() }),
      h("select", { id: "f-building" },
        ...buildings.map(b => h("option", { value: b.id }, `${b.id} ${b.name}`))),
      h("button", { class: "btn", onclick: doScan }, "扫描"),
      h("button", { class: "btn ghost", onclick: toggleRegister }, "录入新包裹")),
    h("div", { id: "register-box", class: "hidden mt12" })));
  $("#f-building").value = state.buildingId;
}
function toggleRegister() {
  const box = $("#register-box");
  box.classList.toggle("hidden");
  if (box.children.length) return;
  box.append(h("div", { class: "info-box" }, "新包裹需绑定收件人代号（虚构）。尺寸单位 mm。"),
    h("div", { class: "row" },
      fld("运单号", h("input", { id: "r-wb", value: "SF9" + String(Date.now()).slice(-6) })),
      fld("收件人代号", h("select", { id: "r-rcode" },
        ...state.meta.recipients.map(r => h("option", { value: r.code },
          `${r.code} ${r.name}`)))),
      fld("尺寸", h("select", { id: "r-size" },
        h("option", { value: "S" }, "小 S"), h("option", { value: "M" }, "中 M"),
        h("option", { value: "L", selected: true }, "大 L"))),
      fld("宽mm", h("input", { id: "r-w", value: "500", type: "number" })),
      fld("高mm", h("input", { id: "r-h", value: "280", type: "number" })),
      fld("深mm", h("input", { id: "r-d", value: "460", type: "number" })),
      h("button", {
        class: "btn navy", onclick: async () => {
          const r = await api("/api/packages/register", {
            waybill: $("#r-wb").value, recipient_code: $("#r-rcode").value,
            size: $("#r-rcode") && $("#r-size").value,
            w: +$("#r-w").value, h: +$("#r-h").value, d: +$("#r-d").value,
          }).catch(onErr);
          if (!r) return;
          toast("包裹已录入并扫描", "ok");
          startFlow(r.package.id, $("#f-building").value);
        },
      }, "录入并开始投递")));
}
function fld(label, ...inp) {
  return h("div", {}, h("label", {}, label), ...inp);
}
async function doScan() {
  const wb = $("#f-waybill").value.trim();
  const bid = $("#f-building").value;
  state.buildingId = bid;
  if (!wb) return toast("请扫描运单号");
  const r = await api("/api/scan", { waybill: wb }).catch(e => {
    if (["DUP_STORED", "DUP_SHELF", "DUP_CLOSED", "IN_TRANSFER"].includes(e.code))
      toast(e.message, "err", 5000);
    else onErr(e);
    return null;
  });
  if (!r) return;
  await startFlow(r.package.id, bid);
}
async function startFlow(pkgId, bid) {
  const suggestion = await api("/api/suggest", { pkg_id: pkgId, building_id: bid });
  state.flow = { phase: "suggest", pkgId, bid, suggestion, log: [] };
  route();
}
async function renderFlow(v) {
  const f = state.flow;
  const detail = await api(`/api/packages/${f.pkgId}`);
  const p = detail.package;
  setCrumb("投递入柜 · 连续路径",
    `${p.waybill}｜${SIZE_CN[p.size]}件 ${p.width_mm}×${p.height_mm}×${p.depth_mm}mm`);
  v.innerHTML = "";
  const steps = ["扫描匹配", "去向（满仓转移）", "门锁动作", "关门回执 / 通知"];
  const phaseIdx = { suggest: 1, acquire: 2, done: 3, shelf: 3, neighbor: 2 }[f.phase] ?? 1;
  v.append(h("div", { class: "flow" }, ...steps.map((s, i) =>
    h("div", { class: "flow-step " + (i < phaseIdx ? "done" : i === phaseIdx ? "active" : "") },
      h("span", { class: "step-no" }, i + 1), h("h4", {}, s)))));

  const body = h("div", { class: "card mt12" });
  v.append(body, h("div", { class: "card" }, h("h3", {}, "包裹事件轨迹"),
    timeline(detail.events)));
  if (f.phase === "suggest") renderSuggest(body);
  if (f.phase === "acquire") renderAcquire(body);
  if (f.phase === "neighbor") renderNeighbor(body);
  if (f.phase === "done") renderDone(body, p);
}
function renderSuggest(body) {
  const f = state.flow, sg = f.suggestion;
  body.append(h("h3", {}, "② 系统按“最小可用格口”匹配，满仓时给出替代去向"));
  if (sg.cell) {
    const c = sg.cell;
    body.append(optRow(`本楼柜机：${SIZE_CN[c.size]}格 ${c.code}`,
      `${c.width_mm}×${c.height_mm}×${c.depth_mm}mm｜最小可容纳格口`,
      "开门投递", () => acquire(c.id)));
  } else {
    body.append(h("div", { class: "warn-box" },
      "本楼全部柜机无可用格口（满仓 / 故障 / 开门未关闭均不可分配），请选择替代去向："));
  }
  if (sg.shelf_slots.length) {
    body.append(h("h3", { class: "mt12" }, "转入同楼人工货架"));
    sg.shelf_slots.slice(0, 4).forEach(s =>
      body.append(optRow(`货架 ${s.rack_no}架 ${s.level_no}层 ${String(s.slot_no).padStart(2, "0")}位`,
        "高密度定位：架号 / 层号 / 位号，前台凭证取件",
        "转入货架", () => toShelf(s.id))));
  }
  if (sg.neighbor_cells.length) {
    body.append(h("h3", { class: "mt12" }, "转入邻楼柜机"));
    sg.neighbor_cells.forEach(c =>
      body.append(optRow(`邻楼 ${c.building_name}：${SIZE_CN[c.size]}格 ${c.code}`,
        `${c.width_mm}×${c.height_mm}×${c.depth_mm}mm｜跨楼转运，旧码作废`,
        "转运邻楼", () => acquire(c.id, true))));
  }
  if (!sg.cell && !sg.shelf_slots.length && !sg.neighbor_cells.length)
    body.append(h("div", { class: "warn-box" }, "全区域无容量：请在包裹详情中做异常封存。"));
  body.append(h("div", { class: "muted mt12", style: "font-size:12px" },
    "每次转移都会作废旧取件码、生成新码并下发新短信；转移记录全程留痕。"));
}
function optRow(title, sub, btn, fn, disabled) {
  return h("div", { class: "option" + (disabled ? " disabled" : "") },
    h("div", {}, h("b", {}, title), h("div", { class: "muted", style: "font-size:12px" }, sub)),
    h("button", { class: "btn sm", disabled: !!disabled, onclick: fn }, btn));
}
function doorLog(lines) {
  return h("div", { class: "door-anim" }, ...lines.map(([t, ok]) =>
    h("div", { class: ok === 1 ? "ok" : ok === 0 ? "fail" : "" },
      (ok === 1 ? "✔ " : ok === 0 ? "✘ " : "» ") + t)));
}
async function acquire(cellId, neighbor) {
  const f = state.flow;
  f.phase = neighbor ? "neighbor" : "acquire";
  f.targetCell = cellId;
  route();
}
function renderAcquire(body) {
  const f = state.flow;
  const log = h("div");
  body.append(h("h3", {}, "③ 取得格口锁 → 门锁适配器开门"),
    log,
    h("div", { class: "row" },
      h("button", { class: "btn", id: "btn-put",
        onclick: () => doAcquire(body) }, "放入包裹并关门"),
      h("button", { class: "btn ghost", onclick: () => doAbort() }, "取回，放弃本格口")));
  doAcquireLock(body, log);
}
function renderNeighbor(body) {
  const f = state.flow;
  const log = h("div");
  body.append(h("h3", {}, "③ 跨楼转运：源柜取出 → 邻楼目标格口开门"),
    h("div", { class: "info-box" },
      "满仓转移采用连续路径：系统先作废旧码，包裹标记“转移中”，到达邻楼关门回执后生成新码。"),
    log,
    h("div", { class: "row" },
      h("button", { class: "btn", onclick: () => doNeighbor(body, log) },
        "已送达邻楼，放入并关门"),
      h("button", { class: "btn ghost", onclick: () => { state.flow.phase = "suggest"; route(); } },
        "返回重选去向")));
  doNeighbor(body, log, true);
}
async function doAcquireLock(body, log) {
  const f = state.flow;
  log.innerHTML = "";
  log.append(doorLog([["请求格口互斥锁…", null]]));
  const r = await api("/api/cells/acquire", { pkg_id: f.pkgId, cell_id: f.targetCell })
    .catch(e => {
      log.append(doorLog([[e.message, 0]]));
      if (e.code === "CELL_LOCKED") {
        log.append(doorLog([["格口竞争：另一名快递员持锁，本次分配失败", 0],
          ["请改选其他格口或等待释放", null]]));
      }
      body.append(h("div", { class: "row mt12" },
        h("button", { class: "btn navy", onclick: () => { state.flow = null; route(); } },
          "返回扫描")));
      return null;
    });
  if (!r) return;
  f.cellId = r.cell_id;
  log.append(doorLog([[`格口锁已获得：${r.cell_id}`, 1],
    [`门锁回执：DOOR OPEN ${r.door.result}（${r.door.detail}）`, 1],
    ["柜门已弹开，请放入包裹…", null]]));
}
async function doAcquire(body) {
  const f = state.flow;
  const r = await api(`/api/packages/${f.pkgId}/close`, {})
    .catch(e => { onErr(e);
      if (e.code === "DOOR_FAULT_AT_CLOSE") { state.flow = null; route(); }
      return null; });
  if (!r) return;
  f.phase = "done"; f.result = r; route();
}
async function doAbort() {
  const f = state.flow;
  await api(`/api/packages/${f.pkgId}/abort`, {}).catch(() => {});
  state.flow = null; route();
}
async function doNeighbor(body, log, dryRun) {
  const f = state.flow;
  if (dryRun) {
    log.innerHTML = "";
    log.append(doorLog([["正在建立转移单（目标：邻楼格口）…", null]]));
    const r = await api(`/api/packages/${f.pkgId}/transfer`, {
      target_type: "cell", target_id: f.targetCell, reason: "full",
    }).catch(e => { log.append(doorLog([[e.message, 0]])); return null; });
    if (!r) return;
    f.transferOpen = true;
    log.append(doorLog([["旧取件码已作废，包裹状态：转移中", 1],
      ["源柜门已关闭、目标格口已开门", 1], ["请将包裹送至邻楼…", null]]));
    return;
  }
  const r = await api(`/api/packages/${f.pkgId}/transfer-close`, {})
    .catch(onErr);
  if (!r) return;
  f.phase = "done"; f.result = r; route();
}
async function toShelf(slotId) {
  const f = state.flow;
  const r = await api(`/api/packages/${f.pkgId}/transfer`, {
    target_type: "shelf", target_id: slotId, reason: "full",
  }).catch(onErr);
  if (!r) return;
  f.phase = "done"; f.result = r; route();
}
function renderDone(body, p) {
  const r = state.flow.result || {};
  body.append(h("h3", {}, "④ 关门回执成功，入柜 / 转移完成"));
  const log = doorLog([["DOOR CLOSE → success，回执已落库", 1]]);
  body.append(log);
  if (r.cell_id) body.append(h("p", {}, "格口：", h("b", { class: "mono" }, r.cell_id)));
  if (r.slot) body.append(h("p", {}, "货架位：",
    h("b", {}, `${r.slot.rack_no}架 ${r.slot.level_no}层 ${String(r.slot.slot_no).padStart(2, "0")}位`)));
  if (r.old_code_invalid)
    body.append(h("div", { class: "info-box" },
      `旧取件码 ${r.old_code_invalid} 已作废（扫码即失效）。`));
  body.append(
    h("p", { class: "mt8" }, "新取件码：", h("span", { class: "code-big" }, r.pickup_code)),
    h("p", { class: "muted", style: "font-size:12px" },
      "短信适配器已生成新通知（入柜/转移通知），网关失败会自动重试。"),
    h("div", { class: "row mt12" },
      h("button", { class: "btn navy", onclick: () => openDetail(p.id) }, "查看包裹详情"),
      h("button", { class: "btn ghost", onclick: () => { state.flow = null; route(); } },
        "继续扫描下一件")));
}

// ---------------- 包裹详情 + 转移连续路径 ----------------
async function openDetail(pkgId) {
  if (state.view !== "detail") state._fromView = state.view;
  state.detailId = pkgId; state.view = "detail";
  state.flow = null; state.transfer = null;
  route();
}
async function viewDetail() {
  const v = $("#view");
  const d = await api(`/api/packages/${state.detailId}`).catch(e => {
    toast(e.message, "err"); state.view = "packages"; route(); return null; });
  if (!d) return;
  const p = d.package, r = d.recipient;
  setCrumb("包裹详情", p.waybill);
  v.innerHTML = "";
  const head = h("div", { class: "card" },
    h("div", { class: "flex-between" },
      h("div", {},
        h("h3", { style: "font-size:16px;margin:0" },
          h("span", { class: "mono" }, p.waybill), " ",
          h("span", { class: `chip ${p.status}` }, STATUS_CN[p.status])),
        h("div", { class: "muted mt8" },
          `${p.company}｜${SIZE_CN[p.size]}件 ${p.width_mm}×${p.height_mm}×${p.depth_mm}mm`)),
      h("button", { class: "btn ghost sm", onclick: () => {
        state.detailId = null; state.view = state._fromView || "packages";
        state._fromView = null; route(); } }, "返回")));
  v.append(head);
  if (state.transfer) { v.append(await transferPanel(p)); }
  else v.append(actionPanel(d), chainCard(d),
    h("div", { class: "grid cols-2" },
      h("div", { class: "card" }, h("h3", {}, "交接记录"),
        ...(d.handovers.length ? d.handovers.map(ho => h("div", {
          style: "font-size:12px;border-bottom:1px solid #eef1f5;padding:6px 0" },
          h("b", {}, ho.kind), " ", fmt(ho.ts), h("div", { class: "muted" },
            `${ho.from_party} → ${ho.to_party}${ho.receiver_name ? " · " + ho.receiver_name : ""}` +
            `${ho.evidence_path ? " · 凭证已留存本地目录" : ""}`)))
          : [h("p", { class: "muted" }, "无")])),
      h("div", { class: "card" }, h("h3", {}, "通知记录"),
        ...(d.notifications.length ? d.notifications.map(n => h("div", {
          style: "font-size:12px;border-bottom:1px solid #eef1f5;padding:6px 0" },
          h("b", {}, n.kind), " ",
          h("span", { class: `chip ${n.status === "sent" ? "stored" : "ready_return"}` },
            n.status === "sent" ? "已送达" : `重试${n.retries}次`),
          h("div", { class: "muted" }, n.content))) : [h("p", { class: "muted" }, "无")]))),
    h("div", { class: "card" }, h("h3", {}, "事件流水（SQLite package_events）"),
      timeline(d.events)));
}
function actionPanel(d) {
  const p = d.package, me = state.me;
  const card = h("div", { class: "card" },
    h("h3", {}, "收件人与存放"),
    h("dl", { class: "detail-grid" },
      h("dt", {}, "收件人代号"), h("dd", {}, `${r_code(d)} ${d.recipient.name}`),
      h("dt", {}, "手机号"), h("dd", { class: "mono" }, d.recipient.phone),
      h("dt", {}, "当前位置"), h("dd", {}, p.current_cell_id
        ? `格口 ${p.current_cell_id}` : p.shelf_id
          ? `货架 ${p.shelf_id}` : "—"),
      h("dt", {}, "取件码"), h("dd", {}, p.pickup_code
        ? h("span", { class: "mono" }, `${p.pickup_code}（版本 v${p.code_seq}）`) : "—"),
      h("dt", {}, "存放时限"), h("dd", {},
        fmt(p.extension_until || p.deadline_at) +
        (p.extension_until ? "（已延期）" : ""))));
  const btns = [];
  if (["stored", "on_shelf", "ready_return", "in_transfer"].includes(p.status)) {
    btns.push(h("button", { class: "btn", onclick: () => startTransfer(p.id) },
      p.status === "in_transfer" ? "选择转移去向" : "满仓 / 故障转移"));
  }
  if (["stored", "on_shelf"].includes(p.status)) {
    btns.push(h("button", { class: "btn ghost sm", onclick: () => doExtend(p.id) },
      "收件人申请延期"));
    btns.push(h("button", { class: "btn ghost sm", onclick: () => makeAuth(p.id) },
      "登记前台代取授权"));
    btns.push(h("button", { class: "btn ghost sm", onclick: () => markMis(p.id) },
      "标记错投"));
  }
  if (p.status === "ready_return") {
    btns.push(h("button", { class: "btn navy", onclick: () => doReturn(p.id) },
      "退回交接签收"));
    btns.push(h("button", { class: "btn danger", onclick: () => doReject(p.id) },
      "退回拒收（留凭证）"));
  }
  if (["on_shelf", "ready_return", "in_transfer", "intransit"].includes(p.status))
    btns.push(h("button", { class: "btn danger", onclick: () => doSeal(p.id) },
      "异常封存"));
  if (p.status === "stored" && p.current_cell_id)
    btns.push(h("button", { class: "btn ghost sm",
      onclick: () => api("/api/cells/report-fault",
        { cell_id: p.current_cell_id, note: "详情页上报" })
        .then(() => { toast("已上报故障，可申请强开"); route(); }).catch(onErr) },
      "上报柜门故障"));
  if (p.status === "in_transfer" && !p.current_cell_id)
    card.append(h("div", { class: "warn-box" }, "包裹已从源柜取出，等待选择去向（转移 / 封存）。"));
  if (btns.length) card.append(h("div", { class: "row mt12" }, ...btns));
  return card;
}
function r_code(d) { return d.recipient.code; }
function chainCard(d) {
  if (!d.transfers.length) return h("div");
  return h("div", { class: "card" },
    h("h3", {}, "转移链（每跳旧码作废 → 新码通知）"),
    h("div", { class: "flow" }, ...d.transfers.map(t =>
      h("div", { class: "flow-step done" },
        h("div", { style: "font-size:12px" },
          h("b", {}, { cell: "柜机格口", shelf: "人工货架" }[t.to_type] || t.to_type),
          h("div", { class: "muted" },
            `原因：${{ full: "满仓", fault: "柜门故障", misdelivery: "错投", manual: "人工" }[t.reason] || t.reason}`),
          h("div", { class: "mono", style: "font-size:11px;margin-top:4px" },
            `${t.old_code || "-"} → ${t.new_code || "待关门"}`),
          h("div", { class: "muted", style: "font-size:11px" },
            `${t.from_ref} → ${t.to_ref}`))))));
}
function timeline(events) {
  return h("div", { class: "timeline" }, ...[...events].reverse().map(e =>
    h("div", { class: "tl-item" },
      h("div", {}, h("b", {}, e.atype), " · ", h("span", { class: "tl-ts" }, fmt(e.ts))),
      h("div", { class: "muted" },
        [e.cell_id && `格口 ${e.cell_id}`, e.old_code && `旧码 ${e.old_code} 作废`,
        e.new_code && `新码 ${e.new_code}`].filter(Boolean).join("｜")))));
}

// -- 详情页动作 --
async function doExtend(pid) {
  const hours = +(prompt("申请延期小时数（1-72）", "12") || 0);
  if (!hours) return;
  const r = await api(`/api/packages/${pid}/extend`, { hours }).catch(onErr);
  if (r) { toast(`已延期至 ${fmt(r.extension_until)}`, "ok"); route(); }
}
async function makeAuth(pid) {
  const name = prompt("代取人姓名（虚构）", "代取-许助理");
  if (!name) return;
  const phone = prompt("代取人手机号（虚构）", "13700090001");
  if (!phone) return;
  const r = await api(`/api/packages/${pid}/authorization`,
    { proxy_name: name, proxy_phone: phone }).catch(onErr);
  if (r) { toast(`授权码 ${r.auth_code} 已登记，前台取件须核对一致`, "ok", 6000); route(); }
}
async function markMis(pid) {
  const correct = prompt("正确楼栋代号（留空仅标记）：B1 / B2 / B3 / B4", "B2");
  const r = await api(`/api/packages/${pid}/misdelivery`,
    { correct_building_id: correct || null, note: "前台发现错投" }).catch(onErr);
  if (r) { toast("错投已处理：" + JSON.stringify(r.misdelivery), "ok"); route(); }
}
async function doReturn(pid) {
  const name = prompt("退回交接签收人（寄件方/转运司机，虚构）", "顺丰转运-冯师傅");
  if (!name) return;
  const sign = prompt("签收签名", name);
  if (sign === null) return;
  const r = await api(`/api/packages/${pid}/return`,
    { receiver_name: name, sign }).catch(onErr);
  if (r) { toast("退回交接完成", "ok"); refreshMeta().then(route); }
}
async function doReject(pid) {
  const note = prompt("拒收说明", "外包装无退回标识，拒收待核实");
  if (!note) return;
  const r = await api(`/api/packages/${pid}/reject`, { note }).catch(onErr);
  if (r) { toast(`已拒收并留存凭证：${r.evidence}`, "err", 6000); route(); }
}
async function doSeal(pid) {
  const reason = prompt("封存原因", "包裹破损渗液");
  if (!reason) return;
  const sign = prompt("经办人签名", state.me.name);
  if (sign === null) return;
  const r = await api(`/api/packages/${pid}/seal`,
    { reason, sign, note: "前台异常处理" }).catch(onErr);
  if (r) { toast(`已异常封存，凭证：${r.evidence}`, "ok", 6000); refreshMeta().then(route); }
}

// -- 转移连续路径（从详情发起） --
async function startTransfer(pid) {
  state.transfer = { phase: "pick", targetCell: null };
  route();
}
async function transferPanel(p) {
  const f = state.transfer;
  const card = h("div", { class: "card" });
  const sg = await api("/api/suggest", { pkg_id: p.id,
    building_id: p.building_id || state.buildingId }).catch(onErr);
  if (!sg) { state.transfer = null; return h("div"); }
  if (f.phase === "pick") {
    card.append(h("h3", {}, "转移连续路径：选择去向（源门故障时须先有强开审批）"));
    if (p.current_cell_id) {
      const cell = await api(`/api/cells/${p.current_cell_id}`).catch(() => null);
      if (cell?.door_status === "fault") {
        const ap = await api("/api/approvals?status=approved");
        const mine = ap.find(a => a.package_id === p.id);
        if (mine) { f.approval = mine.id; card.append(h("div", { class: "info-box" },
          `将使用已批准强开单 #${mine.id}`)); }
        else card.append(h("div", { class: "warn-box" },
          "源格口柜门故障：请先在「强开审批」提交申请并由物业批准，再回本页转移。"));
      }
    }
    sg.shelf_slots.slice(0, 3).forEach(s =>
      card.append(optRow(`人工货架 ${s.rack_no}架${s.level_no}层${String(s.slot_no).padStart(2, "0")}位`,
        "同楼", "转货架", () => runTransfer(p, "shelf", s.id))));
    if (sg.cell) card.append(optRow(`本楼格口 ${sg.cell.code}（${SIZE_CN[sg.cell.size]}）`,
      "同楼重新分配", "转本楼格口", () => runTransfer(p, "cell", sg.cell.id)));
    sg.neighbor_cells.forEach(c => card.append(optRow(
      `邻楼 ${c.building_name} ${c.code}（${SIZE_CN[c.size]}）`, "跨楼",
      "转邻楼格口", () => runTransfer(p, "cell", c.id))));
    card.append(h("button", { class: "btn ghost sm mt8",
      onclick: () => { state.transfer = null; route(); } }, "取消转移"));
  }
  if (f.phase === "open") {
    card.append(h("h3", {}, "转移进行中"),
      doorLog([[`源格口已开门${f.approval ? "（强开）" : ""}，旧码 ${f.oldCode} 已作废`, 1],
        ["包裹取出、源门已关；目标格口已开门", 1], ["等待关门回执…", null]]),
      h("button", { class: "btn", onclick: async () => {
        const r = await api(`/api/packages/${p.id}/transfer-close`, {}).catch(onErr);
        if (r) { state.transfer = null; toast(`转移完成，新码 ${r.pickup_code}`, "ok", 6000);
          refreshMeta().then(route); }
      } }, "关门回执，完成转移"),
      h("button", { class: "btn ghost", onclick: () => doSeal(p.id)
        .then(() => { state.transfer = null; }) }, "目标异常，改封存"));
  }
  return card;
}
async function oneCell(cid) {
  return api(`/api/cells/${cid}`);
}
async function runTransfer(p, type, tid) {
  const f = state.transfer;
  const r = await api(`/api/packages/${p.id}/transfer`, {
    target_type: type, target_id: tid, reason: "manual",
    approval_id: f.approval || null,
  }).catch(e => {
    if (e.code === "NEED_FORCE_OPEN") toast(e.message, "err", 5000);
    else onErr(e);
    return null;
  });
  if (!r) return;
  if (r.await_close) {
    f.phase = "open"; f.oldCode = r.old_code; route();
  } else {
    state.transfer = null;
    toast(`已转入货架，旧码 ${r.old_code_invalid} 作废，新码 ${r.pickup_code}`, "ok", 7000);
    refreshMeta().then(route);
  }
}

// ---------------- 人工货架（高密度定位） ----------------
async function viewShelves() {
  setCrumb("人工货架", "按架号 / 层号 / 位号高密度定位");
  const v = $("#view");
  v.innerHTML = "";
  const bs = state.meta.buildings;
  v.append(h("div", { class: "tabs" }, ...bs.map(b =>
    h("button", {
      class: "tab" + (state.shelfBuilding === b.id ? " active" : ""),
      onclick: () => { state.shelfBuilding = b.id; route(); },
    }, `${b.id} ${b.name}`))));
  const slots = await api(`/api/shelves?building=${state.shelfBuilding}`);
  const racks = {};
  slots.forEach(s => (racks[s.rack_no] ||= []).push(s));
  const wrap = h("div", { class: "grid cols-2" });
  Object.entries(racks).forEach(([rack, list]) => {
    const levels = {};
    list.forEach(s => (levels[s.level_no] ||= []).push(s));
    const rackEl = h("div", { class: "shelf-rack" },
      h("h4", {}, `${rack} 架`,
        h("span", { class: "muted", style: "font-size:11px" },
          `${list.filter(s => s.occupied_pkg).length}/${list.length} 位占用`)),
      ...Object.entries(levels).sort((a, b) => a[0] - b[0]).map(([lv, ss]) =>
        h("div", { class: "shelf-level" },
          h("div", { class: "level-no" }, `${lv}层`),
          ...ss.sort((a, b) => a.slot_no - b.slot_no).map(s => {
            const mine = s.pkg_id && state.me.role === "courier"
              && s.company === state.me.company;
            const el = h("div", {
              class: "shelf-slot" + (s.occupied_pkg ? " occ" : "")
                + (mine ? " owned" : ""),
              title: s.occupied_pkg
                ? (mine ? `${s.waybill} 取件码 ${s.pickup_code}` : "他司包裹")
                : "空位",
            }, h("span", { class: "pos" }, `${String(s.slot_no).padStart(2, "0")}位`),
              s.occupied_pkg
                ? h("span", { class: "wb mono" }, mine ? s.waybill : "他司件")
                : h("span", { class: "muted", style: "font-size:10px" }, "空"));
            if (s.occupied_pkg) el.onclick = async () => {
              const d = await api("/api/packages?status=on_shelf");
              const pkg = d.find(x => x.shelf_id === s.id);
              if (pkg) openDetail(pkg.id);
            };
            return el;
          }))));
    wrap.append(rackEl);
  });
  v.append(wrap);
}

// ---------------- 取件核验 ----------------
async function viewPickup() {
  setCrumb("取件核验", "本人取件 / 前台代取须核对授权；旧码一律失效");
  const v = $("#view");
  v.innerHTML = "";
  v.append(h("div", { class: "card" },
    h("h3", {}, "输入取件码"),
    h("div", { class: "row" },
      h("input", { id: "p-code", class: "mono", placeholder: "6 位取件码",
        style: "max-width:220px;letter-spacing:3px",
        onkeydown: e => e.key === "Enter" && verifyCode() }),
      h("button", { class: "btn", onclick: verifyCode }, "核验")),
    h("p", { class: "muted mt8", style: "font-size:12px" },
      "演示码：货架 YT66000099 = 446688；其历史码 112233 / 224466 已作废。")));
  v.append(h("div", { id: "p-box" }));
}
async function verifyCode() {
  const code = $("#p-code").value.trim();
  const box = $("#p-box");
  box.innerHTML = "";
  const r = await api("/api/verify-code", { code }).catch(e => {
    box.append(h("div", { class: "card" }, h("div", { class: "warn-box" },
      e.code === "BAD_CODE"
        ? "取件码错误或已失效：转移后旧码立即作废，请使用最新短信中的新码。"
        : e.message)));
    return null;
  });
  if (!r) return;
  const card = h("div", { class: "card" },
    h("h3", {}, "核验通过"),
    h("dl", { class: "detail-grid" },
      h("dt", {}, "运单"), h("dd", { class: "mono" }, r.waybill),
      h("dt", {}, "公司"), h("dd", {}, r.company),
      h("dt", {}, "收件人"), h("dd", {}, `${r.recipient}（${r.phone_mask}）`),
      h("dt", {}, "位置"), h("dd", {},
        r.status === "on_shelf" ? "人工货架（前台取件）" : "柜机格口"),
      h("dt", {}, "时限至"), h("dd", {}, fmt(r.deadline))),
    h("div", { class: "row mt12" },
      h("button", { class: "btn", onclick: () => take(r, {}) }, "本人取件，开门/交付")));
  const proxy = h("div", { class: "mt12" },
    h("h4", { style: "font-size:13px;margin-bottom:6px" }, "前台代取（须核对授权）"),
    h("div", { class: "row" },
      fld("授权码", h("input", { id: "p-auth", placeholder: "A 开头", style: "width:150px" })),
      fld("代取人姓名", h("input", { id: "p-name", style: "width:150px" })),
      fld("代取机号", h("input", { id: "p-phone", class: "mono", style: "width:160px" })),
      h("button", { class: "btn navy", onclick: () => take(r, {
        auth_code: $("#p-auth").value.trim(),
        proxy_name: $("#p-name").value.trim(),
        proxy_phone: $("#p-phone").value.trim(),
      }) }, "核对授权并交付")));
  card.append(proxy);
  box.append(card);
}
async function take(r, extra) {
  const sign = prompt("取件签收（电子签名）", extra.proxy_name || "收件人本人");
  if (sign === null) return;
  const resp = await api("/api/pickup", { code: $("#p-code").value.trim(),
    ...extra, sign }).catch(e => {
    toast(e.code === "OVERDUE"
      ? "已超时：包裹进入退回队列，请在「退回交接」联系处理" : e.message, "err", 5000);
    return null;
  });
  if (resp) { toast("取件成功，格口已关闭/货架已释放", "ok"); refreshMeta().then(route); }
}

// ---------------- 超时队列 ----------------
async function viewOverdue() {
  setCrumb("超时队列", "进程内任务每 5 秒扫描时限（含延期），超时自动入退回队列");
  const v = $("#view");
  v.innerHTML = "";
  const rows = await api("/api/overdue");
  const urgent = rows.filter(r => r.remain_hours <= 0);
  const soon = rows.filter(r => r.remain_hours > 0 && r.remain_hours <= 6);
  v.append(
    h("div", { class: "grid cols-3" },
      h("div", { class: "kpi red" }, h("div", { class: "n" }, urgent.length),
        h("div", { class: "l" }, "已超时（即将/已转待退回）")),
      h("div", { class: "kpi orange" }, h("div", { class: "n" }, soon.length),
        h("div", { class: "l" }, "6 小时内到期")),
      h("div", { class: "kpi" }, h("div", { class: "n" }, rows.length),
        h("div", { class: "l" }, "在存包裹合计"))),
    h("div", { class: "card" },
      h("h3", {}, "时限明细（按到期时间排序）"),
      h("table", {}, h("thead", {}, h("tr", {},
        h("th", {}, "运单"), h("th", {}, "公司"), h("th", {}, "收件人"),
        h("th", {}, "位置"), h("th", {}, "到期时间"), h("th", {}, "剩余(h)"),
        h("th", {}, "操作"))),
        h("tbody", {}, ...rows.map(r => h("tr", {},
          h("td", { class: "mono" }, r.waybill), h("td", {}, r.company),
          h("td", {}, `${r.rcode} ${r.rname}`),
          h("td", {}, r.cell_code ? `柜 ${r.cell_code}`
            : `货架 ${(r.shelf_slot || "").slice(4)}`),
          h("td", {}, fmt(r.effective_deadline)),
          h("td", {}, h("span", {
            class: "chip " + (r.remain_hours <= 0 ? "ready_return"
              : r.remain_hours <= 6 ? "in_transfer" : "stored") },
            r.remain_hours.toFixed(1))),
          h("td", {}, h("button", { class: "btn ghost sm",
            onclick: () => openDetail(r.id) }, "详情"))))))));
}

// ---------------- 退回交接 ----------------
async function viewReturns() {
  setCrumb("退回交接", "超时无人取 / 退回拒收均在此处理，凭证写入本地目录");
  const v = $("#view");
  v.innerHTML = "";
  const rows = await api("/api/returns");
  if (!rows.length) v.append(h("div", { class: "card" }, h("p", { class: "muted" }, "暂无待退回包裹。")));
  rows.forEach(r => {
    const card = h("div", { class: "card" },
      h("div", { class: "flex-between" },
        h("div", {}, h("b", { class: "mono" }, r.waybill), " ",
          h("span", { class: "chip ready_return" },
            { timeout: "超时无人取", full: "满仓退回" }[r.return_reason] || r.return_reason),
          r.reject_note ? h("span", { class: "chip sealed" }, "曾被拒收") : null),
        h("div", {},
          h("button", { class: "btn sm", onclick: () => openDetail(r.id) }, "详情/转移"),
          h("button", { class: "btn navy sm", style: "margin-left:6px",
            onclick: () => doReturn(r.id) }, "签收退回"),
          h("button", { class: "btn danger sm", style: "margin-left:6px",
            onclick: () => doReject(r.id) }, "拒收"))),
      h("div", { class: "muted mt8", style: "font-size:12px" },
        `${r.company}｜${r.rcode || ""} ${r.rname}｜${r.bname || ""}｜` +
        `位置：${r.cell_code ? "柜 " + r.cell_code : "货架 " + (r.shelf_id || "-")}`),
      r.reject_note ? h("div", { class: "warn-box" },
        `上次拒收：${r.reject_note}（异常凭证已保存到 data/evidence/）`) : null);
    v.append(card);
  });
}

// ---------------- 强开审批 ----------------
async function viewApprovals() {
  setCrumb("强制开柜审批", "快递员申请 → 物业审批 → 凭审批号强开取出");
  const v = $("#view");
  v.innerHTML = "";
  const pend = await api("/api/approvals?status=pending");
  const decided = (await api("/api/approvals?status=approved"))
    .concat(await api("/api/approvals?status=rejected")).slice(0, 8);
  const isP = state.me.role === "property";
  const tbl = list => h("table", {}, h("thead", {}, h("tr", {},
    h("th", {}, "#"), h("th", {}, "柜机/格口"), h("th", {}, "运单"),
    h("th", {}, "事由"), h("th", {}, "申请人"), h("th", {}, "状态"), h("th", {}, "操作"))),
    h("tbody", {}, ...list.map(a => h("tr", {},
      h("td", {}, a.id),
      h("td", {}, `${a.locker_name} ${a.cell_code}`),
      h("td", { class: "mono" }, a.waybill || "（空柜）"),
      h("td", {}, a.reason), h("td", {}, a.applicant),
      h("td", {}, h("span", {
        class: "chip " + (a.status === "approved" ? "stored"
          : a.status === "rejected" ? "returned" : "in_transfer") },
        { pending: "待审批", approved: "已批准", rejected: "已驳回" }[a.status])),
      h("td", {}, ...approvalActions(a, isP))))));
  v.append(
    h("div", { class: "card" }, h("h3", {}, "待审批",
      h("span", { class: "hint" }, isP ? "物业身份可直接批准 / 驳回" : "仅物业可审批")),
      pend.length ? tbl(pend) : h("p", { class: "muted" }, "无待审批单")),
    h("div", { class: "card" }, h("h3", {}, "已处理（快递员可凭批准单强开取出）"),
      decided.length ? tbl(decided) : h("p", { class: "muted" }, "无")));
}
function approvalActions(a, isP) {
  const btns = [];
  if (a.status === "pending" && isP) {
    btns.push(h("button", { class: "btn sm", onclick: async () => {
      const note = prompt("批准备注", "核实故障属实，同意强开");
      const r = await api(`/api/approvals/${a.id}/decide`,
        { approve: true, note: note || "" }).catch(onErr);
      if (r) { toast("已批准强开", "ok"); route(); }
    } }, "批准"), h("button", { class: "btn ghost sm", style: "margin-left:6px",
      onclick: () => api(`/api/approvals/${a.id}/decide`,
        { approve: false, note: "不符合强开条件" })
        .then(() => { toast("已驳回"); route(); }).catch(onErr) }, "驳回"));
  }
  if (a.status === "approved" && a.package_id)
    btns.push(h("button", { class: "btn navy sm", style: "margin-left:6px",
      onclick: async () => {
        const r = await api(`/api/packages/${a.package_id}/force-retrieve`,
          { approval_id: a.id }).catch(e => {
          if (e.code === "FORBIDDEN") toast("仅本公司快递员可执行强开取出", "err", 5000);
          else onErr(e); return null;
        });
        if (r) { toast("强开成功，包裹已取出，请在详情页选择转移去向或封存", "ok", 6000);
          openDetail(a.package_id); }
      } }, "凭审批强开取出"));
  return btns;
}

// ---------------- 包裹列表 ----------------
async function viewPackages() {
  setCrumb("包裹与事件", state.me.role === "courier"
    ? `公司隔离：仅显示 ${state.me.company}` : "物业视角：全公司可见");
  const v = $("#view");
  v.innerHTML = "";
  const rows = await api("/api/packages");
  const trs = rows.map(r => h("tr", {
    style: "cursor:pointer",
    onclick: () => openDetail(r.id),
  },
    h("td", { class: "mono" }, r.waybill), h("td", {}, r.company),
    h("td", {}, `${r.rcode} ${r.rname}`),
    h("td", {}, SIZE_CN[r.size]),
    h("td", {}, h("span", { class: `chip ${r.status}` }, STATUS_CN[r.status])),
    h("td", {}, r.cell_code ? `柜 ${r.cell_code}`
      : r.shelf_id ? `货架 ${r.shelf_id.slice(4)}` : "-"),
    h("td", { class: "mono" }, r.pickup_code || "-"),
    h("td", {}, fmt(r.extension_until || r.deadline_at)),
    h("td", {}, fmt(r.updated_at))));
  const table = h("table", {},
    h("thead", {}, h("tr", {},
      h("th", {}, "运单"), h("th", {}, "公司"), h("th", {}, "收件人"),
      h("th", {}, "尺寸"), h("th", {}, "状态"), h("th", {}, "位置"),
      h("th", {}, "取件码"), h("th", {}, "时限"), h("th", {}, "更新时间"))),
    h("tbody", {}, ...trs));
  v.append(h("div", { class: "card" },
    h("h3", {}, `共 ${rows.length} 件（点击运单查看详情与完整事件流水）`),
    h("div", { style: "overflow-x:auto" }, table)));
}

// ---------------- 通知记录 ----------------
async function viewNotifications() {
  setCrumb("通知记录", "模拟短信网关：失败自动退避重试（10s/30s/90s）");
  const v = $("#view");
  v.innerHTML = "";
  const rows = await api("/api/notifications");
  v.append(h("div", { class: "card" },
    h("div", { class: "flex-between" },
      h("h3", {}, `最近 ${rows.length} 条`),
      h("div", {}, h("input", { id: "sim-phone", class: "mono",
        placeholder: "注入失败的手机号", value: "13800010005",
        style: "width:170px;display:inline-block" }),
        h("button", { class: "btn ghost sm", onclick: async () => {
          await api("/api/sim/sms-fail", { phone: $("#sim-phone").value, n: 2 });
          toast("已注入：该号码下 2 条短信将发送失败，观察自动重试", "info", 5000);
        } }, "模拟网关失败×2"))),
    ...rows.map(n => h("div", {
      style: "border-bottom:1px solid #eef1f5;padding:8px 0;font-size:13px" },
      h("div", {}, h("b", { class: "mono" }, n.waybill), " ",
        h("span", { class: "muted", style: "font-size:12px" }, n.kind), " ",
        h("span", {
          class: "chip " + (n.status === "sent" ? "stored"
            : n.status === "failed" ? "ready_return" : "in_transfer") },
          n.status === "sent" ? "已送达" : `待发/重试 ${n.retries}`)),
      h("div", {}, n.content),
      n.last_error ? h("div", { class: "muted", style: "color:var(--red);font-size:12px" },
        `网关错误：${n.last_error}`) : null))));
}

boot();
