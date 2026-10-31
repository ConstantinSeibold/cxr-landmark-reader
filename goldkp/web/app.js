"use strict";
// Landmark reader: one card = one image x one landmark group. Coordinates are kept in the
// 512 px frame of the stored image; the display is aspect-corrected with the original size.

const $ = (s) => document.querySelector(s);
const api = (u) => fetch(u).then((r) => { if (!r.ok) throw new Error(r.status); return r.json(); });
const post = (u, b) => fetch(u, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(b) })
  .then(async (r) => { const j = await r.json().catch(() => ({})); if (!r.ok) throw new Error(j.detail || r.status); return j; });

const S = {
  mode: "read", rid: null, pos: 0, total: 0, card: null, pts: [], sel: 0,
  img: null, outlines: null, showMasks: false, hide: false, full: false, hist: [], redo: [],
  drag: null, pan: null, zoomBox: null, ruler: false, line: null, activeMs: 0, lastInput: Date.now(), nEvents: 0, refKind: "main",
  view: { x0: 0, y0: 0, x1: 512, y1: 512, sx: 1, sy: 1 },
};
const VIEW_MIN = 320, THUMB_W = 96, HIT = 10, DRAG_MIN = 2, MAX_GAIN = 2.5;
// zoom: smallest view side in the 512 frame (about 20x the whole image); a press that moves less
// than PAN_MIN screen pixels is a click (places a point), more is a pan
const ZOOM_MIN_SIDE = 24, PAN_MIN = 4;

// ---------------- browser cache ----------------
// kept in this browser only (localStorage): the last reader and card, view settings, and per card
// the changes not yet submitted, so a reload or a server restart continues where the reader left off.
// Submitted answers live on the server; a draft is dropped when its card is submitted.
const LS = {
  get(k) { try { return JSON.parse(localStorage.getItem(`goldkp:${k}`)); } catch (_) { return null; } },
  set(k, v) { try { localStorage.setItem(`goldkp:${k}`, JSON.stringify(v)); } catch (_) { /* full or blocked */ } },
  del(k) { try { localStorage.removeItem(`goldkp:${k}`); } catch (_) { /* blocked */ } },
};
// shortcut labels in Mac notation (⌘ ⌥ ⇧, modifiers in Apple's order) on a Mac; the handlers
// already accept Cmd for Ctrl
const IS_MAC = /mac|iphone|ipad/i.test(navigator.userAgentData?.platform || navigator.platform || "");
function keys(t) {
  if (!IS_MAC) return t;
  return t.replace(/Ctrl\+Shift\+/g, "⇧⌘").replace(/,? ?\/? ?Ctrl\+Y/g, "").replace(/Ctrl\+/g, "⌘")
    .replace(/Alt \+ /g, "⌥ ").replace(/Alt\+/g, "⌥").replace(/Shift\+/g, "⇧").replace(/Shift:/g, "⇧:");
}
function macLabels() {
  if (!IS_MAC) return;
  document.querySelectorAll("[title]").forEach((el) => { el.title = keys(el.title); });
  document.querySelectorAll("#help td").forEach((el) => { el.textContent = keys(el.textContent); });
}

const draftKey = () => `draft:${S.rid}:${S.card.card_id}`;

function saveDraft() {
  if (S.mode === "adjudicate" || !S.card || S.base == null) return;
  const state = stateNow(), comment = $("#comment").value;
  const dirty = state !== S.base || comment !== S.baseComment;
  if (!dirty && !S.activeMs) { LS.del(draftKey()); return; }
  LS.set(draftKey(), { dirty, state, comment, activeMs: S.activeMs, nEvents: S.nEvents, full: S.full, zoomBox: S.zoomBox });
}

// apply a saved draft to the freshly loaded card; undo goes back to the submitted version
function restoreDraft() {
  const d = LS.get(draftKey());
  if (!d) return;
  S.activeMs = d.activeMs || 0; S.nEvents = d.nEvents || 0;
  if (!d.dirty) return;
  const st = JSON.parse(d.state);
  if (st.pts.length !== S.pts.length) { LS.del(draftKey()); return; }
  S.hist = [S.base];
  st.pts.forEach((q, i) => Object.assign(S.pts[i], q));
  $("#flag").checked = st.flag;
  $("#comment").value = d.comment;
  if (!S.card.full) S.full = !!d.full;
  S.zoomBox = d.zoomBox || null;
  $("#msg").textContent = keys("Unsaved changes restored (Ctrl+Z to go back).");
}

function savePrefs() {
  LS.set("prefs", { bright: $("#bright").value, contrast: $("#contrast").value, masks: S.showMasks });
}
function loadPrefs() {
  const p = LS.get("prefs");
  if (!p) return;
  $("#bright").value = p.bright; $("#contrast").value = p.contrast;
  S.showMasks = !!p.masks && S.mode !== "adjudicate";
  $("#btnMasks").textContent = `Outlines: ${S.showMasks ? "on" : "off"}`;
}

// ---------------- start ----------------
async function start() {
  const cfg = await api("/api/config");
  S.mode = cfg.mode;
  loadPrefs();
  const box = $("#readerButtons");
  const ids = cfg.mode === "adjudicate" ? ["adjudicator"] : cfg.readers;
  if (cfg.mode === "adjudicate") $("#pickHint").textContent = "Adjudication: resolve the landmarks the two readers disagreed on.";
  for (const r of ids) {
    const b = document.createElement("button");
    b.textContent = r;
    b.onclick = () => enter(r);
    box.appendChild(b);
  }
  // continue as the last reader, on the last card
  const last = LS.get("last");
  if (last && last.mode === cfg.mode && ids.includes(last.rid)) enter(last.rid, last);
  else if (ids.length === 1) enter(ids[0]);           // a single reader needs no picking
}

// last: where this browser left off ({pos, card_id}); the card is found again even when the
// reading order changed in between
async function enter(rid, last = null) {
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
  if (S.mode !== "adjudicate") await buildJumpMenu(); else { $("#jumpGroup").hidden = true; $("#btnExport").hidden = true; }
  let pos = Math.min(s.cursor, s.total - 1);
  if (last && last.card_id && S.mode !== "adjudicate") {
    try { pos = (await api(`/api/r/${rid}/find?card_id=${encodeURIComponent(last.card_id)}`)).pos; } catch (_) { /* gone: first open card */ }
  } else if (last && Number.isInteger(last.pos) && last.pos >= 0 && last.pos < s.total) pos = last.pos;
  await load(pos);
}

// ---------------- loading a card ----------------
// everything of the next card is fetched first and switched in one step, so the screen never shows
// one card while edits go to another; while it loads, input is ignored; a newer load wins
let loadSeq = 0;
async function load(pos, fromSubmit = false) {
  if (S.submitting && !fromSubmit) return;         // navigation waits for a running submit
  const seq = ++loadSeq;
  S.loading = true;
  if (S.base != null) saveDraft();                 // keep unsaved changes of the card being left
  try {
    const c = S.mode === "adjudicate" ? await api(`/api/adj/item/${pos}`) : await api(`/api/r/${S.rid}/card/${pos}`);
    const img = await loadImage(`/api/img/${c.img}`);
    const outlines = S.showMasks && S.mode !== "adjudicate" ? await api(`/api/r/${S.rid}/card/${pos}/outlines`) : null;
    if (seq !== loadSeq) return;
    commitCard(c, pos, img, outlines);
  } catch (err) {
    if (seq === loadSeq) $("#msg").textContent = `Could not load card ${pos + 1}: ${err.message}`;
  } finally {
    if (seq === loadSeq) S.loading = false;
  }
}

function commitCard(c, pos, img, outlines) {
  $("#msg").textContent = "";
  S.card = c; S.pos = pos; S.full = c.full; S.activeMs = 0; S.nEvents = 0; S.img = img; S.outlines = outlines;
  S.pts = S.mode === "adjudicate" ? adjPoints(c) : c.points.map((p) => ({ ...p, n_edits: 0 }));
  S.sel = Math.max(0, S.pts.findIndex((p) => p.status === "missing"));
  S.hist = []; S.redo = []; S.line = null; S.zoomBox = null; S.lastNudge = null; S.drag = null; S.pan = null;
  S.preFlag = null;
  renderLegend();
  $("#flag").checked = !!c.flag;
  $("#comment").value = c.comment || "";
  S.base = stateNow(); S.baseComment = $("#comment").value;
  if (S.mode !== "adjudicate") restoreDraft();
  LS.set("last", { mode: S.mode, rid: S.rid, pos, card_id: c.card_id });
  $("#groupTitle").textContent = `${c.gid} · ${c.group.name}${c.batch ? " · " + c.batch : ""}`;
  $("#groupRegion").textContent = `${c.group.region} · ${c.view}${c.mode === "scratch" ? " · place from scratch" : ""}`;
  $("#groupDesc").textContent = c.group.description;
  $("#refSeries").hidden = !c.refs.series;
  setRef(c.refs.series && S.refKind === "series" ? "series" : "main");
  $("#jump").value = pos + 1;
  $("#cardPos").textContent = `/ ${S.total}`;
  updateProgress();
  layout();
  renderList();
  if (S.ruler) setRuler(true); else draw();        // refresh the warning for this image's scale
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
  $("#refChips").hidden = !c.refs.series;
  $("#refMain").classList.toggle("on", kind === "main");
  $("#refSeries").classList.toggle("on", kind === "series");
}

async function updateProgress() {
  if (S.mode === "adjudicate") {
    const s = await api("/api/adj/session");
    $("#progressBar").style.width = `${(100 * s.done) / Math.max(1, s.total)}%`;
    $("#totalProgress").textContent = `${s.done} resolved`;
    return;
  }
  const s = await api(`/api/r/${S.rid}/session`);
  $("#progressBar").style.width = `${(100 * s.done) / s.total}%`;
  const g = (S.card.block && s.per_block_group[`${S.card.block}|${S.card.gid}`]) || s.per_group[S.card.gid];
  $("#groupProgress").textContent = `${g.done} / ${g.total} done in this group`;
  $("#totalProgress").textContent = `${s.done} / ${s.total} done`;
}

// ---------------- geometry ----------------
function layout() {
  const c = S.card;
  // a custom zoom (wheel, +/-) replaces the card's view box; every box is square in the 512 frame,
  // so the canvas size does not change with the zoom
  const [x0, y0, x1, y1] = S.zoomBox || (S.full ? [0, 0, 512, 512] : squareBox(c.box));
  // the 512 px image is squashed from the original; stretch y back to the original aspect
  const ar = c.orig_wh ? c.orig_wh[1] / c.orig_wh[0] : 1;
  // fit the view into the free stage, keeping the aspect
  const st = $("#stage"), maxW = Math.max(VIEW_MIN, st.clientWidth), maxH = Math.max(VIEW_MIN, st.clientHeight - 12);
  const aspect = (ar * (y1 - y0)) / (x1 - x0);
  let w = maxW, h = maxW * aspect;
  if (h > maxH) { w = maxH / aspect; h = maxH; }
  const cv = $("#view");
  cv.width = Math.round(w); cv.height = Math.round(h);
  S.view = { x0, y0, x1, y1, sx: cv.width / (x1 - x0), sy: cv.height / (y1 - y0) };
  S.window = S.full ? "" : autoWindow();
  // the button shows the current view; cards without a zoomed view (the whole image is the crop) disable it
  const b = $("#btnCrop");
  b.disabled = c.full;
  b.textContent = c.full ? "View: whole image" : S.full ? "View: whole image (c)" : "View: zoomed (c)";
  b.title = c.full ? "This card is always shown as the whole image" : "Switch between the zoomed crop and the whole image (c)";
  const th = $("#thumb");
  th.width = THUMB_W; th.height = Math.round(THUMB_W * ar);
}
// studies built before cards.crop_box shifted its square inward cut it at the image border into
// a thin strip; widen such a box back to a square inside the image
function squareBox([x0, y0, x1, y1]) {
  const side = Math.min(512, Math.max(x1 - x0, y1 - y0));
  const fit = (a, b) => { const a0 = Math.min(Math.max(0, (a + b) / 2 - side / 2), 512 - side); return [a0, a0 + side]; };
  const [nx0, nx1] = fit(x0, x1), [ny0, ny1] = fit(y0, y1);
  return [nx0, ny0, nx1, ny1];
}
// zoomed crops are windowed to their own grey range (1st to 99th percentile), as radiology viewers
// do on load: a crop of a bright region (upper abdomen under the diaphragm) otherwise shows as white.
// brightness(b) then contrast(c) maps v to (v*b - .5)*c + .5; with b = 1/(2 mid) and c = 2 gain mid
// that is gain * (v - mid) + .5, so the window centre lands on mid grey
function autoWindow() {
  const v = S.view, n = 64, cv = document.createElement("canvas");
  cv.width = cv.height = n;
  const ctx = cv.getContext("2d", { willReadFrequently: true });
  ctx.drawImage(S.img, v.x0, v.y0, v.x1 - v.x0, v.y1 - v.y0, 0, 0, n, n);
  const px = ctx.getImageData(0, 0, n, n).data, lum = [];
  for (let i = 0; i < px.length; i += 4) lum.push((px[i] + px[i + 1] + px[i + 2]) / 765);
  lum.sort((a, b) => a - b);
  const lo = lum[Math.floor(lum.length * 0.01)], hi = lum[Math.floor(lum.length * 0.99)], mid = (lo + hi) / 2;
  if (hi - lo > 0.8 || mid < 0.02) return "";             // already full range, or black
  // at most MAX_GAIN: a near-saturated crop holds only a few grey levels and turns into JPEG blocks
  const gain = Math.min(1 / Math.max(hi - lo, 1e-3), MAX_GAIN);
  return `brightness(${1 / (2 * mid)}) contrast(${2 * gain * mid})`;
}
const toCanvas = (x, y) => [(x - S.view.x0) * S.view.sx, (y - S.view.y0) * S.view.sy];
const toImage = (px, py) => [S.view.x0 + px / S.view.sx, S.view.y0 + py / S.view.sy];

// ---------------- drawing ----------------
function draw() {
  const cv = $("#view"), ctx = cv.getContext("2d");
  const v = S.view;
  ctx.clearRect(0, 0, cv.width, cv.height);
  ctx.filter = `${S.window} brightness(${$("#bright").value}%) contrast(${$("#contrast").value}%)`;
  ctx.imageSmoothingQuality = "high";
  ctx.drawImage(S.img, v.x0, v.y0, v.x1 - v.x0, v.y1 - v.y0, 0, 0, cv.width, cv.height);
  ctx.filter = "none";
  if (S.showMasks && S.outlines) drawOutlines(ctx);
  if (!S.hide) {
    if (S.mode === "adjudicate") drawAdj(ctx); else drawPoints(ctx);
  }
  if (S.line) drawRuler(ctx);
  drawThumb();
  banner();
}

// structure outlines: solid = the landmark's own structure, dashed = a structure its rule reads
function drawOutlines(ctx) {
  for (const o of S.outlines) {
    ctx.setLineDash(o.role === "rule" ? [7, 5] : []);
    for (const [w, col] of [[3.6, "rgba(0,0,0,.85)"], [1.7, o.colour]]) {
      ctx.lineWidth = w; ctx.strokeStyle = col;
      for (const poly of o.polys) {
        ctx.beginPath();
        poly.forEach((q, j) => { const [x, y] = toCanvas(q[0], q[1]); if (j) ctx.lineTo(x, y); else ctx.moveTo(x, y); });
        ctx.closePath(); ctx.stroke();
      }
    }
  }
  ctx.setLineDash([]);
}

function renderLegend() {
  const el = $("#maskLegend");
  el.innerHTML = "";
  el.hidden = !(S.showMasks && S.outlines && S.outlines.length);
  if (el.hidden) return;
  for (const o of S.outlines) {
    const s = document.createElement("span");
    s.className = `chip ${o.role}`;
    s.style.borderColor = o.colour;
    s.textContent = o.name.trim();
    s.title = o.role === "rule" ? "structure the landmark rule reads" : "the landmark's own structure";
    el.appendChild(s);
  }
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

// ---------------- ruler ----------------
// screen-only measuring aid: never saved, not part of undo. The length is approximate: the study
// images carry no pixel spacing (an assumed detector value is used) and a chest film magnifies
// the mediastinum (more on AP than PA), so the tool warns on every use.
function lineMm(a, b) {
  const [w, h] = S.card.orig_wh || [512, 512];
  return Math.hypot(((b[0] - a[0]) * w) / 512, ((b[1] - a[1]) * h) / 512) * S.card.mm_per_px;
}
const fmtCm = (mm) => `≈ ${(mm / 10).toFixed(1)} cm`;

function drawRuler(ctx) {
  const [ax, ay] = toCanvas(...S.line.a), [bx, by] = toCanvas(...S.line.b);
  const len = Math.hypot(bx - ax, by - ay);
  const nx = len ? (ay - by) / len * 5 : 0, ny = len ? (bx - ax) / len * 5 : 0;
  for (const [w, col] of [[3.5, "rgba(0,0,0,.85)"], [1.6, "#ffb703"]]) {
    ctx.lineWidth = w; ctx.strokeStyle = col;
    ctx.beginPath(); ctx.moveTo(ax, ay); ctx.lineTo(bx, by);
    for (const [x, y] of [[ax, ay], [bx, by]]) { ctx.moveTo(x - nx, y - ny); ctx.lineTo(x + nx, y + ny); }
    ctx.stroke();
  }
  const t = `${fmtCm(lineMm(S.line.a, S.line.b))} ⚠`;
  ctx.font = "bold 13px system-ui";
  const tw = ctx.measureText(t).width;
  const tx = Math.min(Math.max(4, bx + 10), ctx.canvas.width - tw - 10), ty = Math.min(Math.max(18, by - 8), ctx.canvas.height - 6);
  ctx.fillStyle = "rgba(0,0,0,.8)"; ctx.fillRect(tx - 4, ty - 14, tw + 8, 19);
  ctx.fillStyle = "#ffb703"; ctx.fillText(t, tx, ty);
}

function setRuler(on) {
  S.ruler = on;
  if (!on) S.line = null;
  $("#btnRuler").classList.toggle("on", on);
  $("#btnRuler").setAttribute("aria-pressed", String(on));
  $("#view").classList.toggle("ruler", on);
  const w = $("#rulerWarn");
  w.hidden = !on;
  if (on) {
    const c = S.card;
    w.textContent = "⚠ Approximate only, not for clinical decisions";
    w.title = (c.spacing_assumed ? `Pixel size assumed (${c.mm_per_px} mm), not read from the image. ` : "")
      + "Not corrected for magnification (AP films magnify the mediastinum more than PA).";
  }
  draw();
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
// ---------------- jump ----------------
async function buildJumpMenu() {
  const d = await api(`/api/r/${S.rid}/cards`);
  const sel = $("#jumpGroup");
  sel.length = 1;
  const seen = new Map();
  for (const c of d.cards) {
    const k = `${c.block}|${c.gid}|${c.view}`;
    if (!seen.has(k)) seen.set(k, { first: c.pos, n: 0, done: 0, gid: c.gid, view: c.view, block: c.block });
    const g = seen.get(k); g.n++; g.done += c.done;
  }
  for (const g of seen.values()) {
    const o = document.createElement("option");
    o.value = g.first;
    o.textContent = `${g.block ? `block ${g.block} · ` : ""}${g.gid} ${d.group_names[g.view][g.gid]} · ${g.view} (${g.done}/${g.n})`;
    sel.appendChild(o);
  }
  const scratch = d.cards.find((c) => c.mode === "scratch");
  if (scratch) {
    const o = document.createElement("option");
    o.value = scratch.pos; o.textContent = "first card without pre-fill (scratch)";
    sel.appendChild(o);
  }
}

function jumpTo(pos) {
  if (Number.isInteger(pos) && pos >= 0 && pos < S.total) load(pos);
  else $("#msg").textContent = Number.isInteger(pos) ? `No card ${pos + 1}.` : "Type a card number.";
}

// ---------------- definition of the selected point ----------------
function renderDef() {
  const p = S.pts[S.sel];
  const box = $("#defBox");
  box.hidden = !p;
  if (!p) return;
  $("#defLabel").textContent = p.label;
  const line = (el, k, v) => { el.innerHTML = ""; if (!v) return; const s = document.createElement("span"); s.className = "k"; s.textContent = k; el.append(s, v); };
  line($("#defMethod"), "Definition:", p.reader || p.method);
  line($("#defRule"), "Construction rule (technical):", p.rule && p.rule !== p.method ? p.rule : "");
  line($("#defNote"), "Reviewer note:", p.note);
}

function renderList() {
  const ol = $("#points");
  ol.innerHTML = "";
  S.pts.forEach((p, i) => {
    const li = document.createElement("li");
    if (i === S.sel) li.classList.add("sel");
    li.title = [p.reader || p.method, p.rule && "Construction rule: " + p.rule].filter(Boolean).join("\n");
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
        b.onclick = (e) => { e.stopPropagation(); snapshot(); p.choice = opt; if (opt !== "new") p.xy = null; S.sel = i; renderList(); draw(); };
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
  renderDef();
  const open = S.pts.filter((p) => !resolved(p)).length;
  $("#btnSubmit").disabled = open > 0;
  $("#btnSubmit").title = open ? `${open} landmark${open > 1 ? "s" : ""} still open` : "Submit and next (Enter)";
  saveDraft();
  $("#btnUndo").disabled = !S.hist.length;
  $("#btnRedo").disabled = !S.redo.length;
}

const resolved = (p) => (S.mode === "adjudicate" ? !!p.choice && (p.choice !== "new" || !!p.xy) : p.status !== "missing");
const complete = () => S.pts.every(resolved);

// ---------------- interaction ----------------
function hitTest(px, py) {
  if (S.hide) return -1;                           // hidden points cannot be grabbed
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

const stateNow = () => JSON.stringify({ pts: S.pts.map((p) => ({ xy: p.xy, status: p.status, unsure: p.unsure, n_edits: p.n_edits, choice: p.choice })),
                                        flag: $("#flag").checked });

function snapshot() {
  S.lastNudge = null;
  S.hist.push(stateNow());
  if (S.hist.length > 200) S.hist.shift();
  S.redo = [];
}

function restore(js) {
  const st = JSON.parse(js);
  st.pts.forEach((q, i) => Object.assign(S.pts[i], q));
  $("#flag").checked = st.flag;
  renderList(); draw();
}

function undo() {
  S.lastNudge = null;
  if (!S.hist.length || S.drag) return;
  S.redo.push(stateNow());
  restore(S.hist.pop());
}

function redo() {
  S.lastNudge = null;
  if (!S.redo.length || S.drag) return;
  S.hist.push(stateNow());
  restore(S.redo.pop());
}

// arrow keys: move the selected point by whole screen pixels (Shift: 5); consecutive nudges
// of the same point form one undo step
function nudge(dx, dy) {
  const p = S.pts[S.sel];
  if (!p || !p.xy || p.status === "missing" || p.status === "not_visible" || S.mode === "adjudicate") return;
  if (S.lastNudge !== S.sel) { snapshot(); S.lastNudge = S.sel; }
  p.xy = [clamp512(p.xy[0] + dx / S.view.sx), clamp512(p.xy[1] + dy / S.view.sy)];
  if (p.status === "ok") { p.status = "moved"; p.n_edits++; S.nEvents++; }
  renderList(); draw();
}

function toggleFlag() {
  $("#flag").checked = !$("#flag").checked;
  $("#flag").onchange();
}

// placing or moving a point on a card flagged "not assessable" makes it assessable again
function unflag() {
  if (S.mode === "adjudicate" || !$("#flag").checked) return;
  $("#flag").checked = false;
  S.preFlag = null;
}

function onDown(e) {
  touch();
  if (e.button !== 0 || S.loading || S.submitting || !S.card) return;
  const [px, py] = canvasPos(e);
  if (S.ruler) {
    const at = toImage(px, py).map(clamp512);
    S.line = { a: at, b: at.slice(), live: true };
    $("#view").setPointerCapture(e.pointerId);
    draw();
    return;
  }
  const hit = hitTest(px, py);
  if (hit >= 0) {
    const redo = S.redo.slice();     // a press that only selects keeps the redo steps
    snapshot();                     // dropped again in onUp if the point did not move
    S.sel = hit;
    const p = S.pts[hit];
    // keep the grab offset so an off-centre grab does not make the point jump to the cursor
    const [qx, qy] = toCanvas(...p.xy);
    S.drag = { i: hit, from: p.xy.slice(), status: p.status, moved: false, dx: px - qx, dy: py - qy, sx: px, sy: py,
               nEvents: S.nEvents, redo };
    $("#view").setPointerCapture(e.pointerId);
  } else {
    // a press on empty image: a pan if it moves, else a click (see onUp)
    S.pan = { sx: px, sy: py, box: [S.view.x0, S.view.y0, S.view.x1 - S.view.x0], moved: false };
    $("#view").setPointerCapture(e.pointerId);
    return;
  }
  renderList(); draw();
}

// a click on empty image: place the selected point when it is missing
function clickAt(px, py) {
  if (S.hide) return;
  const p = S.pts[S.sel];
  if (p && S.mode !== "adjudicate" && (p.status === "missing" || p.status === "not_visible")) {
    snapshot();
    p.xy = toImage(px, py).map(clamp512);
    p.status = p.prefill ? "moved" : "added";
    p.n_edits++; S.nEvents++;
    unflag();
    nextUnresolved();
  } else if (p && S.mode === "adjudicate" && p.choice === "new") {
    snapshot();
    p.xy = toImage(px, py).map(clamp512);
    S.nEvents++;
  }
  renderList(); draw();
}

function onMove(e) {
  if (S.line && S.line.live) { S.line.b = toImage(...canvasPos(e)).map(clamp512); draw(); return; }
  if (S.pan) {
    const [px, py] = canvasPos(e);
    if (!S.pan.moved && Math.hypot(px - S.pan.sx, py - S.pan.sy) < PAN_MIN) return;
    S.pan.moved = true;
    $("#view").classList.add("panning");
    const [x0, y0, side] = S.pan.box;
    setBox(x0 - (px - S.pan.sx) / S.view.sx, y0 - (py - S.pan.sy) / S.view.sy, side);
    return;
  }
  if (!S.drag) return;
  const p = S.pts[S.drag.i];
  const [px, py] = canvasPos(e);
  // a click to select a point is not a move
  if (!S.drag.moved && Math.hypot(px - S.drag.sx, py - S.drag.sy) < DRAG_MIN) return;
  p.xy = toImage(px - S.drag.dx, py - S.drag.dy).map(clamp512);
  if (!S.drag.moved && S.mode !== "adjudicate") { S.drag.moved = true; p.status = p.prefill ? "moved" : "added"; p.n_edits++; S.nEvents++; unflag(); }
  S.drag.moved = true;
  draw();
}

// click = false: the pointer was cancelled (touch or pen gesture, lost capture); end without acting
function onUp(e, click = true) {
  if (S.pan) {
    const { moved, sx, sy } = S.pan;
    S.pan = null;
    $("#view").classList.remove("panning");
    if (!moved && click) clickAt(sx, sy);
    return;
  }
  if (S.line && S.line.live) {
    S.line.live = false;
    if (S.line.a[0] === S.line.b[0] && S.line.a[1] === S.line.b[1]) S.line = null;      // a click clears the line
    draw();
    return;
  }
  if (S.drag) {
    if (!S.drag.moved) { S.hist.pop(); S.redo = S.drag.redo; }
    S.drag = null; renderList(); draw();
  }
}

const clamp512 = (v) => Math.min(511.99, Math.max(0, v));

function nextUnresolved() {
  const i = S.pts.findIndex((p) => (S.mode === "adjudicate" ? !p.choice : p.status === "missing"));
  if (i >= 0) S.sel = i;
}

function setNotVisible() {
  const p = S.pts[S.sel];
  if (!p || S.mode === "adjudicate") return;
  snapshot();
  p.status = "not_visible"; p.n_edits++; S.nEvents++;
  nextUnresolved(); renderList(); draw();
}
function toggleUnsure() { const p = S.pts[S.sel]; if (p && S.mode !== "adjudicate") { snapshot(); p.unsure = !p.unsure; renderList(); draw(); } }
function resetPoint() {
  const p = S.pts[S.sel];
  if (!p || S.mode === "adjudicate") return;
  snapshot();
  p.xy = p.prefill ? p.prefill.slice() : null;
  p.status = p.prefill ? "ok" : "missing";
  renderList(); draw();
}

async function toggleMasks() {
  if (S.mode === "adjudicate" || S.loading) return;
  S.showMasks = !S.showMasks;
  savePrefs();
  $("#btnMasks").textContent = `Outlines: ${S.showMasks ? "on" : "off"}`;
  if (S.showMasks && !S.outlines) {
    const card = S.card, o = await api(`/api/r/${S.rid}/card/${S.pos}/outlines`);
    if (card !== S.card || !S.showMasks) return;
    S.outlines = o;
  }
  renderLegend();
  draw();
}

function toggleFull() { if (S.card.full) return; S.full = !S.full; S.zoomBox = null; layout(); draw(); }

// ---------------- zoom and pan ----------------
// after Google Maps: the wheel (or a trackpad pinch) zooms at the cursor, dragging the image pans,
// +/- and fit sit bottom right on the image
function setBox(x0, y0, side) {
  side = Math.min(512, Math.max(ZOOM_MIN_SIDE, side));
  x0 = Math.min(Math.max(0, x0), 512 - side); y0 = Math.min(Math.max(0, y0), 512 - side);
  S.zoomBox = [x0, y0, x0 + side, y0 + side];
  const cv = $("#view");
  S.view = { x0, y0, x1: x0 + side, y1: y0 + side, sx: cv.width / side, sy: cv.height / side };
  draw();
}

// zoom by factor f (> 1 in) keeping the image point under (px, py) in place
function zoomAt(f, px, py) {
  if (!S.card) return;
  const v = S.view, side = v.x1 - v.x0, ns = Math.min(512, Math.max(ZOOM_MIN_SIDE, side / f));
  const [ix, iy] = toImage(px, py);
  setBox(ix - (px / $("#view").width) * ns, iy - (py / $("#view").height) * ns, ns);
}
const zoomCentre = (f) => zoomAt(f, $("#view").width / 2, $("#view").height / 2);
function zoomFit() { S.zoomBox = null; layout(); draw(); }

function onWheel(e) {
  e.preventDefault();
  touch();
  // a trackpad pinch arrives as a wheel event with ctrlKey and small deltas
  const k = e.ctrlKey ? 0.01 : 0.0015;
  const [px, py] = canvasPos(e);
  zoomAt(Math.exp(-e.deltaY * k), px, py);
  // a zoom during a pan: continue the pan from the new view
  if (S.pan) Object.assign(S.pan, { sx: px, sy: py, box: [S.view.x0, S.view.y0, S.view.x1 - S.view.x0] });
}

async function submit() {
  if (S.submitting || S.loading || S.drag || S.pan) return;
  if (!complete()) { $("#msg").textContent = "Resolve every landmark first (place it or mark it not visible)."; return; }
  S.submitting = true;
  $("#comment").readOnly = true; $("#flag").disabled = true;
  const card = S.card, key = S.mode === "adjudicate" ? null : draftKey();
  let saved = false;
  try {
    let r;
    if (S.mode === "adjudicate") {
      const keys = {};
      S.pts.forEach((p) => { keys[p.key] = { choice: p.choice, xy: p.choice === "new" ? p.xy : null }; });
      r = await post(`/api/adj/item/${S.pos}/resolve`, { item_id: S.card.item_id, keys });
    } else {
      r = await post(`/api/r/${S.rid}/card/${S.pos}/submit`, {
        card_id: S.card.card_id,
        points: S.pts.map((p) => ({ key: p.key, status: p.status, unsure: p.unsure, xy: p.xy, n_edits: p.n_edits })),
        flag: $("#flag").checked ? "not_assessable" : null, comment: $("#comment").value,
        active_ms: S.activeMs, n_events: S.nEvents,
      });
    }
    saved = true;
    if (key) { LS.del(key); if (S.card === card) S.base = null; buildJumpMenu(); }
    if (S.pos >= S.total - 1) {
      // the last position is not necessarily the last open card (jumping around)
      const s = await api(S.mode === "adjudicate" ? "/api/adj/session" : `/api/r/${S.rid}/session`);
      updateProgress();
      if (s.cursor < s.total) { await load(s.cursor, true); $("#msg").textContent = `Saved. Card ${s.cursor + 1} is still open.`; }
      else { S.base = stateNow(); S.baseComment = $("#comment").value; $("#msg").textContent = "All cards done. Thank you."; }
      return;
    }
    await load(r.next, true);
  } catch (err) { $("#msg").textContent = saved ? `Saved, but the next card did not load: ${err.message}` : `Not saved: ${err.message}`; }
  finally { S.submitting = false; $("#comment").readOnly = false; $("#flag").disabled = false; }
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

async function exportNow() {
  try {
    const r = await post(`/api/r/${S.rid}/export`, {});
    $("#msg").style.color = "#2a9d8f";
    $("#msg").textContent = `Exported ${r.done}/${r.cards} cards (${r.answered} landmarks) to DATA/${r.dir}`;
    setTimeout(() => { $("#msg").textContent = ""; $("#msg").style.color = ""; }, 8000);
  } catch (err) { $("#msg").textContent = `Export failed: ${err.message}`; }
}

// brightness and contrast: the sliders clamp the value to their own range
function adjust(sel, d) { const el = $(sel); el.value = +el.value + d; savePrefs(); draw(); }
function resetImage() { $("#bright").value = 100; $("#contrast").value = 100; savePrefs(); draw(); }

// ---------------- wiring ----------------
function wire() {
  const cv = $("#view");
  cv.addEventListener("pointerdown", onDown);
  cv.addEventListener("pointermove", onMove);
  cv.addEventListener("pointerup", onUp);
  cv.addEventListener("wheel", onWheel, { passive: false });
  $("#zoomIn").onclick = () => zoomCentre(1.5);
  $("#zoomOut").onclick = () => zoomCentre(1 / 1.5);
  $("#zoomFit").onclick = zoomFit;
  $("#btnSubmit").onclick = submit;
  $("#btnPrev").onclick = () => S.pos > 0 && load(S.pos - 1);
  $("#btnCrop").onclick = toggleFull;
  $("#btnMasks").onclick = toggleMasks;
  $("#btnRuler").onclick = () => setRuler(!S.ruler);
  $("#btnHide").onpointerdown = () => { S.hide = true; draw(); };
  $("#btnHide").onpointerup = () => { S.hide = false; draw(); };
  $("#bright").oninput = () => { savePrefs(); draw(); };
  $("#contrast").oninput = () => { savePrefs(); draw(); };
  $("#comment").addEventListener("input", saveDraft);
  // time, zoom and view are written when the page goes away (reload, close, tab switch)
  window.addEventListener("pagehide", saveDraft);
  document.addEventListener("visibilitychange", () => { if (document.hidden) saveDraft(); });
  $("#who").onclick = () => { LS.del("last"); location.reload(); };
  $("#btnResetView").onclick = resetImage;
  window.addEventListener("resize", () => {
    if (!S.card) return;
    const w = S.window; layout(); S.window = w; draw();     // keep the window level of this card
  });
  cv.addEventListener("pointercancel", (e) => onUp(e, false));
  cv.addEventListener("lostpointercapture", (e) => onUp(e, false));
  // hidden points come back however the hold ends (released elsewhere, window left)
  const unhide = () => { if (S.hide) { S.hide = false; draw(); } };
  window.addEventListener("pointerup", unhide);
  window.addEventListener("blur", unhide);
  // a clicked control gives the focus back, so shortcuts (Enter, arrows, Space) keep working
  document.addEventListener("change", (e) => { if (e.target.matches("input[type=range], input[type=checkbox]")) e.target.blur(); });
  document.addEventListener("click", (e) => { const b = e.target.closest("button"); if (b && e.detail > 0) b.blur(); });
  $("#refMain").onclick = () => setRef("main");
  $("#refSeries").onclick = () => setRef("series");
  $("#btnUndo").onclick = undo;
  $("#btnExport").onclick = exportNow;
  $("#jump").addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); jumpTo(parseInt($("#jump").value, 10) - 1); $("#jump").blur(); }
    if (e.key === "Escape") $("#jump").blur();
  });
  // the box shows the current card again when left without jumping (as in Chrome's PDF viewer)
  $("#jump").addEventListener("focus", () => $("#jump").select());
  $("#jump").addEventListener("blur", () => { if (S.card) $("#jump").value = S.pos + 1; });
  $("#jumpGroup").onchange = () => {
    const v = $("#jumpGroup").value;
    if (v !== "") jumpTo(parseInt(v, 10));
    $("#jumpGroup").value = ""; $("#jumpGroup").blur();
  };
  $("#btnRedo").onclick = redo;
  $("#flag").onchange = () => {
    const now = $("#flag").checked;
    $("#flag").checked = !now; snapshot(); $("#flag").checked = now;
    if (now) {
      S.preFlag = S.pts.map((p) => p.status);
      S.pts.forEach((p) => { p.status = "not_visible"; });
    } else if (S.preFlag) {
      S.pts.forEach((p, i) => { if (p.status === "not_visible") p.status = S.preFlag[i]; });
      S.preFlag = null;
    }
    renderList(); draw();
  };
  $("#helpBtn").onclick = () => { $("#help").hidden = false; };
  $("#helpClose").onclick = () => { $("#help").hidden = true; };
  document.addEventListener("keydown", (e) => {
    touch();                                       // typing a comment is active time too
    const t = e.target.tagName;
    if (t === "INPUT" || t === "TEXTAREA" || t === "SELECT" || !S.card) return;
    const k = e.key.length === 1 ? e.key.toLowerCase() : e.key;    // Caps Lock does not matter
    if (k === "Escape") {
      if (!$("#help").hidden) { $("#help").hidden = true; return; }
      if (S.line) { S.line = null; draw(); return; }
      if (S.drag) {                                // back to before the drag, edit counts included
        const { nEvents, redo } = S.drag;
        S.drag = null; S.redo = redo; restore(S.hist.pop()); S.nEvents = nEvents;
      }
      return;
    }
    if (k === "?") { $("#help").hidden = !$("#help").hidden; return; }
    if (!$("#help").hidden || S.loading || S.submitting) return;   // no edits under the help overlay, while loading or saving
    if (k === "Enter" && e.repeat) { e.preventDefault(); return; }          // holding Enter submits once
    if ((e.ctrlKey || e.metaKey) && (k.toLowerCase() === "y" || (k.toLowerCase() === "z" && e.shiftKey))) { e.preventDefault(); redo(); return; }
    if ((e.ctrlKey || e.metaKey) && k.toLowerCase() === "z") { e.preventDefault(); undo(); return; }
    if ((e.ctrlKey || e.metaKey) && k.toLowerCase() === "s") { e.preventDefault(); if (S.mode !== "adjudicate") exportNow(); return; }
    if (e.altKey && k.startsWith("Arrow")) {             // pan by a fifth of the view
      e.preventDefault();
      const v = S.view, step = (v.x1 - v.x0) / 5;
      setBox(v.x0 + (k === "ArrowLeft" ? -step : k === "ArrowRight" ? step : 0),
             v.y0 + (k === "ArrowUp" ? -step : k === "ArrowDown" ? step : 0), v.x1 - v.x0);
      return;
    }
    // [ ] { } need Option (Mac) or AltGr (= Ctrl+Alt, Windows) on many keyboards, e.g. German
    if ("[]{}".includes(k) && !e.metaKey && (!e.ctrlKey || e.altKey)) {
      if (k === "[" || k === "]") adjust("#bright", k === "]" ? 10 : -10);
      else adjust("#contrast", k === "}" ? 10 : -10);
      return;
    }
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (k.startsWith("Arrow")) {
      e.preventDefault();
      const st = e.shiftKey ? 5 : 1;
      nudge(k === "ArrowLeft" ? -st : k === "ArrowRight" ? st : 0, k === "ArrowUp" ? -st : k === "ArrowDown" ? st : 0);
      return;
    }
    if (k === "Enter") { e.preventDefault(); submit(); }
    else if (k === "Backspace") { e.preventDefault(); if (S.pos > 0) load(S.pos - 1); }
    else if (k === "Tab") { e.preventDefault(); S.lastNudge = null; S.sel = (S.sel + (e.shiftKey ? S.pts.length - 1 : 1)) % S.pts.length; renderList(); draw(); }
    else if (k === " ") { e.preventDefault(); S.lastNudge = null; nextUnresolved(); renderList(); draw(); }
    else if (k === "f") toggleFlag();
    else if (k === "n") setNotVisible();
    else if (k === "u") toggleUnsure();
    else if (k === "r") resetPoint();
    else if (k === "m") toggleMasks();
    else if (k === "c") toggleFull();
    else if (k === "l") setRuler(!S.ruler);
    else if (k === "+" || k === "=") zoomCentre(1.5);
    else if (k === "-") zoomCentre(1 / 1.5);
    else if (k === "z") zoomFit();
    else if (k === "1") setRef("main");
    else if (k === "2" && S.card.refs.series) setRef("series");
    else if (k === "h" && !S.hide) { S.hide = true; draw(); }
    else if (k === "g") { e.preventDefault(); $("#jump").focus(); }
    else if (k === "0") resetImage();
  });
  document.addEventListener("keyup", (e) => { if (e.key.toLowerCase() === "h" && S.hide) { S.hide = false; draw(); } });
}

wire();
macLabels();
start();
