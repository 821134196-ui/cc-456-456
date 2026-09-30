/* 商务楼快递柜系统前端：纯原生 JS，无框架依赖。 */
const SIZE_NAME = { S: "小", M: "中", L: "大" };
const STATUS_NAME = {
  intake: "待入柜", stored: "柜内存放", on_shelf: "人工货架", overdue: "超时",
  picked: "已取出", returned: "已退回", sealed: "异常封存",
};
const ROLE_NAME = { courier: "快递员", property: "物业", frontdesk: "前台", guest: "收件人" };

let META = { users: [], buildings: [], lockers: [], shelves: [], companies: [] };
let state = {
  userId: null,
  lockerId: null,
  shelfId: null,
  lockerView: null,
  shelfView: null,
  wf: { step: 1, scan: null, alloc: null },
};

/* ---------------- 基础工具 ---------------- */
const $ = (s) => document.querySelector(s);
const $$ = (s) => Array.from(document.querySelectorAll(s));
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (iso) => iso ? iso.replace("T", " ").slice(0, 16) : "—";

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json" };
  if (state.userId) headers["X-User-Id"] = state.userId;
  const res = await fetch(path, { method: opts.method || "GET", headers, body: opts.body ? JSON.stringify(opts.body) : undefined });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(data.error || `HTTP ${res.status}`);
    err.data = data;
    err.status = res.status;
    throw err;
  }
  return data;
}

let toastSeq = 0;
function toast(msg, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = msg;
  $("#toastWrap").appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; el.style.transition = "opacity .3s"; }, 3200);
  setTimeout(() => el.remove(), 3600);
}

function role() {
  return META.users.find((u) => u.id === state.userId)?.role;
}
function isProperty() { return role() === "property"; }
function isFrontdesk() { return role() === "frontdesk"; }
function isCourier() { return role() === "courier"; }

/* ---------------- 导航 ---------------- */
$$(".navbtn").forEach((btn) => btn.addEventListener("click", () => showView(btn.dataset.view)));
function showView(name) {
  $$(".navbtn").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
  if (name === "dashboard") loadDashboard();
  if (name === "lockers") loadLockerView();
  if (name === "shelves") loadShelfView();
  if (name === "overdue") loadOverdue();
  if (name === "returns") loadReturns();
  if (name === "approvals") loadApprovals();
  if (name === "pickup") renderPickup();
}

/* ---------------- 启动 ---------------- */
async function init() {
  META = await api("/api/meta");
  const sel = $("#userSelect");
  sel.innerHTML = META.users.map((u) => {
    const co = u.company_name ? ` · ${u.company_name}` : "";
    return `<option value="${u.id}">${esc(u.name)}（${ROLE_NAME[u.role]}${co}）</option>`;
  }).join("");
  state.userId = localStorage.getItem("lockerUserId") || META.users[0].id;
  sel.value = state.userId;
  sel.addEventListener("change", () => {
    state.userId = sel.value;
    localStorage.setItem("lockerUserId", state.userId);
    toast(`已切换操作人：${sel.selectedOptions[0].textContent}`);
    refreshCurrent();
  });

  $("#lockerSelect").innerHTML = META.lockers.map((l) =>
    `<option value="${l.id}">${esc(l.building_name)} · ${esc(l.name)}</option>`).join("");
  state.lockerId = META.lockers[0].id;
  $("#lockerSelect").value = state.lockerId;
  $("#lockerSelect").addEventListener("change", (e) => { state.lockerId = e.target.value; loadLockerView(); });

  $("#shelfSelect").innerHTML = META.shelves.map((s) =>
    `<option value="${s.id}">${esc(s.building_name)} · ${esc(s.name)}</option>`).join("");
  state.shelfId = META.shelves[0].id;
  $("#shelfSelect").value = state.shelfId;
  $("#shelfSelect").addEventListener("change", (e) => { state.shelfId = e.target.value; loadShelfView(); });

  $("#drawerClose").onclick = closeDrawer;
  $("#drawerMask").onclick = closeDrawer;
  $("#btnRunScheduler").onclick = async () => {
    try {
      const r = await api("/api/scheduler/tick", { method: "POST", body: {} });
      toast(`扫描完成：超时 ${r.stats.overdue} 单，退回入队 ${r.stats["return"]} 单，短信重试 ${r.stats.notify} 条`, "ok");
      loadOverdue(); loadReturns(); loadDashboard();
    } catch (e) { toast(e.message, "err"); }
  };

  startClock();
  renderWf();
  showView("dashboard");
  setInterval(() => {
    const active = $(".navbtn.active").dataset.view;
    if (["dashboard", "overdue", "returns", "approvals"].includes(active)) refreshCurrent();
  }, 15000);
}

function refreshCurrent() {
  const active = $(".navbtn.active").dataset.view;
  showView(active);
}

function startClock() {
  const tick = () => $("#clock").textContent = "系统时间 " + new Date().toLocaleString("zh-CN", { hour12: false });
  tick(); setInterval(tick, 1000);
}

/* ---------------- 总览 ---------------- */
async function loadDashboard() {
  const d = await api("/api/dashboard");
  const c = d.counts;
  const kpis = [
    ["柜内存放", c.stored || 0, ""], ["货架暂存", c.on_shelf || 0, ""],
    ["超时未取", c.overdue || 0, "orange"], ["待退回交接", c.return_pending || 0, "amber"],
    ["异常封存", c.sealed || 0, "red"], ["待审批强开", c.force_pending || 0, "red"],
    ["待发/重试短信", c.notify_pending || 0, "amber"], ["已完成取件", c.picked || 0, "green"],
  ];
  $("#kpiRow").innerHTML = kpis.map(([label, val, cls]) =>
    `<div class="kpi ${cls}"><div class="k-label">${label}</div><div class="k-value">${val}</div></div>`).join("");

  $("#dashLockers").innerHTML = d.lockers.map((l) => {
    const pct = Math.round(l.full_rate * 100);
    const rateCls = pct >= 100 ? "color:var(--red)" : pct >= 80 ? "color:var(--orange-dark)" : "color:var(--green)";
    return `<div class="locker-card" data-locker="${l.id}">
      <div class="lc-top">
        <div><div class="lc-name">${esc(l.name)} <span style="font-weight:400;font-size:12px">· ${esc(l.building_name)}</span></div>
        <div class="lc-building">${l.occupied}/${l.total} 格口占用</div></div>
        <div class="lc-rate" style="${rateCls}">${pct}%</div>
      </div>
      <div class="rate-bar"><i style="width:${pct}%"></i></div>
      <div class="lc-sub">${pct >= 100 ? "已满仓，新包裹需转移" : pct >= 80 ? "接近满仓" : "运力正常"}</div>
    </div>`;
  }).join("");
  $$("#dashLockers .locker-card").forEach((card) =>
    card.addEventListener("click", () => {
      state.lockerId = card.dataset.locker;
      $("#lockerSelect").value = state.lockerId;
      showView("lockers");
    }));
}

/* ---------------- 柜机格口图 ---------------- */
async function loadLockerView() {
  state.lockerView = await api(`/api/lockers/${state.lockerId}`);
  const { locker, compartments, occupied, total, full_rate } = state.lockerView;
  $("#lockerTitle").textContent = `${locker.building_name} · ${locker.name}`;
  const pct = Math.round(full_rate * 100);
  const badge = $("#lockerRate");
  badge.textContent = `满仓率 ${pct}%（${occupied}/${total}）`;
  badge.className = "rate-badge" + (pct >= 100 ? " hot" : pct >= 80 ? " warn" : "");

  $("#compMap").innerHTML = compartments.map((cp) => {
    const cls = ["cell", `c-${cp.size}`, cp.status, cp.door_status === "open" ? "open" : "", cp.door_status === "fault" ? "fault" : ""].join(" ");
    const label = cp.status === "free" ? "空闲" :
      cp.door_status === "fault" ? esc(cp.fault_note || "门锁故障") :
      cp.status === "allocating" ? "开门等待关门回执" : "点击查看包裹";
    return `<div class="${cls}" data-comp="${cp.id}" title="${esc(cp.code)} ${SIZE_NAME[cp.size]}格口">
      <div class="c-code">${esc(cp.code)}</div>
      <div class="c-size">${SIZE_NAME[cp.size]}</div>
      <div class="c-pkg">${label}</div>
    </div>`;
  }).join("");
  $$("#compMap .cell").forEach((cell) =>
    cell.addEventListener("click", () => onCompartmentClick(cell.dataset.comp)));
}

async function onCompartmentClick(compId) {
  const cp = state.lockerView.compartments.find((x) => x.id === compId);
  if (cp.occupant_package_id) {
    openPackage(cp.occupant_package_id);
    return;
  }
  if (cp.status === "fault") {
    openCompartmentActions(cp);
    return;
  }
  if (cp.status === "allocating") {
    toast("该格口门已打开，正在等待关门回执", "warn");
    return;
  }
  toast(`${cp.code}：${SIZE_NAME[cp.size]}格口空闲${cp.door_status === "open" ? "（门未关闭，禁止分配）" : ""}`);
}

function openCompartmentActions(cp) {
  $("#drawerTitle").textContent = `格口 ${cp.code} · 故障处理`;
  $("#drawerBody").innerHTML = `
    <dl class="kv">
      <dt>格口编号</dt><dd>${esc(cp.code)}（${SIZE_NAME[cp.size]}格口）</dd>
      <dt>所属柜机</dt><dd>${esc(state.lockerView.locker.name)}</dd>
      <dt>状态</dt><dd><span class="tag sealed">故障维修</span></dd>
      <dt>故障说明</dt><dd>${esc(cp.fault_note || "—")}</dd>
    </dl>
    <div class="section-title">操作</div>
    <div class="btn-row">
      ${!cp.occupant_package_id && isProperty() ? `<button class="btn" id="btnRepair">维修完成，恢复格口</button>` : ""}
    </div>
    <p class="empty" style="margin-top:10px">${cp.occupant_package_id ? "内有封存包裹，须经物业审批强制开柜取出。" : ""}${isCourier() ? "快递员无维修权限，请联系物业。" : ""}</p>`;
  showDrawer();
  $("#btnRepair")?.addEventListener("click", async () => {
    try { await api(`/api/compartments/${cp.id}/repair`, { method: "POST", body: {} });
      toast("维修完成，格口已恢复", "ok"); closeDrawer(); loadLockerView();
    } catch (e) { toast(e.message, "err"); }
  });
}

/* ---------------- 入柜/转移连续路径工作流 ---------------- */
function setStep(n) {
  state.wf.step = n;
  $$("#stepper li").forEach((li) => {
    const s = Number(li.dataset.step);
    li.classList.toggle("active", s === n);
    li.classList.toggle("done", s < n);
  });
  renderWf();
}

function renderWf() {
  const body = $("#wfBody");
  const s = state.wf.step;
  if (s === 1) body.innerHTML = panelScan();
  if (s === 2) body.innerHTML = panelScanResult();
  if (s === 3) body.innerHTML = panelDoorOpen();
  if (s === 4) body.innerHTML = panelDone();
  bindWf();
}

function panelScan() {
  const lockerOpts = META.lockers.map((l) =>
    `<option value="${l.id}" ${l.id === state.lockerId ? "selected" : ""}>${esc(l.building_name)} · ${esc(l.name)}</option>`).join("");
  return `<div class="wf-panel active">
    <h3>第 1 步 · 扫描运单</h3>
    <div class="field"><label>运单号（虚构，可试：SF600601 / HD600602）</label>
      <input id="wfWaybill" placeholder="例如 SF600601" value="SF600601"></div>
    <div class="field"><label>投放柜机</label><select id="wfLocker">${lockerOpts}</select></div>
    <div class="btn-row"><button class="btn orange" id="btnScan">扫描运单</button>
      <button class="btn ghost" id="btnWfReset">重置</button></div>
    <p class="empty">重复扫码、错投、满仓、尺寸不符都会在下一步给出分流路径。</p>
  </div>`;
}

function panelScanResult() {
  const r = state.wf.scan;
  if (!r) return `<div class="wf-panel active"></div>`;
  let html = `<div class="wf-panel active"><h3>第 2 步 · 匹配结果与分流</h3>`;

  if (r.repeat) {
    html += `<div class="wf-result warn">重复扫码：该包裹状态为「${STATUS_NAME[r.status] || r.status}」，当前位置：${esc(r.position)}。</div>
      <div class="btn-row"><button class="btn orange" id="btnRepeatPkg">查看包裹轨迹</button>
      <button class="btn ghost" id="btnBack1">返回重扫</button></div>`;
    html += `</div>`;
    return html;
  }

  html += `<div class="wf-result info">运单 <b>${esc(r.waybill)}</b> · ${SIZE_NAME[r.size]}格口包裹`;
  const lb = META.buildings.find((b) => b.id === r.target_building_id);
  html += ` · 目标楼栋：${esc(lb?.name || r.target_building_id)}</div>`;

  if (r.misdirected) {
    html += `<div class="wf-result warn">⚠ 错投检测：当前柜机不在包裹目标楼栋，不能直接入柜。请登记凭证并转投正确楼栋。</div>
      <div class="btn-row">
        <button class="btn ghost" id="btnMisdirect">登记错投凭证</button>
        <button class="btn orange" id="btnMisdirectForward">转投正确楼栋柜机</button>
        <button class="btn ghost" id="btnBack1">返回重扫</button>
      </div><div id="misExtra"></div>`;
  } else if (r.suggest_compartment_id) {
    html += `<div class="wf-result ok">已匹配到最小可用格口，开门后请放入包裹并等待关门回执。</div>
      <div class="btn-row"><button class="btn orange" id="btnAllocate">锁定格口并开门</button>
      <button class="btn ghost" id="btnBack1">返回重扫</button></div>`;
  } else if (r.locker_full) {
    html += `<div class="wf-result err">该柜机满仓（或门未关闭/故障导致无适配格口）。请选择连续转移路径：</div>
      <h3 style="margin-top:12px">路径 A · 转入同楼人工货架</h3>
      <div class="wf-flow-btns">`;
    const lbId = META.lockers.find((l) => l.id === $("#wfLocker")?.value)?.building_id || state.lockerView.locker.building_id;
    META.shelves.filter((x) => x.building_id === lbId).forEach((sh) => {
      html += `<button class="btn ghost btn-shelf" data-shelf="${sh.id}">转 ${esc(sh.name)}</button>`;
    });
    html += `</div><h3 style="margin-top:12px">路径 B · 转入邻楼柜机</h3><div class="wf-flow-btns" id="neighborBtns"><button class="btn ghost" id="btnLoadNeighbors">查询邻楼可用格口</button></div>`;
  }
  html += `</div>`;
  return html;
}

function panelDoorOpen() {
  const a = state.wf.alloc;
  return `<div class="wf-panel active">
    <h3>第 3 步 · 关门回执</h3>
    <div class="wf-result warn">格口门已打开（${esc(a.code || "")}）。只有关门回执成功，包裹才算正式入柜。</div>
    <div class="field"><label>若包裹实际尺寸更大（尺寸不符），选择实际尺寸后重新匹配</label>
      <select id="wfActualSize"><option value="">尺寸相符，无需纠正</option><option value="M">实际为中格口 M</option><option value="L">实际为大格口 L</option></select></div>
    <div class="btn-row">
      <button class="btn orange" id="btnCloseReceipt">关门回执成功 · 确认入柜</button>
      <button class="btn ghost" id="btnSizeMismatch">按实际尺寸重新匹配</button>
      <button class="btn danger" id="btnCancelIntake">放弃投放并释放格口</button>
    </div>
    <p class="empty">门保持打开期间，该格口不会被分配给其他快递员。</p>
  </div>`;
}

function panelDone() {
  const d = state.wf.done;
  return `<div class="wf-panel active">
    <h3>第 4 步 · 入柜完成通知</h3>
    <div class="wf-result ok">✓ 关门回执成功，包裹已入柜。系统已生成新取件码并发出短信通知（旧码全部作废）。</div>
    <dl class="kv">
      <dt>取件码</dt><dd><span class="code-chip">${esc(d.pickup_code)}</span></dd>
      <dt>存放时限至</dt><dd>${fmt(d.deadline_at)}</dd>
    </dl>
    <div class="btn-row"><button class="btn orange" id="btnRestart">继续扫描下一单</button>
      <button class="btn ghost" id="btnViewPkg">查看包裹轨迹</button></div>
  </div>`;
}

function bindWf() {
  $("#btnWfReset")?.addEventListener("click", resetWf);
  $("#btnBack1")?.addEventListener("click", () => setStep(1));
  $("#btnRestart")?.addEventListener("click", resetWf);
  $("#btnRepeatPkg")?.addEventListener("click", () => openPackage(state.wf.scan.package_id));

  $("#btnScan")?.addEventListener("click", async () => {
    const waybill = $("#wfWaybill").value.trim();
    const locker_id = $("#wfLocker").value;
    state.lockerId = locker_id;
    if (!waybill) return toast("请输入运单号", "warn");
    try {
      state.wf.scan = await api("/api/packages/scan", { method: "POST", body: { waybill, locker_id } });
      setStep(2);
    } catch (e) {
      if (e.status === 409 && e.data?.status) {
        // 重复扫码：连续路径内展示，不弹多层窗口
        state.wf.scan = { waybill, repeat: true, ...e.data };
        setStep(2);
      } else {
        toast(e.message, "err");
      }
    }
  });

  $("#btnAllocate")?.addEventListener("click", async () => {
    const r = state.wf.scan;
    try {
      const a = await api("/api/packages/allocate", { method: "POST", body: { waybill: r.waybill, locker_id: $("#wfLocker").value } });
      const comp = await api(`/api/lockers/${state.lockerId}`);
      const cp = comp.compartments.find((x) => x.id === a.compartment_id);
      state.wf.alloc = { ...a, code: cp.code };
      setStep(3);
      loadLockerView();
    } catch (e) {
      if (e.data?.race) toast("格口竞争失败：另一名快递员已获得该格口锁，请重新扫描匹配", "err");
      else toast(e.message, "err");
    }
  });

  $$(".btn-shelf").forEach((btn) => btn.addEventListener("click", () => doTransferShelf(btn.dataset.shelf)));
  $("#btnLoadNeighbors")?.addEventListener("click", loadNeighborBtns);
  $("#btnMisdirect")?.addEventListener("click", doMisdirect);
  $("#btnMisdirectForward")?.addEventListener("click", doMisdirectForward);

  $("#btnCloseReceipt")?.addEventListener("click", doCloseReceipt);
  $("#btnSizeMismatch")?.addEventListener("click", doSizeMismatch);
  $("#btnCancelIntake")?.addEventListener("click", doCancelIntake);
  $("#btnViewPkg")?.addEventListener("click", () => openPackage(state.wf.done.package_id));
}

async function loadNeighborBtns() {
  const r = state.wf.scan;
  const wrap = $("#neighborBtns");
  try {
    const data = await api(`/api/buildings/${r.target_building_id}/neighbors?size=${r.size}`);
    const avail = data.options.filter((o) => o.available);
    if (!avail.length) {
      wrap.innerHTML = `<span class="empty">邻楼柜机也无适配格口，请走人工货架。</span>`;
      return;
    }
    wrap.innerHTML = avail.map((o) =>
      `<button class="btn orange btn-neighbor" data-locker="${o.locker_id}">${esc(o.building_name)} · ${esc(o.locker_name)}</button>`).join("");
    $$(".btn-neighbor").forEach((b) => b.addEventListener("click", () => doTransferLocker(b.dataset.locker)));
  } catch (e) { toast(e.message, "err"); }
}

async function doTransferShelf(shelfId) {
  const r = state.wf.scan;
  try {
    const res = await api(`/api/packages/${r.package_id}/transfer-shelf`, {
      method: "POST", body: { shelf_id: shelfId, reason: "full" },
    });
    state.wf.done = { pickup_code: res.new_code, deadline_at: null, package_id: r.package_id, shelf: res.target, old_code: res.old_code };
    setStep(4);
    toast(`已转入人工货架：${res.target}，旧码 ${res.old_code || "无"} 已作废`, "ok");
  } catch (e) { toast(e.message, "err"); }
}

async function doTransferLocker(lockerId, reason = "full") {
  const r = state.wf.scan;
  try {
    const a = await api(`/api/packages/${r.package_id}/transfer-locker`, {
      method: "POST", body: { locker_id: lockerId, reason },
    });
    state.wf.alloc = a;
    state.lockerId = lockerId;
    $("#lockerSelect").value = lockerId;
    setStep(3);
    toast(`旧取件码 ${a.old_code || "无"} 已作废，目标格口已开门，请完成关门回执`, "warn");
    loadLockerView();
  } catch (e) { toast(e.message, "err"); }
}

async function doMisdirect() {
  const r = state.wf.scan;
  try {
    const res = await api(`/api/packages/${r.package_id}/misdirect`, { method: "POST", body: { note: "扫描发现投放楼栋与目标楼栋不符" } });
    $("#misExtra").innerHTML = `<div class="wf-result ok" style="margin-top:10px">错投凭证已留存：${esc(res.evidence.split("/").pop())}</div>`;
    toast("错投凭证已保存到本地目录", "ok");
  } catch (e) { toast(e.message, "err"); }
}

async function doMisdirectForward() {
  const r = state.wf.scan;
  // 找正确楼栋的柜机并直接转投
  const data = await api(`/api/buildings/${r.target_building_id}/neighbors?size=${r.size}`);
  const target = data.options.find((o) => o.building_id === r.target_building_id && o.available);
  if (!target) return toast("正确楼栋暂无适配格口，请转人工货架", "warn");
  await doTransferLocker(target.locker_id, "misdelivery");
}

async function doCloseReceipt() {
  const a = state.wf.alloc;
  try {
    const res = await api(`/api/packages/${a.package_id}/close-receipt`, {
      method: "POST", body: { alloc_token: a.alloc_token },
    });
    state.wf.done = { ...res, package_id: a.package_id };
    setStep(4);
    toast(`入柜成功，取件码 ${res.pickup_code}，短信通知已排队`, "ok");
    loadLockerView();
  } catch (e) { toast(e.message, "err"); }
}

async function doSizeMismatch() {
  const actual = $("#wfActualSize").value;
  if (!actual) return toast("请先选择包裹的实际尺寸", "warn");
  const a = state.wf.alloc;
  try {
    const res = await api(`/api/packages/${a.package_id}/size-mismatch`, {
      method: "POST", body: { actual_size: actual },
    });
    const comp = state.lockerView.compartments.find((x) => x.id === res.compartment_id) ||
      (await api(`/api/lockers/${state.lockerId}`)).compartments.find((x) => x.id === res.compartment_id);
    state.wf.alloc = { ...res, code: comp?.code || "?" };
    renderWf();
    toast(`已改配 ${SIZE_NAME[actual]} 格口 ${comp?.code || ""}，新门已打开`, "ok");
    loadLockerView();
  } catch (e) {
    if (e.status === 503 && e.data?.full) toast("同柜无更大格口：请转人工货架或邻楼柜机", "err");
    else toast(e.message, "err");
  }
}

async function doCancelIntake() {
  const a = state.wf.alloc;
  try {
    await api(`/api/packages/${a.package_id}/cancel`, { method: "POST", body: {} });
    toast("已放弃投放，格口释放", "ok");
    resetWf(); loadLockerView();
  } catch (e) { toast(e.message, "err"); }
}

function resetWf() {
  state.wf = { step: 1, scan: null, alloc: null, done: null };
  setStep(1);
}

/* ---------------- 人工货架 ---------------- */
async function loadShelfView() {
  state.shelfView = await api(`/api/shelves/${state.shelfId}`);
  const { shelf, slots } = state.shelfView;
  $("#shelfTitle").textContent = `${shelf.building_name} · ${shelf.name}`;
  const racks = {};
  slots.forEach((s) => { (racks[s.rack_no] = racks[s.rack_no] || []).push(s); });
  const sortedRacks = Object.keys(racks).sort();
  $("#shelfMap").innerHTML = sortedRacks.map((rack) => {
    const list = racks[rack];
    const levels = {};
    list.forEach((s) => (levels[s.level_no] = levels[s.level_no] || []).push(s));
    const occ = list.filter((s) => s.status === "occupied").length;
    const isQ = rack === "Q";
    return `<div class="rack ${isQ ? "quarantine" : ""}">
      <div class="rack-head"><span>${isQ ? "Q · 异常封存架" : rack + " 架"}</span>
        <span>${occ}/${list.length} 位占用</span></div>
      ${Object.keys(levels).sort((a, b) => a - b).map((lv) => `
        <div class="rack-level"><div class="level-no">${lv}层</div>
          <div class="level-slots">
            ${levels[lv].sort((a, b) => a.pos_no - b.pos_no).map((s) => `
              <div class="slot ${s.status} ${isQ ? "quarantine" : ""}" data-slot="${s.id}">
                <b>${rack}-${lv}-${s.pos_no}</b>
                <span class="s-pkg">${s.status === "occupied" ? "📦 有件" : "空"}</span>
              </div>`).join("")}
          </div></div>`).join("")}
    </div>`;
  }).join("");
  $$("#shelfMap .slot").forEach((el) =>
    el.addEventListener("click", () => onSlotClick(el.dataset.slot)));
  $("#shelfBody").innerHTML = `<p class="empty">点击左侧任一货位查看包裹与操作。</p>`;
}

async function onSlotClick(slotId) {
  const slot = state.shelfView.slots.find((x) => x.id === slotId);
  if (!slot.occupant_package_id) {
    $("#shelfBody").innerHTML = `<p class="empty">货位 ${esc(slot.rack_no)}-${slot.level_no}-${slot.pos_no} 空闲。</p>`;
    return;
  }
  const d = await api(`/api/packages/${slot.occupant_package_id}`);
  renderShelfPackage(d);
}

function renderShelfPackage(d) {
  const p = d.package;
  const isQ = state.shelfView.slots.find((s) => s.id === p.current_shelf_slot_id)?.rack_no === "Q";
  $("#shelfBody").innerHTML = `
    <dl class="kv">
      <dt>货位</dt><dd>${esc(d.position)}</dd>
      <dt>运单号</dt><dd class="mono">${esc(p.waybill)}</dd>
      <dt>状态</dt><dd><span class="tag ${p.status}">${STATUS_NAME[p.status]}</span></dd>
      <dt>尺寸</dt><dd>${SIZE_NAME[p.size]}格口</dd>
      <dt>存放时限</dt><dd>${fmt(p.deadline_at)}</dd>
      <dt>当前有效码</dt><dd>${activeCodeHtml(d.codes)}</dd>
    </dl>
    <div class="btn-row">
      <button class="btn orange" id="shelfDetail">完整轨迹/转移记录</button>
      ${!isQ && p.status !== "picked" ? `<button class="btn ghost" id="shelfProxy">生成代取授权</button>` : ""}
    </div>
    ${isQ ? `<p class="empty">异常封存区包裹，等待退回拒收处理或物业进一步处置。</p>` : ""}`;
  $("#shelfDetail").onclick = () => openPackage(p.id);
  $("#shelfProxy")?.addEventListener("click", async () => {
    const name = prompt("被授权人姓名（虚构，如：同事陈默）", "陈默");
    if (!name) return;
    try {
      const r = await api(`/api/packages/${p.id}/proxy`, { method: "POST", body: { authorized_name: name } });
      toast(`代取授权已生成：授权码 ${r.auth_code}（${name}）`, "ok");
    } catch (e) { toast(e.message, "err"); }
  });
}

function activeCodeHtml(codes) {
  const active = codes.filter((c) => c.status === "active");
  const voids = codes.filter((c) => c.status !== "active").slice(-2);
  return active.map((c) => `<span class="code-chip">${esc(c.code)}</span>`).join(" ") +
    voids.map((c) => `<span class="code-chip ${c.status}" title="${esc(c.void_reason || "")}">${esc(c.code)}</span>`).join(" ");
}

/* ---------------- 取件核验 ---------------- */
function renderPickup() {
  $("#pickupBody").innerHTML = `
    <div class="field"><label>取件码（短信中的 6 位数字；可试已作废码 700001）</label>
      <input id="pkCode" placeholder="6 位取件码" maxlength="6"></div>
    <div class="field"><label>本人取件：手机尾号（收件人预留手机号后 4 位）</label>
      <input id="pkPhone" placeholder="后 4 位" maxlength="4"></div>
    <div class="section-title">前台代取（必须核对授权）</div>
    <div class="field"><label>代取授权码（4 位，由收件人/前台生成）</label>
      <input id="pkProxyCode" placeholder="如无留空" maxlength="4"></div>
    <div class="field"><label>被授权人姓名</label><input id="pkProxyName" placeholder="授权时登记的姓名"></div>
    <div class="btn-row"><button class="btn orange" id="btnPickupVerify">核验并开门</button></div>
    <div id="pkResult"></div>`;
  $("#btnPickupVerify").onclick = doPickupVerify;
}

async function doPickupVerify() {
  const body = {
    code: $("#pkCode").value.trim(),
    phone_last4: $("#pkPhone").value.trim() || null,
    proxy_code: $("#pkProxyCode").value.trim() || null,
    proxy_name: $("#pkProxyName").value.trim() || null,
  };
  try {
    const r = await api("/api/pickup/verify", { method: "POST", body });
    let html = `<div class="wf-result ok" style="margin-top:12px">核验通过（${r.mode === "proxy" ? "前台代取，授权已核对" : "本人取件"}）`;
    html += r.on_shelf ? "，请在人工货架按定位取件" : "，柜门已弹开";
    html += `：<b>${esc(r.position)}</b> · ${esc(r.recipient_alias)}</div>
      <div class="btn-row"><button class="btn orange" id="btnPickupDone">关门回执 · 取件完成</button></div>`;
    $("#pkResult").innerHTML = html;
    $("#btnPickupDone").onclick = async () => {
      try {
        await api(`/api/packages/${r.package_id}/pickup-complete`, {
          method: "POST", body: { proxy_auth_id: r.proxy_auth_id },
        });
        toast("取件完成，格口/货位已释放，取件码已使用", "ok");
        renderPickup();
      } catch (e) { toast(e.message, "err"); }
    };
  } catch (e) {
    let hint = "";
    if (e.data?.void) hint = "——请在包裹轨迹中找到最新有效码（每次转移都会作废旧码）";
    if (e.data?.proxy_invalid) hint = "——前台代取必须核对有效授权码";
    if (e.data?.fault) hint = "——格口故障封存，请到「强开审批」处理";
    $("#pkResult").innerHTML = `<div class="wf-result err" style="margin-top:12px">${esc(e.message)} ${esc(hint)}</div>`;
  }
}

/* ---------------- 超时队列 ---------------- */
async function loadOverdue() {
  const items = (await api("/api/overdue")).items;
  $("#overdueBody").innerHTML = items.length ? items.map((p) => `
    <tr>
      <td class="mono">${esc(p.waybill)}</td>
      <td>${esc(p.company_name)}</td>
      <td>${esc(p.recipient_alias)}</td>
      <td>${p.current_shelf_slot_id ? "货架" : "柜机"}</td>
      <td>${SIZE_NAME[p.size]}</td>
      <td>${fmt(p.deadline_at)}</td>
      <td><button class="btn ghost sm lnk-pkg" data-pkg="${p.id}">查看有效码</button></td>
      <td class="btn-row" style="margin:0">
        <button class="btn ghost sm btn-extend" data-pkg="${p.id}" data-n="${p.ext_count}">收件人延期24h（已延${p.ext_count}次）</button>
      </td>
    </tr>`).join("") :
    `<tr><td colspan="8" class="empty" style="text-align:center;padding:20px">暂无超时包裹</td></tr>`;
  $$("#overdueBody .lnk-pkg").forEach((b) => b.onclick = () => openPackage(b.dataset.pkg));
  $$("#overdueBody .btn-extend").forEach((b) => b.onclick = async () => {
    try { await api(`/api/packages/${b.dataset.pkg}/extend`, { method: "POST", body: { hours: 24 } });
      toast("延期成功，取件时限已顺延 24 小时", "ok"); loadOverdue();
    } catch (e) { toast(e.message, "err"); }
  });
}

/* ---------------- 退回交接 ---------------- */
async function loadReturns() {
  const items = (await api("/api/returns")).items;
  $("#returnsBody").innerHTML = items.length ? items.map((r) => `
    <tr>
      <td class="mono">${esc(r.waybill)}</td>
      <td>${esc(r.company_name)}</td>
      <td>${esc(r.recipient_alias)}</td>
      <td>${esc(r.reason)}</td>
      <td>${fmt(r.ts)}</td>
      <td><button class="btn ghost sm lnk-rpkg" data-pkg="${r.package_id}">位置/轨迹</button></td>
      <td><span class="tag ${r.status}">${r.status === "pending" ? "待交接" : "拒收封存"}</span></td>
      <td class="btn-row" style="margin:0">
        ${r.status === "pending" ? `
          <button class="btn orange sm btn-handover" data-id="${r.id}">交接签收</button>
          <button class="btn danger sm btn-reject" data-id="${r.id}">退回拒收</button>` :
          `<span class="empty">${esc(r.reject_reason || "")}</span>`}
      </td>
    </tr>`).join("") :
    `<tr><td colspan="8" class="empty" style="text-align:center;padding:20px">退回队列为空</td></tr>`;
  $$("#returnsBody .lnk-rpkg").forEach((b) => b.onclick = () => openPackage(b.dataset.pkg));
  $$("#returnsBody .btn-handover").forEach((b) => b.onclick = async () => {
    const who = prompt("签收人（快递公司交接人，虚构姓名）", "仓管贺舟");
    if (!who) return;
    try { await api(`/api/returns/${b.dataset.id}/handover`, { method: "POST", body: { handed_to: who } });
      toast("退回交接完成，包裹状态已置为已退回", "ok"); loadReturns();
    } catch (e) { toast(e.message, "err"); }
  });
  $$("#returnsBody .btn-reject").forEach((b) => b.onclick = async () => {
    const reason = prompt("拒收原因（将写入异常凭证并封存 Q 架）", "运单污损无法核对归属");
    if (!reason) return;
    try { const r = await api(`/api/returns/${b.dataset.id}/reject`, { method: "POST", body: { reject_reason: reason } });
      toast(`已拒收并封存，凭证：${r.evidence.split("/").pop()}`, "warn"); loadReturns();
    } catch (e) { toast(e.message, "err"); }
  });
}

/* ---------------- 强开审批 ---------------- */
async function loadApprovals() {
  const items = (await api("/api/force-open")).items;
  const nameOf = (id) => META.users.find((u) => u.id === id)?.name || id;
  $("#approvalsBody").innerHTML = items.length ? items.map((r) => `
    <tr>
      <td>#${r.id}</td>
      <td>${esc(r.locker_name)} / <b>${esc(r.comp_code)}</b></td>
      <td>${esc(nameOf(r.applicant))}</td>
      <td style="max-width:240px">${esc(r.reason)}</td>
      <td>${fmt(r.ts)}</td>
      <td><span class="tag ${r.status === "rejected" ? "rejected-force" : r.status}">${
        { pending: "待审批", approved: "已批准", rejected: "已驳回", executed: "已执行" }[r.status]}</span></td>
      <td>${esc(r.note || "")}</td>
      <td class="btn-row" style="margin:0">
        ${r.status === "pending" && isProperty() ? `
          <button class="btn orange sm btn-approve" data-id="${r.id}">批准</button>
          <button class="btn danger sm btn-deny" data-id="${r.id}">驳回</button>` : ""}
        ${r.status === "approved" ? `<button class="btn sm btn-exec" data-id="${r.id}">执行强开并取件</button>` : ""}
        ${r.package_id ? `<button class="btn ghost sm lnk-fpkg" data-pkg="${r.package_id}">包裹</button>` : ""}
      </td>
    </tr>`).join("") :
    `<tr><td colspan="8" class="empty" style="text-align:center;padding:20px">暂无强开申请</td></tr>`;
  $$("#approvalsBody .btn-approve").forEach((b) => b.onclick = () => decideForce(b.dataset.id, true));
  $$("#approvalsBody .btn-deny").forEach((b) => b.onclick = () => decideForce(b.dataset.id, false));
  $$("#approvalsBody .btn-exec").forEach((b) => b.onclick = () => executeForce(b.dataset.id));
  $$("#approvalsBody .lnk-fpkg").forEach((b) => b.onclick = () => openPackage(b.dataset.pkg));
}

async function decideForce(id, approve) {
  const note = prompt(approve ? "物业审批意见（批准）" : "驳回原因", approve ? "核实客户身份与故障情况，同意强开" : "");
  if (note === null) return;
  try { await api(`/api/force-open/${id}/decide`, { method: "POST", body: { approve, note } });
    toast(approve ? "已批准，可执行强开" : "已驳回", approve ? "ok" : "warn"); loadApprovals();
  } catch (e) { toast(e.message, "err"); }
}

async function executeForce(id) {
  try {
    const r = await api(`/api/force-open/${id}/execute`, { method: "POST", body: {} });
    toast("已凭物业审批强制开门，原取件码作废；请完成取件关门回执", "warn");
    // 强开后直接走取件完成（门已开）
    if (r.package_id && confirm("柜门已强制打开。包裹现在取出吗？确定后将上报关门回执并结束占用。")) {
      await api(`/api/packages/${r.package_id}/pickup-complete`, { method: "POST", body: {} });
      toast("取件完成，故障格口保持维修态", "ok");
    }
    loadApprovals();
  } catch (e) { toast(e.message, "err"); }
}

/* ---------------- 包裹详情抽屉 ---------------- */
async function openPackage(packageId) {
  try {
    const d = await api(`/api/packages/${packageId}`);
    renderDrawer(d);
    showDrawer();
  } catch (e) {
    if (e.status === 403) toast(e.message, "err");
    else toast(e.message, "err");
  }
}

function renderDrawer(d) {
  const p = d.package;
  const co = META.companies.find((x) => x.id === p.company_id);
  $("#drawerTitle").textContent = `包裹 ${p.waybill}`;
  const sameBuildingShelves = META.shelves.filter((s) => s.building_id === p.building_id);
  const movable = ["stored", "on_shelf", "overdue"].includes(p.status);
  $("#drawerBody").innerHTML = `
    <dl class="kv">
      <dt>运单号</dt><dd class="mono">${esc(p.waybill)}</dd>
      <dt>状态</dt><dd><span class="tag ${p.status}">${STATUS_NAME[p.status]}</span></dd>
      <dt>快递公司</dt><dd>${esc(co?.name || p.company_id)}</dd>
      <dt>收件人代号</dt><dd>${esc(p.recipient_alias || "")}</dd>
      <dt>尺寸</dt><dd>${SIZE_NAME[p.size]}格口</dd>
      <dt>当前位置</dt><dd>${esc(d.position)}</dd>
      <dt>入柜时间</dt><dd>${fmt(p.stored_at)}</dd>
      <dt>存放时限</dt><dd>${fmt(p.deadline_at)}（已延期 ${p.ext_count} 次）</dd>
      ${p.picked_at ? `<dt>取出时间</dt><dd>${fmt(p.picked_at)}</dd>` : ""}
      ${p.note ? `<dt>备注</dt><dd>${esc(p.note)}</dd>` : ""}
    </dl>

    <div class="section-title">取件码链（转移即作废）</div>
    ${d.codes.map((c) => `<div style="margin-bottom:6px">
      <span class="code-chip ${c.status}">${esc(c.code)}</span>
      <span class="tag ${c.status === "active" ? "stored" : c.status === "used" ? "picked" : "returned"}">${
        { active: "有效", void: "已作废", used: "已使用" }[c.status]}</span>
      <span class="empty">${esc(c.issued_for)}${c.void_reason ? " · " + esc(c.void_reason) : ""}</span>
    </div>`).join("")}

    ${d.transfers.length ? `<div class="section-title">转移链（${d.transfers.length} 次）</div>
    ${d.transfers.map((t, i) => `<div class="notif-row">#${i + 1} ${fmt(t.ts)} · ${
      { full: "满仓", fault: "故障", size_mismatch: "尺寸不符" }[t.reason] || t.reason}
      <br><span class="empty">${esc(t.from_kind)} → ${esc(t.to_kind)} ｜ 旧码 ${esc(t.old_code || "—")} → 新码 ${esc(t.new_code || "待关门回执")}</span></div>`).join("")}` : ""}

    <div class="section-title">短信通知（含重试）</div>
    ${d.notifications.length ? d.notifications.map((n) => `<div class="notif-row">
      <span class="n-status tag ${n.status === "sent" ? "picked" : n.status === "failed" ? "sealed" : "pending"}">${
        { sent: "已送达", failed: `失败重试×${n.attempts}`, queued: "待发" }[n.status]}</span>
      ${fmt(n.ts)}<br><span class="empty">${esc(n.text)}</span>${n.receipt_id ? `<br><span class="empty">回执：${esc(n.receipt_id)}</span>` : ""}
      ${n.last_error ? `<br><span class="empty" style="color:var(--red)">${esc(n.last_error)}</span>` : ""}
    </div>`).join("") : "<p class='empty'>无通知</p>"}

    <div class="section-title">操作</div>
    <div class="btn-row">
      ${movable ? sameBuildingShelves.map((s) =>
        `<button class="btn ghost sm d-transfer-shelf" data-shelf="${s.id}">转 ${esc(s.name)}</button>`).join("") : ""}
      ${p.status === "sealed" && p.current_compartment_id ? `<button class="btn danger sm d-force-apply" data-comp="${p.current_compartment_id}">申请强制开柜</button>` : ""}
      ${["stored", "on_shelf", "overdue"].includes(p.status) ? `<button class="btn ghost sm d-extend">延期24h</button>` : ""}
      ${["stored", "on_shelf", "overdue"].includes(p.status) ? `<button class="btn ghost sm d-proxy">代取授权</button>` : ""}
    </div>

    <div class="section-title">事件轨迹</div>
    <ul class="timeline">
      ${d.events.slice().reverse().map((e) => `<li><div class="t-time">${fmt(e.ts)} · ${esc(e.actor || "")}</div>${esc(e.detail)}</li>`).join("")}
    </ul>`;

  $$(".d-transfer-shelf").forEach((b) => b.onclick = async () => {
    try {
      const r = await api(`/api/packages/${p.id}/transfer-shelf`, { method: "POST", body: { shelf_id: b.dataset.shelf } });
      toast(`已转移货架，旧码 ${r.old_code || "无"} 作废，新码 ${r.new_code}`, "ok");
      closeDrawer(); loadLockerView(); refreshCurrent();
    } catch (e) { toast(e.message, "err"); }
  });
  $(".d-force-apply")?.addEventListener("click", async () => {
    const reason = prompt("强开申请事由", "柜门故障，客户需取件");
    if (!reason) return;
    try { await api(`/api/compartments/${p.current_compartment_id}/force-open/apply`, { method: "POST", body: { reason } });
      toast("强开申请已提交，等待物业审批", "ok"); closeDrawer();
    } catch (e) { toast(e.message, "err"); }
  });
  $(".d-extend")?.addEventListener("click", async () => {
    try { const r = await api(`/api/packages/${p.id}/extend`, { method: "POST", body: {} });
      toast(`延期成功至 ${fmt(r.deadline_at)}`, "ok"); openPackage(p.id);
    } catch (e) { toast(e.message, "err"); }
  });
  $(".d-proxy")?.addEventListener("click", async () => {
    const name = prompt("被授权人姓名（虚构）", "陈默");
    if (!name) return;
    try { const r = await api(`/api/packages/${p.id}/proxy`, { method: "POST", body: { authorized_name: name } });
      toast(`授权码 ${r.auth_code}（${name}），前台代取时核验`, "ok"); openPackage(p.id);
    } catch (e) { toast(e.message, "err"); }
  });
}

function showDrawer() { $("#drawer").classList.add("show"); $("#drawerMask").classList.add("show"); }
function closeDrawer() { $("#drawer").classList.remove("show"); $("#drawerMask").classList.remove("show"); }

init().catch((e) => toast("初始化失败：" + e.message, "err"));
