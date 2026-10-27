"use strict";
// Landmark reader: one card = one image x one landmark group. Coordinates are kept in the
// 512 px frame of the stored image; the display is aspect-corrected with the original size.

const $ = (s) => document.querySelector(s);
const api = (u) => fetch(u).then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); });
const post = (u, b) => fetch(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) })
  .then(async (r) => { const j = await r.json().catch(() => ({})); if (!r.ok) throw new Error(j.detail || r.status); return j; });

const S = {
  mode: "read", rid: null, pos: 0, total: 0, card: null, pts: [], sel: 0,
  img: null, maskImg: null, showMasks: false, hide: false, full: false,
  drag: null, activeMs: 0, lastInput: Date.now(), nEvents: 0, refKind: "main",
  view: { x0: 0, y0: 0, x1: 512, y1: 512, sx: 1, sy: 1 },
};
const VIEW_W = 720, VIEW_H_MAX = 760, HIT = 10, DRAG_MIN = 2;

// ---------------- start ----------------
async function start() {
  const cfg = await api("/api/config");
  S.mode = cfg.mode;
  const box = $("#readerButtons");
  const ids = cfg.mode === "adjudicate" ? ["adjudicator"] : cfg.readers;
  if (cfg.mode === "adjudicate") $("#pickHint").textContent = "Adjudication: resolve the landmarks the two readers disagreed on.";
  for (const r of ids) {
    const b = document.createElement("button");
    b.textContent = r;
    b.onclick = () => enter(r);
    box.appendChild(b);
  }
}

async function enter(rid) {
  S.rid = rid;
  $("#pick").hidden = true;
  $("#main").hidden = false;
  $("#who").textContent = S.mode === "adjudicate" ? "adjudication" : `reader ${rid}`;
  $("#adjLegend").hidden = S.mode !== "adjudicate";
  $("#flagRow").hidden = S.mode === "adjudicate";
  $("#comment").hidden = S.mode === "adjudicate";
  const s = await api(S.mode === "adjudicate" ? "/api/adj/session" : `/api/r/${rid}/session`);
  S.total = s.total;
  if (!s.total) { $("#banner").textContent = "Nothing to do."; return; }
  await load(Math.min(s.cursor, s.total - 1));
}

// ---------------- loading a card ----------------
async function load(pos) {
  $("#msg").textContent = "";
  const c = S.mode === "adjudicate" ? await api(`/api/adj/item/${pos}`) : await api(`/api/r/${S.rid}/card/${pos}`);
  S.card = c; S.pos = pos; S.full = c.full; S.activeMs = 0; S.nEvents = 0;
  S.pts = S.mode === "adjudicate" ? adjPoints(c) : c.points.map((p) => ({ ...p, n_edits: 0 }));
  S.sel = Math.max(0, S.pts.findIndex((p) => p.status === "missing"));
  S.img = await loadImage(`/api/img/${c.img}`);
  S.maskImg = null;
  if (S.showMasks && S.mode !== "adjudicate") S.maskImg = await loadImage(`/api/r/${S.rid}/card/${pos}/masks.png`);
  $("#flag").checked = !!c.flag;
  $("#comment").value = c.comment || "";
  $("#groupTitle").textContent = `${c.gid} · ${c.group.name}${c.batch ? " · " + c.batch : ""}`;
  $("#groupRegion").textContent = `${c.group.region} · ${c.view}${c.mode === "scratch" ? " · place from scratch" : ""}`;
  $("#groupDesc").textContent = c.group.description;
  $("#refSeries").hidden = !c.refs.series;
  setRef(c.refs.series && S.refKind === "series" ? "series" : "main");
  $("#cardPos").textContent = `card ${pos + 1} / ${S.total}`;
  updateProgress();
  layout();
  renderList();
  draw();
}

function loadImage(src) {
  return new Promise((res, rej) => { const i = new Image(); i.onload = () => res(i); i.onerror = rej; i.src = src; });
}

function setRef(kind) {
  const c = S.card;
  S.refKind = kind;
  const src = c.refs[kind] || c.refs.main;
  $("#ref").hidden = !src;
  if (src) $("#ref").src = src;
  $("#refMain").classList.toggle("on", kind === "main");
  $("#refSeries").classList.toggle("on", kind === "series");
}

async function updateProgress() {
  if (S.mode === "adjudicate") {
    const s = await api("/api/adj/session");
    $("#progressBar").style.width = `${(100 * s.done) / Math.max(1, s.total)}%`;
    $("#groupProgress").textContent = `${s.done} resolved`;
    return;
  }
  const s = await api(`/api/r/${S.rid}/session`);
  $("#progressBar").style.width = `${(100 * s.done) / s.total}%`;
  const g = s.per_group[S.card.gid];
  $("#groupProgress").textContent = `${S.card.gid}: ${g.done}/${g.total} · total ${s.done}/${s.total}`;
}

// ---------------- geometry ----------------
function layout() {
  const c = S.card;
  const [x0, y0, x1, y1] = S.full ? [0, 0, 512, 512] : c.box;
  // the 512 px image is squashed from the original; stretch y back to the original aspect
  const ar = c.orig_wh ? c.orig_wh[1] / c.orig_wh[0] : 1;
  let w = VIEW_W, h = (VIEW_W * ar * (y1 - y0)) / (x1 - x0);
  if (h > VIEW_H_MAX) { w *= VIEW_H_MAX / h; h = VIEW_H_MAX; }
  const cv = $("#view");
  cv.width = Math.round(w); cv.height = Math.round(h);
  S.view = { x0, y0, x1, y1, sx: cv.width / (x1 - x0), sy: cv.height / (y1 - y0) };
  $("#btnCrop").textContent = S.full ? "Crop" : "Full image";
  const th = $("#thumb");
  th.width = 130; th.height = Math.round(130 * ar);
}
const toCanvas = (x, y) => [(x - S.view.x0) * S.view.sx, (y - S.view.y0) * S.view.sy];
const toImage = (px, py) => [S.view.x0 + px / S.view.sx, S.view.y0 + py / S.view.sy];

// ---------------- drawing ----------------
function draw() {
  const cv = $("#view"), ctx = cv.getContext("2d");
  const v = S.view;
  ctx.clearRect(0, 0, cv.width, cv.height);
  ctx.filter = `brightness(${$("#bright").value}%) contrast(${$("#contrast").value}%)`;
  ctx.imageSmoothingQuality = "high";
  ctx.drawImage(S.img, v.x0, v.y0, v.x1 - v.x0, v.y1 - v.y0, 0, 0, cv.width, cv.height);
  ctx.filter = "none";
  if (S.showMasks && S.maskImg) ctx.drawImage(S.maskImg, v.x0, v.y0, v.x1 - v.x0, v.y1 - v.y0, 0, 0, cv.width, cv.height);
  if (!S.hide) {
    if (S.mode === "adjudicate") drawAdj(ctx); else drawPoints(ctx);
  }
  drawThumb();
  banner();
}

function drawPoints(ctx) {
  const at = {};
  S.pts.forEach((p) => { if (p.xy && p.status !== "not_visible" && p.status !== "missing") at[p.key] = toCanvas(...p.xy); });
  ctx.strokeStyle = "rgba(255,209,102,.85)"; ctx.lineWidth = 1.2;
  for (const [a, b] of S.card.edges || []) {
    if (at[a] && at[b]) { ctx.beginPath(); ctx.moveTo(...at[a]); ctx.lineTo(...at[b]); ctx.stroke(); }
  }
  const labelled = new Set();
  S.pts.forEach((p, i) => {
    const q = at[p.key];
    if (!q) return;
    const sel = i === S.sel;
    ctx.beginPath(); ctx.arc(q[0], q[1], sel ? 7 : 5, 0, 2 * Math.PI);
    ctx.fillStyle = p.colour; ctx.fill();
    ctx.lineWidth = sel ? 3 : 1.5; ctx.strokeStyle = sel ? "#ffffff" : "#000000"; ctx.stroke();
    if (p.unsure) { ctx.fillStyle = "#ffd166"; ctx.font = "bold 12px system-ui"; ctx.fillText("?", q[0] + 7, q[1] + 4); }
    const inst = p.label.split(" ")[0];
    if (S.card.batch && !labelled.has(inst)) {
      labelled.add(inst);
      ctx.font = "11px system-ui"; ctx.fillStyle = "rgba(0,0,0,.6)";
      const t = p.label.split(" ").slice(0, p.label.startsWith("post") || p.label.startsWith("ante") ? 3 : 1).join(" ");
      const w = ctx.measureText(t).width;
      ctx.fillRect(q[0] + 8, q[1] - 16, w + 6, 14);
      ctx.fillStyle = "#fff"; ctx.fillText(t, q[0] + 11, q[1] - 5);
    }
  });
}

function drawThumb() {
  const th = $("#thumb"), ctx = th.getContext("2d");
  ctx.drawImage(S.img, 0, 0, th.width, th.height);
  const v = S.view, k = th.width / 512, ky = th.height / 512;
  ctx.strokeStyle = "#2a9d8f"; ctx.lineWidth = 2;
  ctx.strokeRect(v.x0 * k, v.y0 * ky, (v.x1 - v.x0) * k, (v.y1 - v.y0) * ky);
}

function banner() {
  const p = S.pts[S.sel];
  let t = "";
  if (S.mode === "adjudicate") t = p && p.choice === "new" && !p.xy ? `Place: ${p.label}` : "";
  else if (p && p.status === "missing") t = `Place: ${p.label} (click) — or n if not visible`;
  $("#banner").textContent = t;
}

// ---------------- list ----------------
const STATUS_TEXT = { ok: "ok", moved: "moved", added: "placed", not_visible: "not visible", missing: "missing" };
function renderList() {
  const ol = $("#points");
  ol.innerHTML = "";
  S.pts.forEach((p, i) => {
    const li = document.createElement("li");
    if (i === S.sel) li.classList.add("sel");
    li.title = [p.rule && "Rule: " + p.rule, p.method && "Method: " + p.method].filter(Boolean).join("\n");
    const dot = document.createElement("span"); dot.className = "dot"; dot.style.background = p.colour;
    const lab = document.createElement("span"); lab.className = "lab"; lab.textContent = p.label;
    li.append(dot, lab);
    if (S.mode === "adjudicate") {
      const ch = document.createElement("span"); ch.className = "choices";
      for (const opt of ["A", "B", "mean", "new", "not_visible"]) {
        if ((opt === "A" && !p.a) || (opt === "B" && !p.b) || (opt === "mean" && !(p.a && p.b))) continue;
        const b = document.createElement("button");
        b.textContent = opt === "not_visible" ? "not vis." : opt;
        if (p.choice === opt) b.classList.add("on");
        b.onclick = (e) => { e.stopPropagation(); p.choice = opt; if (opt !== "new") p.xy = null; S.sel = i; renderList(); draw(); };
        ch.appendChild(b);
      }
      const r = document.createElement("span"); r.className = "muted"; r.textContent = p.reason || "";
      li.append(ch, r);
    } else {
      const st = document.createElement("span"); st.className = `st ${p.status}`; st.textContent = STATUS_TEXT[p.status];
      li.appendChild(st);
      if (p.unsure) { const u = document.createElement("span"); u.className = "uns"; u.textContent = "unsure"; li.appendChild(u); }
    }
    li.onclick = () => { S.sel = i; renderList(); draw(); };
    ol.appendChild(li);
  });
  const sel = ol.children[S.sel];
  if (sel) sel.scrollIntoView({ block: "nearest" });
  $("#btnSubmit").disabled = !complete();
}

function complete() {
  if (S.mode === "adjudicate") return S.pts.every((p) => p.choice && (p.choice !== "new" || p.xy));
  return S.pts.every((p) => p.status !== "missing");
}

// ---------------- interaction ----------------
function hitTest(px, py) {
  let best = -1, bd = HIT;
  S.pts.forEach((p, i) => {
    const xy = S.mode === "adjudicate" ? (p.choice === "new" ? p.xy : null) : (p.status !== "missing" && p.status !== "not_visible" ? p.xy : null);
    if (!xy) return;
    const [x, y] = toCanvas(...xy);
    const d = Math.hypot(x - px, y - py);
    if (d < bd) { bd = d; best = i; }
  });
  return best;
}

function canvasPos(e) {
  const r = $("#view").getBoundingClientRect();
  return [((e.clientX - r.left) * $("#view").width) / r.width, ((e.clientY - r.top) * $("#view").height) / r.height];
}

function onDown(e) {
  touch();
  const [px, py] = canvasPos(e);
  const hit = hitTest(px, py);
  if (hit >= 0) {
    S.sel = hit;
    const p = S.pts[hit];
    // keep the grab offset so an off-centre grab does not make the point jump to the cursor
    const [qx, qy] = toCanvas(...p.xy);
    S.drag = { i: hit, from: p.xy.slice(), status: p.status, moved: false, dx: px - qx, dy: py - qy, sx: px, sy: py };
    $("#view").setPointerCapture(e.pointerId);
  } else {
    const p = S.pts[S.sel];
    if (p && S.mode !== "adjudicate" && (p.status === "missing" || p.status === "not_visible")) {
      p.xy = toImage(px, py).map(clamp512);
      p.status = p.prefill ? "moved" : "added";
      p.n_edits++; S.nEvents++;
      nextUnresolved();
    } else if (p && S.mode === "adjudicate" && p.choice === "new") {
      p.xy = toImage(px, py).map(clamp512);
      S.nEvents++;
    }
  }
  renderList(); draw();
}

function onMove(e) {
  if (!S.drag) return;
  const p = S.pts[S.drag.i];
  const [px, py] = canvasPos(e);
  // a click to select a point is not a move
  if (!S.drag.moved && Math.hypot(px - S.drag.sx, py - S.drag.sy) < DRAG_MIN) return;
  p.xy = toImage(px - S.drag.dx, py - S.drag.dy).map(clamp512);
  if (!S.drag.moved && S.mode !== "adjudicate") { S.drag.moved = true; p.status = p.prefill ? "moved" : "added"; p.n_edits++; S.nEvents++; }
  S.drag.moved = true;
  draw();
}

function onUp() {
  if (S.drag) { S.drag = null; renderList(); draw(); }
}

const clamp512 = (v) => Math.min(511.99, Math.max(0, v));

function nextUnresolved() {
  const i = S.pts.findIndex((p) => (S.mode === "adjudicate" ? !p.choice : p.status === "missing"));
  if (i >= 0) S.sel = i;
}

function setNotVisible() {
  const p = S.pts[S.sel];
  if (!p || S.mode === "adjudicate") return;
  p.status = "not_visible"; p.n_edits++; S.nEvents++;
  nextUnresolved(); renderList(); draw();
}
function toggleUnsure() { const p = S.pts[S.sel]; if (p && S.mode !== "adjudicate") { p.unsure = !p.unsure; renderList(); draw(); } }
function resetPoint() {
  const p = S.pts[S.sel];
  if (!p || S.mode === "adjudicate") return;
  p.xy = p.prefill ? p.prefill.slice() : null;
  p.status = p.prefill ? "ok" : "missing";
  renderList(); draw();
}

async function toggleMasks() {
  if (S.mode === "adjudicate") return;
  S.showMasks = !S.showMasks;
  $("#btnMasks").textContent = `Masks: ${S.showMasks ? "on" : "off"}`;
  if (S.showMasks && !S.maskImg) S.maskImg = await loadImage(`/api/r/${S.rid}/card/${S.pos}/masks.png`);
  draw();
}

function toggleFull() { S.full = !S.full; layout(); draw(); }

async function submit() {
  if (!complete()) { $("#msg").textContent = "Resolve every landmark first (place it or mark it not visible)."; return; }
  try {
    let r;
    if (S.mode === "adjudicate") {
      const keys = {};
      S.pts.forEach((p) => { keys[p.key] = { choice: p.choice, xy: p.choice === "new" ? p.xy : null }; });
      r = await post(`/api/adj/item/${S.pos}/resolve`, { keys });
    } else {
      r = await post(`/api/r/${S.rid}/card/${S.pos}/submit`, {
        points: S.pts.map((p) => ({ key: p.key, status: p.status, unsure: p.unsure, xy: p.xy, n_edits: p.n_edits })),
        flag: $("#flag").checked ? "not_assessable" : null, comment: $("#comment").value,
        active_ms: S.activeMs, n_events: S.nEvents,
      });
    }
    if (S.pos >= S.total - 1) { $("#banner").textContent = "All cards done — thank you."; updateProgress(); return; }
    await load(r.next);
  } catch (err) { $("#msg").textContent = `Not saved: ${err.message}`; }
}

// ---------------- adjudication ----------------
function adjPoints(c) {
  return c.keys.map((k) => {
    const prev = c.resolved && c.resolved.keys[k.key];
    return { ...k, choice: prev ? prev.choice : null, xy: prev ? prev.xy : null, status: "adj" };
  });
}

function drawAdj(ctx) {
  S.pts.forEach((p, i) => {
    const sel = i === S.sel;
    ctx.strokeStyle = sel ? "#ffffff" : p.colour; ctx.lineWidth = sel ? 2.5 : 1.5;
    if (p.a) { const [x, y] = toCanvas(...p.a); ctx.beginPath(); ctx.arc(x, y, 6, 0, 2 * Math.PI); ctx.stroke(); }
    if (p.b) { const [x, y] = toCanvas(...p.b); ctx.strokeRect(x - 5, y - 5, 10, 10); }
    if (p.a && p.b) {
      const [x, y] = toCanvas((p.a[0] + p.b[0]) / 2, (p.a[1] + p.b[1]) / 2);
      ctx.beginPath(); ctx.moveTo(x, y - 6); ctx.lineTo(x + 6, y); ctx.lineTo(x, y + 6); ctx.lineTo(x - 6, y); ctx.closePath(); ctx.stroke();
    }
    if (p.choice === "new" && p.xy) {
      const [x, y] = toCanvas(...p.xy);
      ctx.fillStyle = p.colour; ctx.beginPath(); ctx.arc(x, y, 5, 0, 2 * Math.PI); ctx.fill();
    }
  });
}

// ---------------- active time ----------------
function touch() { S.lastInput = Date.now(); }
setInterval(() => {
  if (!S.card || document.hidden) return;
  if (Date.now() - S.lastInput < 60000) S.activeMs += 1000;
  const s = Math.floor(S.activeMs / 1000);
  $("#timer").textContent = `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}, 1000);

// ---------------- wiring ----------------
function wire() {
  const cv = $("#view");
  cv.addEventListener("pointerdown", onDown);
  cv.addEventListener("pointermove", onMove);
  cv.addEventListener("pointerup", onUp);
  $("#btnSubmit").onclick = submit;
  $("#btnPrev").onclick = () => S.pos > 0 && load(S.pos - 1);
  $("#btnCrop").onclick = toggleFull;
  $("#btnMasks").onclick = toggleMasks;
  $("#btnHide").onpointerdown = () => { S.hide = true; draw(); };
  $("#btnHide").onpointerup = () => { S.hide = false; draw(); };
  $("#bright").oninput = draw;
  $("#contrast").oninput = draw;
  $("#btnResetView").onclick = () => { $("#bright").value = 100; $("#contrast").value = 100; draw(); };
  $("#refMain").onclick = () => setRef("main");
  $("#refSeries").onclick = () => setRef("series");
  $("#flag").onchange = () => {
    if ($("#flag").checked) { S.pts.forEach((p) => { p.status = "not_visible"; }); renderList(); draw(); }
  };
  $("#helpBtn").onclick = () => { $("#help").hidden = false; };
  $("#helpClose").onclick = () => { $("#help").hidden = true; };
  document.addEventListener("keydown", (e) => {
    const t = e.target.tagName;
    if (t === "INPUT" || t === "TEXTAREA" || t === "SELECT" || !S.card) return;
    touch();
    const k = e.key;
    if (k === "Escape") {
      if (!$("#help").hidden) { $("#help").hidden = true; return; }
      if (S.drag) { const p = S.pts[S.drag.i]; p.xy = S.drag.from; p.status = S.drag.status; S.drag = null; renderList(); draw(); }
      return;
    }
    if (k === "?") { $("#help").hidden = !$("#help").hidden; return; }
    if (k === "Enter") { e.preventDefault(); submit(); }
    else if (k === "Backspace") { e.preventDefault(); if (S.pos > 0) load(S.pos - 1); }
    else if (k === "Tab" || k === "ArrowDown") { e.preventDefault(); S.sel = (S.sel + (e.shiftKey ? S.pts.length - 1 : 1)) % S.pts.length; renderList(); draw(); }
    else if (k === "ArrowUp") { e.preventDefault(); S.sel = (S.sel + S.pts.length - 1) % S.pts.length; renderList(); draw(); }
    else if (k === "n") setNotVisible();
    else if (k === "u") toggleUnsure();
    else if (k === "r") resetPoint();
    else if (k === "m") toggleMasks();
    else if (k === "c") toggleFull();
    else if (k === "1") setRef("main");
    else if (k === "2" && S.card.refs.series) setRef("series");
    else if (k === "h" && !S.hide) { S.hide = true; draw(); }
  });
  document.addEventListener("keyup", (e) => { if (e.key === "h" && S.hide) { S.hide = false; draw(); } });
}

wire();
start();
