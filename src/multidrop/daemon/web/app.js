// Beam web UI (a fork of MultiDrop's). Talks only to the local daemon; peer-supplied
// strings (device and file names, texts) are always inserted as text, never as HTML.
// The daemon's CSP forbids inline style attributes: geometry goes through CSSOM
// custom properties (el.style.setProperty), never setAttribute("style").
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => [...document.querySelectorAll(sel)];
const SVG = "http://www.w3.org/2000/svg";
const PROTO = { quickshare: "Quick Share", localsend: "LocalSend", airdrop: "AirDrop" };
let token = "";
let groups = [];
let lastSeq = 0;
const jobs = new Map();          // job id -> {info, files: Map(name -> {done,size}), state}
const resend = new Map();        // job id -> {target, files|paths|text, label}, for a retry with PIN
const manual = [];               // targets added by address
const unchecked = new Map();     // offer id -> Set of file ids the user unticked
let selection = [];              // local paths handed over (Explorer "Send to")
const doneFlash = new Map();     // device name -> time the last transfer finished

function el(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") e.className = v;
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const c of children) e.append(c instanceof Node ? c : String(c ?? ""));
  return e;
}

function icon(name) {
  const s = document.createElementNS(SVG, "svg");
  const u = document.createElementNS(SVG, "use");
  u.setAttribute("href", `#i-${name}`);
  s.append(u);
  s.setAttribute("aria-hidden", "true");
  return s;
}

function human(n) {
  if (n === null || n === undefined) return "size unknown";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  while (n >= 1000 && i < units.length - 1) { n /= 1000; i++; }
  return i ? `${n.toFixed(1)} ${units[i]}` : `${n} B`;
}

const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;

async function api(path, opts = {}) {
  opts.headers = { ...(opts.headers || {}), "X-Multidrop-Token": token };
  const r = await fetch(path, opts);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.error || `HTTP ${r.status}`);
  return body;
}

// -- navigation -------------------------------------------------------------

let wantedPane = null;            // remembered pane, until the tab for it exists

function show(pane, remember = true) {
  const item = $(`.nav-item[data-pane="${pane}"]`);
  if (!item || item.hidden) { pane = "nearby"; remember = false; }
  for (const b of $$(".nav-item")) {
    if (b.dataset.pane === pane) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
  }
  for (const p of $$(".pane")) p.hidden = p.dataset.pane !== pane ||
    (p.dataset.pane !== "nearby" && $(`.nav-item[data-pane="${p.dataset.pane}"]`).hidden);
  if (remember) {
    wantedPane = pane;
    try { sessionStorage.setItem("beam-pane", pane); } catch { /* storage may be off */ }
  }
  if (pane === "nearby") layoutPeers();
}

function enablePane(pane) {
  $(`.nav-item[data-pane="${pane}"]`).hidden = false;
}

// -- status, offers -------------------------------------------------------

async function loadStatus() {
  const s = await api("/api/status");
  const ports = Object.entries(s.protocols)
    .map(([p, v]) => `${PROTO[p] || p} ${v.port}`).join(", ");
  $("#status").textContent = `Visible as “${s.alias}”`;
  $("#status-detail").textContent = s.accept === "all" ? "Accepts everything automatically" : "Ready to receive";
  $("#status").title = `${ports}. Saves to ${s.download_dir}`;
  $("#status-dot").className = "dot on";
  document.querySelectorAll(".alias").forEach((e) => { e.textContent = `“${s.alias}”`; });
}

const PARTIAL_NOTE = {
  discard: "Quick Share can only accept everything: unselected files are transferred but not saved.",
  none: "This sender only allows accepting everything or nothing.",
};

async function loadOffers() {
  const offers = await api("/api/offers");
  const box = $("#offers");
  box.replaceChildren();
  $("#offers-section").hidden = offers.length === 0;
  $("#offers-title").textContent = offers.length > 1 ? `${offers.length} incoming requests` : "Incoming";
  for (const id of unchecked.keys()) if (!offers.some((o) => o.id === id)) unchecked.delete(id);
  for (const o of offers) {
    const selectable = o.partial !== "none" && o.files.length > 1;
    const off = unchecked.get(o.id) || new Set();
    unchecked.set(o.id, off);
    const items = el("ul", { class: selectable ? "files selectable" : "files" });
    for (const f of o.files) {
      const label = f.text !== null ? `Text: ${f.text}` : `${f.name} (${human(f.size)})`;
      if (!selectable) { items.append(el("li", {}, label)); continue; }
      const cb = el("input", { type: "checkbox", "data-file": f.id, onchange: (ev) => {
        if (ev.target.checked) off.delete(f.id); else off.add(f.id);
      } });
      cb.checked = !off.has(f.id);
      items.append(el("li", {}, el("label", {}, cb, label)));
    }
    const answer = (accept, files) => api(`/api/offers/${o.id}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify(files ? { accept, files } : { accept }),
    }).catch((e) => addLog(`Couldn’t answer ${o.from}: ${e.message}`)).finally(loadOffers);
    const chosen = () => o.files.map((f) => f.id).filter((id) => !off.has(id));
    const note = o.files.length > 1 ? PARTIAL_NOTE[o.partial] : null;
    const what = o.files.length === 1 && o.files[0].text !== null ? "a message"
      : plural(o.files.length, "item", "items");
    box.append(el("div", { class: "offer" },
      el("div", { class: "offer-head" },
        el("div", { class: "avatar" }, icon("phone")),
        el("div", {},
          el("div", { class: "offer-from" }, o.from),
          el("div", { class: "caption" }, `wants to send ${what}, ` +
            (o.size_unknown ? "size unknown" : human(o.total_size)) +
            ` via ${PROTO[o.protocol] || o.protocol}`))),
      items,
      note ? el("p", { class: "caption hint" }, note) : "",
      o.pin ? el("div", { class: "code-line" }, el("span", { class: "caption" }, "Check that the sender shows "),
                 el("span", { class: "pin" }, o.pin)) : "",
      el("div", { class: "actions" },
        el("button", { class: "btn", type: "button", onclick: () => answer(false) }, "Decline"),
        selectable ? el("button", { class: "btn", type: "button", "data-action": "accept-selected", onclick: () => {
          const ids = chosen();
          if (ids.length) answer(true, ids); else answer(false);
        } }, "Accept selected") : "",
        el("button", { class: "btn primary", type: "button", onclick: () => answer(true) },
           selectable ? "Accept all" : "Accept"))));
  }
  const first = box.querySelector(".btn.primary");
  if (first && !box.contains(document.activeElement)) first.focus();
}

// -- devices and sending --------------------------------------------------

async function loadDevices(refresh) {
  const btn = $("#refresh");
  btn.disabled = true;
  btn.textContent = refresh ? "Searching…" : "Search";
  $("#radar").classList.add("searching");
  try {
    groups = await api("/api/devices" + (refresh ? "?refresh=1&timeout=12" : ""));
  } catch (e) {
    addLog(`Device search failed: ${e.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = "Search";
    $("#radar").classList.remove("searching");
  }
  renderDevices();
}

function allGroups() {
  return groups.concat(manual);
}

const KIND = { phone: "phone", mobile: "phone", tablet: "tablet", laptop: "laptop",
               desktop: "pc", apple: "phone", web: "pc", headless: "pc", server: "pc" };

function renderDevices() {
  const box = $("#devices");
  box.replaceChildren();
  const select = $("#text-target");
  select.replaceChildren();
  for (const g of allGroups()) {
    let chosen = g.targets[0];
    const protoSel = el("select", { "aria-label": `How to send to ${g.name}`,
                                    onclick: (ev) => ev.stopPropagation(),
                                    onchange: (ev) => { chosen = g.targets[ev.target.value]; } });
    g.targets.forEach((t, i) => protoSel.append(el("option", { value: i }, PROTO[t.protocol] || t.protocol)));
    const peer = el("div", { class: "peer drop", tabindex: "0", role: "button", "data-name": g.name,
                             title: `${g.name}, ${chosen.address}` },
      el("div", { class: "avatar" }, icon(KIND[g.type] || "laptop"),
         el("span", { class: "badge-done" }, icon("check"))),
      el("div", { class: "peer-name" }, g.name),
      g.targets.length > 1 ? protoSel
        : el("div", { class: "peer-meta" }, PROTO[chosen.protocol] || chosen.protocol));
    makeDropTarget(peer, (files) => upload(targetOf(chosen), files, g.name),
                   () => sendPaths(targetOf(chosen), selection, g.name));
    box.append(peer);
    g.targets.forEach((t) => select.append(el("option", { value: JSON.stringify(targetOf(t)) },
      `${g.name} (${PROTO[t.protocol] || t.protocol})`)));
  }
  $("#devices-empty").hidden = allGroups().length > 0;
  if (!select.options.length) select.append(el("option", { value: "" }, "No devices yet"));
  layoutPeers();
  paintPeers();
}

// Places devices on the radar's rings: up to 5 on the inner ring, then the outer.
function layoutPeers() {
  const radar = $("#radar");
  const w = radar.clientWidth, h = radar.clientHeight;
  if (!w || !h) return;
  // Devices sit on the first ring; the outer rings are only there for depth.
  const r = Math.max(96, Math.min(h / 2 - 76, w / 2 - 64, 150));
  radar.style.setProperty("--r1", `${r}px`);
  radar.style.setProperty("--r2", `${r * 1.55}px`);
  radar.style.setProperty("--r3", `${r * 2.2}px`);
  const peers = [...$("#devices").children];
  const inner = peers.slice(0, 6), outer = peers.slice(6);
  // The outer set uses an ellipse: the radar is wider than it is tall.
  const place = (list, rx, ry, offset) => list.forEach((p, i) => {
    const a = offset + (i / list.length) * 2 * Math.PI;
    p.style.setProperty("--x", `${Math.round(Math.cos(a) * rx)}px`);
    p.style.setProperty("--y", `${Math.round(Math.sin(a) * ry)}px`);
  });
  place(inner, r, r, inner.length === 1 ? -Math.PI / 4 : -Math.PI / 2 + Math.PI / inner.length / 2);
  place(outer, Math.min(w / 2 - 64, r * 2.1), Math.min(h / 2 - 70, r * 1.2), -Math.PI / 2);
}

// Transfer state on the avatars: a ring that fills, then a check.
function paintPeers() {
  const state = new Map();
  for (const j of jobs.values()) {
    let done = 0, size = 0;
    for (const f of j.files.values()) { done += f.done; size += f.size; }
    if (["running", "waiting"].includes(j.state)) {
      state.set(j.info.to, { state: j.state === "waiting" || !size ? "waiting" : "sending",
                             p: size ? done / size : 0 });
    }
  }
  const now = Date.now();
  for (const p of $$("#devices .peer")) {
    const s = state.get(p.dataset.name);
    p.classList.toggle("sending", s?.state === "sending");
    p.classList.toggle("waiting", s?.state === "waiting");
    p.classList.toggle("done", !s && now - (doneFlash.get(p.dataset.name) || 0) < 5000);
    p.style.setProperty("--p", s ? s.p.toFixed(3) : "0");
  }
  const active = [...jobs.values()].filter((j) => ["running", "waiting", "qr"].includes(j.state)).length;
  $("#activity-count").hidden = !active;
  $("#activity-count").textContent = active;
}

function targetOf(t) {
  return t.key ? { key: t.key } : { protocol: t.protocol, address: t.address, port: t.port };
}

// onSelection: what a click does while handed-over files are waiting.
function makeDropTarget(card, onFiles, onSelection) {
  card.addEventListener("dragover", (ev) => { ev.preventDefault(); card.classList.add("over"); });
  card.addEventListener("dragleave", () => card.classList.remove("over"));
  card.addEventListener("drop", async (ev) => {
    ev.preventDefault();
    card.classList.remove("over");
    const files = await filesFromDrop(ev.dataTransfer);
    if (files.length) onFiles(files);
  });
  const activate = (ev) => {
    if (ev.target.closest("[data-stop], label, select")) return;   // controls inside the card
    if (selection.length && onSelection) { onSelection(); return; }
    const input = $("#pick-files");
    input.value = "";
    input.onchange = () => {
      const files = [...input.files].map((f) => ({ file: f, path: f.name }));
      if (files.length) onFiles(files);
    };
    input.click();
  };
  card.addEventListener("click", activate);
  card.addEventListener("keydown", (ev) => {
    if ((ev.key === "Enter" || ev.key === " ") && ev.target === card) { ev.preventDefault(); activate(ev); }
  });
}

// Folders arrive as directory entries; walk them to keep relative paths.
async function filesFromDrop(dt) {
  const entries = [...dt.items].map((i) => i.webkitGetAsEntry && i.webkitGetAsEntry()).filter(Boolean);
  if (!entries.length) return [...dt.files].map((f) => ({ file: f, path: f.name }));
  const out = [];
  async function walk(entry, prefix) {
    if (entry.isFile) {
      const file = await new Promise((res, rej) => entry.file(res, rej));
      out.push({ file, path: prefix + file.name });
    } else if (entry.isDirectory) {
      const reader = entry.createReader();
      for (;;) {
        const batch = await new Promise((res, rej) => reader.readEntries(res, rej));
        if (!batch.length) break;
        for (const e of batch) await walk(e, `${prefix}${entry.name}/`);
      }
    }
  }
  for (const e of entries) await walk(e, "");
  return out;
}

async function upload(target, files, label, pin) {
  const form = new FormData();
  form.append("target", JSON.stringify(target));
  if (pin) form.append("pin", pin);
  for (const f of files) {
    form.append("path", f.path);
    form.append("file", f.file, f.file.name);
  }
  const total = files.reduce((s, f) => s + f.file.size, 0);
  addLog(`Sending ${plural(files.length, "file", "files")} (${human(total)}) to ${label}`);
  try {
    const { job } = await api("/api/send", { method: "POST", body: form });
    remember(job, { target, files, label });
  } catch (e) {
    addLog(`Couldn’t send to ${label}: ${e.message}`);
  }
}

async function sendPaths(target, paths, label, pin) {
  addLog(`Sending ${plural(paths.length, "item", "items")} to ${label}`);
  try {
    const { job } = await api("/api/send", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target, paths, pin: pin || null }) });
    remember(job, { target, paths, label });
    if (paths === selection) await clearSelection();
  } catch (e) {
    addLog(`Couldn’t send to ${label}: ${e.message}`);
  }
}

async function sendText(target, text, label, pin) {
  const { job } = await api("/api/send", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ target, text, pin: pin || null }) });
  remember(job, { target, text, label });
}

// Kept so a LocalSend receiver that asks for a PIN can be retried without
// picking the files again.
function remember(job, what) {
  if (isLocalSend(what.target)) resend.set(job, what);
  while (resend.size > 10) resend.delete(resend.keys().next().value);
  renderJobs();
}

function isLocalSend(t) {
  if (t.protocol) return t.protocol === "localsend";
  return (t.key || "").startsWith("localsend:");
}

function retryWithPin(id, pin) {
  const w = resend.get(id);
  if (!w || !pin) return;
  resend.delete(id);
  if (w.files) upload(w.target, w.files, w.label, pin);
  else if (w.paths) sendPaths(w.target, w.paths, w.label, pin);
  else sendText(w.target, w.text, w.label, pin).catch((e) => addLog(`Couldn’t send: ${e.message}`));
}

// -- browser download link ---------------------------------------------------

function shareOptions() {
  return { minutes: +$("#share-minutes").value, no_pin: !$("#share-pin-on").checked,
           https: $("#share-https").checked };
}

async function startShare(files) {
  const o = shareOptions();
  const form = new FormData();
  form.append("minutes", String(o.minutes));
  if (o.no_pin) form.append("no_pin", "1");
  if (o.https) form.append("https", "1");
  for (const f of files) {
    form.append("path", f.path);
    form.append("file", f.file, f.file.name);
  }
  try {
    showShare(await api("/api/share", { method: "POST", body: form }));
  } catch (e) {
    addLog(`Couldn’t create the download link: ${e.message}`);
  }
}

async function shareSelection() {
  try {
    showShare(await api("/api/share", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ paths: selection, ...shareOptions() }) }));
    await clearSelection();
    show("link");
  } catch (e) {
    addLog(`Couldn’t create the download link: ${e.message}`);
  }
}

// -- files handed over by the desktop (Explorer "Send to") ------------------

async function loadSelection() {
  selection = (await api("/api/selection")).paths;
  $("#selection").replaceChildren(...selection.map((p) =>
    el("li", { title: p }, p.split(/[\\/]/).pop())));
  $("#selection-title").textContent = `${plural(selection.length, "item", "items")} ready to send`;
  $("#selection-section").hidden = !selection.length;
  if (selection.length) show("nearby");
}

async function clearSelection() {
  await api("/api/selection", { method: "DELETE" });
  await loadSelection();
}

function showShare(s) {
  const box = $("#share");
  box.hidden = !s || !s.running;
  if (box.hidden) return;
  const doc = new DOMParser().parseFromString(s.svg, "image/svg+xml");   // from our daemon
  $("#share-qr").replaceChildren(document.importNode(doc.documentElement, true));
  $("#share-url").textContent = s.url;
  $("#share-url").href = s.url;
  $("#share-pin-row").hidden = !s.pin;
  $("#share-pin").textContent = s.pin || "";
  $("#share-tls-hint").hidden = !s.url.startsWith("https:");
  // iPhone hotspots always use 172.20.10.0/28; the hotspot iPhone cannot reach its clients.
  $("#share-hotspot").hidden = !/^https?:\/\/172\.20\.10\./.test(s.url);
  const until = new Date(s.expires * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  $("#share-info").textContent =
    `${plural(s.files.length, "file", "files")}, ${human(s.size)}. Open until ${until}. ` +
    `${plural(s.downloads, "download", "downloads")} so far.`;
}

// -- jobs -----------------------------------------------------------------

const JOB_STATE = { running: "Sending", waiting: "Waiting for them to accept", done: "Sent",
                    failed: "Failed", cancelled: "Cancelled", qr: "Waiting for the QR code to be scanned" };

function renderJobs() {
  const box = $("#jobs");
  box.replaceChildren();
  const list = [...jobs.entries()].reverse().slice(0, 10);
  for (const [id, j] of list) {
    let done = 0, size = 0;
    for (const f of j.files.values()) { done += f.done; size += f.size; }
    const label = JOB_STATE[j.state] || j.state;
    const pct = size && j.state === "running" ? `, ${Math.round((done / size) * 100)}%` : "";
    const row = el("div", { class: `job ${j.state}` },
      el("span", { class: "what" }, `${j.info.items.join(", ")} to ${j.info.to}`),
      el("span", { class: "state" }, j.error ? `${label}: ${j.error}` : label + pct));
    const actions = el("div", { class: "actions" });
    if (["running", "waiting", "qr"].includes(j.state)) {
      actions.append(el("button", { type: "button", class: "btn", onclick: () =>
        api(`/api/jobs/${id}/cancel`, { method: "POST" }).catch(() => {}) }, "Cancel"));
    }
    row.append(actions);
    if (size && j.state === "running") row.append(el("progress", { max: size, value: done }));
    if (j.state === "failed" && j.pinRequired && resend.has(id)) {
      const input = el("input", { class: "pin-input", inputmode: "numeric", autocomplete: "off",
                                  placeholder: "PIN", "aria-label": "Receiver PIN" });
      row.append(el("form", { class: "inline pin-form", onsubmit: (ev) => {
        ev.preventDefault();
        retryWithPin(id, input.value.trim());
      } }, input, el("button", { type: "submit", class: "btn primary" }, "Send again with PIN")));
    }
    box.append(row);
  }
  paintPeers();
}

function onJobEvent(ev) {
  if (ev.event === "job_started") {
    jobs.set(ev.job, { info: ev.info, files: new Map(), state: "running" });
  }
  const j = jobs.get(ev.job);
  if (!j) return;
  switch (ev.event) {
    case "progress": j.files.set(ev.file, { done: ev.done, size: ev.size }); break;
    case "waiting": j.state = "waiting"; break;
    case "pin": j.pin = ev.pin; addLog(`Code ${ev.pin} for ${ev.to}: it must match the receiver`); break;
    case "qr": j.state = "qr"; showQr(ev.svg); break;
    case "qr_scanned": j.state = "running"; j.info.to = ev.device; showQr(null); break;
    case "job_done":
      j.state = "done"; j.info = ev.info; showQr(null);
      doneFlash.set(j.info.to, Date.now()); setTimeout(paintPeers, 5100);
      break;
    case "job_failed":
      j.state = "failed"; j.error = ev.error; j.pinRequired = !!ev.pin_required; showQr(null); break;
    case "job_cancelled": j.state = "cancelled"; showQr(null); break;
  }
  if (j.state === "waiting" && ev.event === "progress") j.state = "running";
  renderJobs();
}

function showQr(svg) {
  const box = $("#qr");
  box.replaceChildren();
  if (!svg) return;
  // The SVG is generated by our own daemon (segno), not by a peer.
  const doc = new DOMParser().parseFromString(svg, "image/svg+xml");
  box.append(document.importNode(doc.documentElement, true));
}

// -- event stream and log -------------------------------------------------

const LOG_TEXT = {
  offer: (e) => `${e.offer.from} wants to send ${plural(e.offer.files.length, "item", "items")} (${PROTO[e.offer.protocol]})`,
  offer_accepted: (e) => `Accepted${e.auto ? " automatically" : ""}` +
    (e.partial ? ` (${e.offer.files.length} of ${e.offer.offered})` : ""),
  offer_rejected: () => "Declined",
  offer_expired: () => "The request expired without an answer",
  offer_withdrawn: () => "The sender cancelled the request",
  file_received: (e) => `Received ${e.file}, saved to ${e.path}`,
  message: (e) => `${e.kind === "url" ? "Link" : "Text"} from ${e.from}: ${e.text}`,
  session_done: (e) => `Finished receiving from ${e.from}`,
  failed: (e) => `Problem with ${e.from || "a device"}: ${e.error}`,
  file_failed: (e) => `Couldn’t receive ${e.file}: ${e.error}`,
  cancelled: (e) => `${e.from} cancelled`,
  rejected: (e) => `Declined: ${e.from || e.to || ""} ${e.reason || ""}`,
  job_done: (e) => `Sent ${e.info.items.join(", ")} to ${e.info.to}`,
  job_failed: (e) => `Couldn’t send to ${e.info.to}: ${e.error}` +
    (e.pin_required && !e.restored ? ". Enter the PIN in Activity" : ""),
  qr_scanned: (e) => `${e.device} scanned the QR code`,
  share_started: (e) => `Download link created for ${plural(e.files.length, "file", "files")}`,
  share_visit: (e) => `${e.from} opened the download link`,
  share_download: (e) => `${e.from} is downloading ${e.file}`,
  share_wrong_pin: (e) => `Wrong PIN from ${e.from}`,
  share_stopped: (e) => `Download link closed (${e.reason}, ${plural(e.downloads, "download", "downloads")})`,
  share_moved: (e) => `Download link moved to the new network: ${e.url}`,
};

function addLog(text, time, old) {
  const t = new Date((time || Date.now() / 1000) * 1000);
  const log = $("#log");
  const when = t.toDateString() === new Date().toDateString()
    ? t.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" })
    : t.toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  log.prepend(el("li", old ? { class: "old", title: "Before Beam last restarted" } : {},
                 el("time", {}, when), el("span", {}, text)));
  while (log.children.length > 200) log.lastChild.remove();
}

function connect() {
  const es = new EventSource(`/api/events?t=${encodeURIComponent(token)}&since=${lastSeq}`);
  es.onmessage = (msg) => {
    const ev = JSON.parse(msg.data);
    lastSeq = Math.max(lastSeq, ev.seq);
    if (ev.job) onJobEvent(ev);
    else if (ev.event === "job_started") onJobEvent(ev);
    if (ev.event.startsWith("offer")) loadOffers().catch(() => {});
    if (ev.event === "selection") loadSelection().catch(() => {});
    if (["share_download", "share_stopped", "share_moved"].includes(ev.event)) {
      api("/api/share").then(showShare).catch(() => {});
    }
    const text = LOG_TEXT[ev.event];
    if (text) addLog(text(ev), ev.time, ev.restored);
  };
  es.onerror = () => {
    $("#status").textContent = "Reconnecting…";
    $("#status-detail").textContent = "Lost the connection to Beam’s service";
    $("#status-dot").className = "dot off";
    es.close();
    setTimeout(() => { connect(); loadStatus().catch(() => {}); loadOffers().catch(() => {}); }, 3000);
  };
}

// -- app window on Windows: window.pywebview.api (win_window.DesktopApi) -------

function showNetwork(n) {
  $("#network").hidden = n.ok;
}

async function initNetwork(d) {
  const refresh = () => d.network().then(showNetwork).catch(() => {});
  $("#network-allow").onclick = async () => {
    $("#network-status").textContent = "Waiting for Windows…";
    try {
      const n = await d.allow_network();
      showNetwork(n);
      $("#network-status").textContent = n.ok ? "" : "Windows didn’t allow it. Try again and choose Yes.";
    } catch (e) {
      $("#network-status").textContent = String(e.message || e);
    }
  };
  await refresh();
  // Another Wi-Fi, or the first-start prompt answered (each check runs PowerShell).
  setInterval(() => { if (!document.hidden) refresh(); }, 30000);
}

const desktop = () => window.pywebview && window.pywebview.api;

async function initDesktop() {
  const d = desktop();
  const s = await d.settings();
  $("#welcome").hidden = !s.welcome;
  $("#welcome-ok").onclick = () => { $("#welcome").hidden = true; d.welcome_done(); };
  $("#set-autostart").checked = s.autostart;
  $("#set-sendto").checked = s.sendto;
  for (const [id, fn] of [["#set-autostart", "set_autostart"], ["#set-sendto", "set_sendto"]]) {
    $(id).onchange = (ev) => d[fn](ev.target.checked).catch((e) => {
      ev.target.checked = !ev.target.checked;
      addLog(`Couldn’t change the setting: ${e.message || e}`);
    });
  }
  $("#open-downloads").onclick = () => d.open_downloads();
  initNetwork(d);
  enablePane("settings");
  if (s.mirror) initMirror(d);
  if (s.audio) initAudio(d);
  restorePane();
}

async function initAudio(d) {
  enablePane("sound");
  const run = (p) => p.then(showSpeakers).catch((e) => {
    $("#speaker-status").textContent = String(e.message || e);
  });
  $("#speaker-play").onclick = () => {
    $("#speaker-status").textContent = "Connecting…";
    run(d.play_on($("#speaker").value));
  };
  $("#speaker-stop").onclick = () => run(d.stop_audio());
  $("#speaker-search").onclick = () => {
    $("#speaker-status").textContent = "Searching…";
    run(d.search_speakers());
  };
  $("#speaker-volume").onchange = (ev) => run(d.set_volume(ev.target.value / 100));
  const sp = await d.speakers();
  showSpeakers(sp);
  if (!sp.list.length) $("#speaker-search").click();
}

function showSpeakers(sp) {
  const sel = $("#speaker");
  const chosen = sp.current || sel.value;
  sel.replaceChildren(...sp.list.map((r) => el("option",
    r.supported ? { value: r.name } : { value: r.name, disabled: "" },
    r.supported ? r.name : `${r.name} (not supported)`)));
  if (!sp.list.length) sel.append(el("option", { value: "" }, "No speakers found"));
  if (chosen) sel.value = chosen;
  $("#speaker-volume").value = Math.round(sp.volume * 100);
  $("#speaker-stop").hidden = !sp.current;
  $("#speaker-play").hidden = !!sp.current;
  $("#speaker-play").disabled = !sp.list.some((r) => r.supported);
  $("#speaker-status").textContent = sp.current ? `Playing on ${sp.current}`
    : sp.list.length ? "" : "No speakers found. They must be on the same Wi-Fi as this PC.";
}

const STREAM_STATE = { streaming: "Mirroring", connecting: "Connecting…",
                       pin_required: "Waiting for the code" };
let pinFor = null;                // IP of the TV whose code or password is asked for

function initMirror(d) {
  enablePane("screen");
  const run = (p) => p.then(showScreens).catch((e) => {
    $("#mirror-status").textContent = String(e.message || e);
  });
  $("#mirror-start").onclick = () => {
    const opt = $("#screen").selectedOptions[0];
    if (!opt || !opt.value) return;
    $("#mirror-status").textContent = "Connecting…";
    run(d.mirror_to(opt.value, +opt.dataset.port || null));
  };
  $("#mirror-pin").onsubmit = (ev) => {
    ev.preventDefault();
    const code = $("#mirror-code").value;
    $("#mirror-code").value = "";
    run(d.mirror_pin(pinFor, code));
  };
  if (d.mirror_quality) d.mirror_quality().then((q) => showQuality(d, q)).catch(() => {});
  const poll = () => {
    if (document.visibilityState === "visible" && !$("#desktop-mirror").hidden) run(d.screens());
  };
  run(d.screens());
  setInterval(poll, 2000);
}

function showQuality(d, q) {
  const box = $("#mirror-quality");
  const detail = Object.fromEntries(q.options.map((o) => [o.id, o.detail]));
  box.replaceChildren(...q.options.map((o) => el("button", {
    type: "button", role: "radio", "aria-checked": String(o.id === q.current), "data-quality": o.id,
    onclick: () => d.set_mirror_quality(o.id).then((n) => showQuality(d, n))
      .catch((e) => { $("#mirror-status").textContent = String(e.message || e); }),
  }, o.label)));
  $("#mirror-quality-detail").textContent = detail[q.current] || "";
}

function showScreens(m) {
  const sel = $("#screen");
  const chosen = sel.value;
  sel.replaceChildren(...m.screens.map((s) => el("option", { value: s.ip, "data-port": s.port || "" },
    s.model ? `${s.name} (${s.model})` : s.name)));
  if (!m.screens.length) sel.append(el("option", { value: "" }, "Looking for TVs…"));
  if (chosen) sel.value = chosen;
  $("#mirror-start").disabled = !m.screens.length;
  $("#mirror-streams").replaceChildren(...m.streams.map((s) => el("li", {},
    el("span", { class: `dot ${s.state === "streaming" ? "on" : "off"}` }),
    el("span", { class: "grow" }, el("span", { class: "row-title" }, s.device), " ",
       el("span", { class: "caption" }, STREAM_STATE[s.state] || s.state)),
    el("button", { type: "button", class: "btn danger", onclick: () => {
      window.pywebview.api.stop_mirror(s.ip).then(showScreens).catch(() => {});
    } }, "Stop"))));
  const waiting = m.streams.find((s) => s.state === "pin_required");
  pinFor = waiting ? waiting.ip : null;
  $("#mirror-pin").hidden = !waiting;
  if (waiting) {
    const password = waiting.credential === "password";
    $("#mirror-pin-label").textContent = password
      ? `Password for ${waiting.device}` : `Code shown on ${waiting.device}`;
    $("#mirror-code").type = password ? "password" : "text";
    $("#mirror-code").inputMode = password ? "text" : "numeric";
  }
  $("#mirror-status").textContent = m.error
    || (m.screens.length ? "" : "Looking for Apple TVs and AirPlay TVs on your Wi-Fi…");
}

function restorePane() {
  if (wantedPane === null) {
    try { wantedPane = sessionStorage.getItem("beam-pane") || "nearby"; } catch { wantedPane = "nearby"; }
  }
  show(selection.length ? "nearby" : wantedPane, false);
}

function init() {
  for (const b of $$(".nav-item")) b.addEventListener("click", () => show(b.dataset.pane));
  window.addEventListener("resize", layoutPeers);
  const m = location.hash.match(/t=([^&]+)/);
  try {
    token = m ? decodeURIComponent(m[1]) : sessionStorage.getItem("multidrop-token") || "";
  } catch { token = m ? decodeURIComponent(m[1]) : ""; }
  if (!token) { $("#notoken").hidden = false; show("nearby"); return; }
  try { sessionStorage.setItem("multidrop-token", token); } catch { /* storage may be off */ }
  history.replaceState(null, "", location.pathname);   // token out of the address bar

  $("#refresh").addEventListener("click", () => loadDevices(true));
  makeDropTarget($("#qr-card"), (files) => upload({ qr: true }, files, "QR code"),
                 () => sendPaths({ qr: true }, selection, "QR code"));
  makeDropTarget($("#share-card"), startShare, shareSelection);
  $("#selection-clear").addEventListener("click", () => clearSelection().catch(() => {}));
  $("#share-stop").addEventListener("click", () =>
    api("/api/share", { method: "DELETE" }).catch(() => {}).finally(() => showShare(null)));
  api("/api/share").then(showShare).catch(() => {});
  $("#manual").addEventListener("submit", (ev) => {
    ev.preventDefault();
    const raw = $("#manual-address").value.trim();
    const mm = raw.match(/^\[?([^\]]+?)\]?(?::(\d+))?$/);
    if (!mm) return;
    const protocol = $("#manual-protocol").value;
    manual.push({ name: raw, type: null, targets: [{ protocol, address: mm[1], port: mm[2] ? +mm[2] : null }] });
    $("#manual-address").value = "";
    renderDevices();
  });
  $("#text-form").addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const target = $("#text-target").value;
    if (!target) { addLog("Choose a device to send the text to"); return; }
    const label = $("#text-target").selectedOptions[0].textContent;
    try {
      await sendText(JSON.parse(target), $("#text").value, label);
      $("#text").value = "";
    } catch (e) { addLog(`Couldn’t send the text: ${e.message}`); }
  });

  restorePane();
  loadStatus().catch((e) => { $("#status").textContent = `Error: ${e.message}`; $("#status-dot").className = "dot off"; });
  loadOffers().catch(() => {});
  loadSelection().catch(() => {});
  if (desktop()) initDesktop().catch(() => {});
  else window.addEventListener("pywebviewready", () => initDesktop().catch(() => {}), { once: true });
  loadDevices(false);
  renderJobs();
  connect();
}

init();
