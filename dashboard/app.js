"use strict";
// FabricPulse dashboard. Reads the JSON written by `python -m fabricpulse.run`
// (and `--sweep`) from ../results/. No build step, no dependencies.

const RESULTS = "../results";
const POLICIES = [
  { id: "ecmp", label: "ECMP", color: "var(--s1)", blurb: "static hash" },
  { id: "dlb", label: "DLB", color: "var(--s2)", blurb: "local load balancing" },
  { id: "telemetry", label: "Telemetry loop", color: "var(--s3)", blurb: "fabric-wide, 1 ms" },
];
const SCENARIOS = {
  healthy: ["Healthy", "No faults. What hash collisions cost on their own."],
  gray_link: ["Gray link", "A spine-to-leaf optic drops to 30% goodput while the port stays up."],
  flapping_link: ["Flapping link", "A leaf-to-spine link bounces: 30 ms down every 250 ms, ten times."],
  bad_nic: ["Bad NIC", "One training host's NIC drops to 40% goodput. No route avoids it."],
  noisy_neighbor: ["Noisy neighbour", "Bursty inference transfers share the fabric with training."],
};
const DARK = matchMedia("(prefers-color-scheme: dark)").matches;
// Sequential blue: near zero recedes toward the surface in either mode.
const RAMP = DARK
  ? ["#2c2c2a", "#104281", "#1c5cab", "#2a78d6", "#5598e7", "#86b6ef", "#cde2fb"]
  : ["#e1e0d9", "#cde2fb", "#9ec5f4", "#6da7ec", "#3987e5", "#256abf", "#104281"];

const state = { scenario: "gray_link", policy: "ecmp", t: 0, playing: false, link: null };
const cache = {};
let summary = null, sweep = null, timer = null;

const $ = (id) => document.getElementById(id);
const NS = "http://www.w3.org/2000/svg";
function svg(tag, attrs = {}, parent) {
  const e = document.createElementNS(NS, tag);
  for (const k in attrs) e.setAttribute(k, attrs[k]);
  if (parent) parent.appendChild(e);
  return e;
}
function el(tag, props = {}, parent) {
  const e = Object.assign(document.createElement(tag), props);
  if (parent) parent.appendChild(e);
  return e;
}
const fmt = (v, p = 1) => (v == null || Number.isNaN(v) ? "–" : v.toFixed(p));
const getJSON = (path) => fetch(path).then((r) => { if (!r.ok) throw new Error(path); return r.json(); });

// ---------------------------------------------------------------- tooltip
const tip = $("tip");
function showTip(evt, html) {
  tip.innerHTML = html;
  tip.hidden = false;
  const w = tip.offsetWidth, h = tip.offsetHeight;
  let x = evt.clientX + 14, y = evt.clientY + 14;
  if (x + w > innerWidth - 8) x = evt.clientX - w - 14;
  if (y + h > innerHeight - 8) y = evt.clientY - h - 14;
  tip.style.left = x + "px"; tip.style.top = y + "px";
}
const hideTip = () => { tip.hidden = true; };
const tipRow = (color, name, value, dash) =>
  `<div class="r"><span><i class="swatch${dash ? " dash" : ""}" style="border-color:${color}"></i>${name}</span><b>${value}</b></div>`;

// -------------------------------------------------------------- line chart
// series: [{name, color, pts: [[x, y]...], step, dash}]
function lineChart(host, o) {
  host.innerHTML = "";
  const W = Math.max(300, host.clientWidth), H = o.height || 220;
  const m = { l: 40, r: o.rightPad ?? 12, t: 10, b: 30 };
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": o.title || "" }, host);
  const [x0, x1] = o.xDomain, [y0, y1] = o.yDomain;
  const tx = o.xLog ? (v) => Math.log10(v) : (v) => v;
  const X = (v) => m.l + ((tx(v) - tx(x0)) / (tx(x1) - tx(x0))) * (W - m.l - m.r);
  const Y = (v) => H - m.b - ((v - y0) / (y1 - y0)) * (H - m.t - m.b);

  for (const v of o.yTicks) {
    svg("line", { class: "gridline", x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v) }, root);
    svg("text", { x: m.l - 6, y: Y(v) + 4, "text-anchor": "end" }, root).textContent = o.yFmt ? o.yFmt(v) : v;
  }
  svg("line", { class: "axisline", x1: m.l, x2: W - m.r, y1: H - m.b, y2: H - m.b }, root);
  for (const v of o.xTicks) {
    svg("text", { x: X(v), y: H - m.b + 16, "text-anchor": "middle" }, root).textContent = o.xFmt ? o.xFmt(v) : v;
  }
  if (o.xLabel) svg("text", { x: W - m.r, y: H - 2, "text-anchor": "end" }, root).textContent = o.xLabel;
  for (const r of o.refs || []) {
    svg("line", { class: "ref", x1: m.l, x2: W - m.r, y1: Y(r.y), y2: Y(r.y) }, root);
    const left = r.side === "left";
    svg("text", { class: "lbl", x: left ? m.l + 4 : W - m.r, y: Y(r.y) - 4, "text-anchor": left ? "start" : "end" }, root).textContent = r.label;
  }
  for (const v of o.vlines || []) {
    svg("line", { class: "ref", x1: X(v.x), x2: X(v.x), y1: m.t, y2: H - m.b }, root);
    svg("text", { class: "lbl", x: X(v.x) + 4, y: m.t + 9 }, root).textContent = v.label;
  }
  for (const s of o.series) {
    if (!s.pts.length) continue;
    let d = "";
    s.pts.forEach(([x, y], i) => {
      const px = X(x).toFixed(1), py = Y(Math.min(y1, Math.max(y0, y))).toFixed(1);
      d += i === 0 ? `M${px},${py}` : s.step ? `H${px}V${py}` : `L${px},${py}`;
    });
    const p = svg("path", { d, fill: "none", "stroke-width": s.dash ? 1.5 : 2, "stroke-linejoin": "round" }, root);
    p.style.stroke = s.color;
    if (s.dash) p.setAttribute("stroke-dasharray", "5 3");
    if (o.markers) for (const [x, y] of s.pts) {
      const c = svg("circle", { cx: X(x), cy: Y(y), r: 4, "stroke-width": 2 }, root);
      c.style.fill = s.color; c.style.stroke = "var(--surface)";
    }
  }
  const cursor = svg("line", { class: "cursor", y1: m.t, y2: H - m.b, visibility: "hidden" }, root);
  const setCursor = (x) => {
    if (x == null || x < x0 || x > x1) return cursor.setAttribute("visibility", "hidden");
    cursor.setAttribute("x1", X(x)); cursor.setAttribute("x2", X(x)); cursor.setAttribute("visibility", "visible");
  };
  // Hover: crosshair plus every series' value at that x.
  const hover = svg("line", { class: "cursor", y1: m.t, y2: H - m.b, visibility: "hidden", "stroke-dasharray": "2 2" }, root);
  const hit = svg("rect", { x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b, fill: "transparent" }, root);
  const at = (s, x) => {            // last point at or before x (or nearest for marker charts)
    let best = null;
    for (const p of s.pts) {
      if (o.markers) { if (!best || Math.abs(tx(p[0]) - tx(x)) < Math.abs(tx(best[0]) - tx(x))) best = p; }
      else if (p[0] <= x) best = p; else break;
    }
    return best || s.pts[0];
  };
  hit.addEventListener("mousemove", (e) => {
    const b = root.getBoundingClientRect();
    const frac = ((e.clientX - b.left) * (W / b.width) - m.l) / (W - m.l - m.r);
    let x = o.xLog ? 10 ** (tx(x0) + frac * (tx(x1) - tx(x0))) : x0 + frac * (x1 - x0);
    if (o.markers && o.series[0].pts.length) x = at(o.series[0], x)[0];
    hover.setAttribute("x1", X(x)); hover.setAttribute("x2", X(x)); hover.setAttribute("visibility", "visible");
    let html = `<div style="color:var(--muted)">${o.xTip ? o.xTip(x) : x}</div>`;
    for (const s of o.series) if (s.pts.length) {
      const p = at(s, x);
      html += tipRow(s.color, s.name, o.yTip ? o.yTip(p[1]) : p[1], s.dash);
    }
    showTip(e, html);
  });
  hit.addEventListener("mouseleave", () => { hover.setAttribute("visibility", "hidden"); hideTip(); });
  if (o.onClick) hit.addEventListener("click", (e) => {
    const b = root.getBoundingClientRect();
    o.onClick(x0 + (((e.clientX - b.left) * (W / b.width) - m.l) / (W - m.l - m.r)) * (x1 - x0));
  });
  return { setCursor };
}
function legend(host, items) {
  host.innerHTML = "";
  for (const it of items) {
    const s = el("span", {}, host);
    const sw = el("i", { className: "swatch" + (it.dash ? " dash" : "") }, s);
    sw.style.borderColor = it.color;
    s.append(it.name);
  }
}

// ------------------------------------------------------------------ data
async function loadScenario(sc) {
  if (!cache[sc]) {
    const traces = await Promise.all(POLICIES.map((p) => getJSON(`${RESULTS}/runs/${sc}__${p.id}.json`)));
    cache[sc] = Object.fromEntries(POLICIES.map((p, i) => [p.id, traces[i]]));
  }
  return cache[sc];
}
const trace = () => cache[state.scenario][state.policy];
const agg = (sc, po) => summary.aggregate.find((a) => a.scenario === sc && a.policy === po);

// ----------------------------------------------------------------- tiles
function renderPickers() {
  const host = $("scenarios");
  host.innerHTML = "";
  for (const id in SCENARIOS) {
    const b = el("button", { textContent: SCENARIOS[id][0], role: "tab" }, host);
    b.setAttribute("aria-selected", id === state.scenario);
    b.onclick = () => select({ scenario: id, link: null, t: 0 });
  }
  $("scenario-desc").textContent = SCENARIOS[state.scenario][1];
  const tiles = $("tiles");
  tiles.innerHTML = "";
  for (const p of POLICIES) {
    const a = agg(state.scenario, p.id);
    const b = el("button", { className: "tile" }, tiles);
    b.setAttribute("aria-pressed", p.id === state.policy);
    b.onclick = () => select({ policy: p.id });
    b.innerHTML =
      `<div class="name"><i class="dot" style="background:${p.color}"></i>${p.label}<span class="badge">${p.blurb}</span></div>` +
      `<div class="value">${fmt(a.mfu_pct)}<small>% MFU</small></div>` +
      `<div class="rows">${fmt(a.mfu_loss_rel_pct)}% lost to the network · iteration ${fmt(a.iter_ms_p50)} ms` +
      (a.inference_p99_slowdown ? ` · inference p99 ${fmt(a.inference_p99_slowdown, 2)}×` : "") + `</div>`;
  }
}

// ---------------------------------------------------------------- fabric
let fabricLinks = [];        // [{j, path, hit, leaf, spine, dir, name, comp}]
function parseLink(name) {
  const up = /^L(\d+)->S(\d+)$/.exec(name), dn = /^S(\d+)->L(\d+)$/.exec(name);
  return up ? { leaf: +up[1], spine: +up[2], dir: "up" } : { leaf: +dn[2], spine: +dn[1], dir: "dn" };
}
function buildFabric() {
  const tr = trace(), topo = tr.topology, host = $("fabric");
  host.innerHTML = "";
  const W = Math.max(320, host.clientWidth), H = 250, yS = 34, yL = 206;
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Leaf-spine fabric, links coloured by utilisation" }, host);
  const sx = (s) => W * (s + 0.5) / topo.n_spine, lx = (l) => W * (l + 0.5) / topo.n_leaf;
  const gLinks = svg("g", {}, root);
  fabricLinks = tr.fabric_links.map((idx, j) => {
    const name = topo.link_names[idx], p = parseLink(name);
    const off = p.dir === "up" ? -3.5 : 3.5;        // the two directions sit side by side
    const a = { x1: lx(p.leaf) + off, y1: yL - 14, x2: sx(p.spine) + off, y2: yS + 14 };
    const ring = svg("line", { ...a, "stroke-width": 9, "stroke-linecap": "round", visibility: "hidden" }, gLinks);
    ring.style.stroke = "var(--ink)";
    const line = svg("line", { ...a, "stroke-width": 3.5, "stroke-linecap": "round" }, gLinks);
    return { j, name, line, ring, a, comp: `${p.dir}:${name}`, ...p };
  });
  const gMarks = svg("g", {}, root);
  for (const L of fabricLinks) {
    L.mark = svg("text", { class: "node", x: (L.a.x1 * 0.65 + L.a.x2 * 0.35), y: (L.a.y1 * 0.65 + L.a.y2 * 0.35) + 4,
      "text-anchor": "middle", visibility: "hidden" }, gMarks);
    L.mark.textContent = "⚠";
    L.mark.style.fill = "var(--serious)";
    L.mark.style.paintOrder = "stroke"; L.mark.style.stroke = "var(--surface)"; L.mark.style.strokeWidth = "3px";
    const hit = svg("line", { ...L.a, "stroke-width": 12, stroke: "transparent" }, gMarks);
    hit.style.cursor = "pointer";
    hit.addEventListener("mousemove", (e) => {
      const t = trace(), i = state.t;
      showTip(e, `<b>${L.name}</b>` +
        `<div class="r"><span>peak utilisation</span><b>${Math.round(t.util_peak[i][L.j] * 100)}%</b></div>` +
        `<div class="r"><span>peak queue</span><b>${(t.queue_peak_gbit[i][L.j] * 125).toFixed(1)} MB</b></div>` +
        `<div class="r"><span>errored traffic</span><b>${t.err_mean_gbps[i][L.j].toFixed(0)} Gbps</b></div>` +
        `<div style="color:var(--muted)">click to inspect this link</div>`);
    });
    hit.addEventListener("mouseleave", hideTip);
    hit.addEventListener("click", () => select({ link: L.j }));
  }
  const node = (x, y, label) => {
    svg("rect", { class: "nodebox", x: x - 26, y: y - 13, width: 52, height: 26, rx: 6 }, root);
    svg("text", { class: "node", x, y: y + 4, "text-anchor": "middle" }, root).textContent = label;
  };
  for (let s = 0; s < topo.n_spine; s++) node(sx(s), yS, `S${s}`);
  for (let l = 0; l < topo.n_leaf; l++) {
    node(lx(l), yL, `L${l}`);
    svg("text", { x: lx(l), y: yL + 30, "text-anchor": "middle" }, root).textContent = `${topo.hosts_per_leaf} × ${topo.host_gbps}G`;
  }
  $("fabric-legend").innerHTML =
    `<span>0%<i class="ramp" style="background:linear-gradient(90deg,${RAMP.join(",")})"></i>100% peak utilisation</span>` +
    `<span><i class="swatch" style="border-color:var(--critical);border-top-width:5px"></i>overloaded (&gt;100%)</span>` +
    `<span><b style="color:var(--serious)">⚠</b> errors on link</span>` +
    `<span><i class="swatch dash" style="border-color:var(--muted)"></i>down or quarantined</span>`;
}
function paintFabric() {
  const tr = trace(), i = state.t, now = tr.t_ms[i] / 1000;
  const down = new Set();
  for (const [t, link, up] of tr.link_events) if (t <= now + tr.bucket_ms / 1000) up ? down.delete(link) : down.add(link);
  const quarantined = new Set(tr.advisories.filter((a) => a.t <= now).map((a) => a.component));
  for (const L of fabricLinks) {
    const u = tr.util_peak[i][L.j], over = u > 1.005;
    const off = down.has(tr.fabric_links[L.j]) || quarantined.has(L.comp);
    L.line.style.stroke = over ? "var(--critical)" : RAMP[Math.min(RAMP.length - 1, Math.ceil(u * (RAMP.length - 1) - 1e-9))];
    L.line.setAttribute("stroke-width", over ? 5.5 : 3.5);
    L.line.setAttribute("stroke-dasharray", off ? "5 6" : "");
    if (off && !over && u === 0) L.line.style.stroke = "var(--muted)";
    L.mark.setAttribute("visibility", tr.err_mean_gbps[i][L.j] > 0.5 ? "visible" : "hidden");
    L.ring.setAttribute("visibility", L.j === state.link ? "visible" : "hidden");
  }
  $("clock").textContent = `${now.toFixed(2)} s`;
  $("scrub").value = i;
  if (iterChart) iterChart.setCursor(now);
  if (resChart) resChart.setCursor(now);
}
function renderEvents() {
  const tr = trace(), host = $("events");
  host.innerHTML = "";
  host.className = "picker";
  const chip = (t, label) => {
    const b = el("button", { textContent: `${t.toFixed(2)} s  ${label}` }, host);
    b.onclick = () => select({ t: Math.max(0, Math.min(tr.t_ms.length - 1, Math.round((t * 1000) / tr.bucket_ms))) });
  };
  if (tr.meta.t_fault != null) chip(tr.meta.t_fault, `fault on ${tr.meta.fault}`);
  for (const a of tr.advisories) chip(a.t, `advisory: ${a.component}`);
  for (const mg of tr.migrations) chip(mg.t, `worker moved h${mg.from} → h${mg.to}`);
  if (!host.children.length) el("span", { className: "note", textContent: "No faults or advisories in this run." }, host);
}

// ---------------------------------------------------------------- charts
let iterChart = null, resChart = null;
function renderIter() {
  const sc = cache[state.scenario], dur = trace().t_ms.at(-1) / 1000 + 0.01;
  const series = POLICIES.map((p) => ({
    name: p.label, color: p.color, step: true,
    pts: sc[p.id].iterations.map((it) => [it.t_end, (it.t_end - it.t_start) * 1000]),
  }));
  const ideal = trace().meta.ideal_iter_ms;
  const ymax = Math.max(120, Math.ceil(Math.max(...series.flatMap((s) => s.pts.map((p) => p[1]))) / 40) * 40);
  const tf = trace().meta.t_fault;
  iterChart = lineChart($("iter-chart"), {
    title: "Iteration time by policy", series, xDomain: [0, dur], yDomain: [0, ymax],
    xTicks: [0, 1, 2, 3, 4].filter((v) => v <= dur), yTicks: [0, ymax / 4, ymax / 2, (3 * ymax) / 4, ymax],
    xFmt: (v) => v + " s", yFmt: (v) => Math.round(v), xLabel: "ms per iteration, by end time",
    refs: [{ y: ideal, label: `ideal ${Math.round(ideal)} ms` }],
    vlines: tf != null ? [{ x: tf, label: "fault" }] : [],
    xTip: (x) => `t = ${x.toFixed(2)} s`, yTip: (y) => `${y.toFixed(1)} ms`,
    onClick: (x) => select({ t: Math.max(0, Math.min(trace().t_ms.length - 1, Math.round((x * 1000) / trace().bucket_ms))) }),
  });
  legend($("iter-legend"), POLICIES.map((p) => ({ name: p.label, color: p.color })));
}
function hottestLink(tr) {
  let best = 0, bestN = -1;
  for (let j = 0; j < tr.fabric_links.length; j++) {
    let n = 0;
    for (let i = 0; i < tr.util_peak.length; i++) if (tr.util_peak[i][j] > 1.005) n++;
    if (n > bestN) { bestN = n; best = j; }
  }
  return best;
}
function renderRes() {
  const tr = trace(), j = state.link, per = Math.round(1000 / tr.bucket_ms);
  const color = POLICIES.find((p) => p.id === state.policy).color;
  const peak = tr.t_ms.map((t, i) => [t / 1000, tr.util_peak[i][j] * 100]);
  const polled = [];
  for (let i = 0; i + per <= tr.t_ms.length; i += per) {
    let s = 0;
    for (let k = i; k < i + per; k++) s += tr.util_mean[k][j];
    polled.push([tr.t_ms[i] / 1000, (s / per) * 100], [tr.t_ms[i + per - 1] / 1000, (s / per) * 100]);
  }
  const maxPeak = Math.max(...peak.map((p) => p[1])), maxPoll = Math.max(...polled.map((p) => p[1]));
  const ymax = Math.max(200, Math.ceil(maxPeak / 50) * 50);
  resChart = lineChart($("res-chart"), {
    title: "Link utilisation at two resolutions",
    series: [{ name: "10 ms peak", color, pts: peak }, { name: "1 s polled average", color: "var(--ink)", pts: polled, dash: true }],
    xDomain: [0, tr.t_ms.at(-1) / 1000 + 0.01], yDomain: [0, ymax],
    xTicks: [0, 1, 2, 3, 4], yTicks: [0, ymax / 4, ymax / 2, (3 * ymax) / 4, ymax],
    xFmt: (v) => v + " s", yFmt: (v) => Math.round(v) + "%", refs: [{ y: 100, label: "capacity" }],
    xTip: (x) => `t = ${x.toFixed(2)} s`, yTip: (y) => `${Math.round(y)}%`,
  });
  legend($("res-legend"), [{ name: "10 ms peak", color }, { name: "1 s polled average", color: "var(--ink)", dash: true }]);
  $("res-note").textContent = `Link ${tr.topology.link_names[tr.fabric_links[j]]}: the fine-grained view peaks at ` +
    `${Math.round(maxPeak)}% of capacity, a 1-second poller never reports more than ${Math.round(maxPoll)}%. ` +
    `Click any link in the fabric to inspect it.`;
}
function renderSweep() {
  const host = $("sweep");
  host.innerHTML = "";
  if (!sweep) return void el("p", { className: "note", textContent: "Run `python -m fabricpulse.run --sweep` to generate this view." }, host);
  const label = (p) => (p < 1 ? `${p * 1000} ms` : `${p} s`);
  for (const sc of ["gray_link", "flapping_link", "bad_nic"]) {
    const box = el("div", {}, host);
    el("h3", { textContent: SCENARIOS[sc][0] }, box);
    const rows = sweep.filter((r) => r.scenario === sc);
    const ecmp = agg(sc, "ecmp"), ideal = ecmp.ideal_mfu_pct;
    const chart = el("div", { className: "chart" }, box);
    lineChart(chart, {
      title: `MFU against control period, ${sc}`, height: 190, markers: true, xLog: true,
      series: [{ name: "MFU", color: "var(--s3)", pts: rows.map((r) => [r.control_period_s, r.mfu_pct]) }],
      xDomain: [0.0007, 45], yDomain: [20, 45], xTicks: rows.map((r) => r.control_period_s), yTicks: [20, 25, 30, 35, 40, 45],
      xFmt: label, yFmt: (v) => v + "%",
      refs: [{ y: ideal, label: `ideal ${fmt(ideal, 2)}%` }, { y: ecmp.mfu_pct, label: `ECMP ${fmt(ecmp.mfu_pct)}%`, side: "left" }],
      xTip: (x) => `control period ${label(x)}`, yTip: (y) => `${fmt(y)}%`,
    });
  }
}

// ------------------------------------------------------------ feed/table
function renderFeed() {
  const tr = trace(), adv = $("advisories"), sus = $("suspects");
  adv.innerHTML = ""; sus.innerHTML = "";
  if (!tr.advisories.length) {
    el("p", { className: "note", textContent: state.policy === "telemetry"
      ? "Nothing to report in this run."
      : "This policy has no health monitor, so it raises no advisories. Select the telemetry loop to see them." }, adv);
  } else {
    const ul = el("ul", { className: "feed" }, adv);
    for (const a of tr.advisories) {
      const li = el("li", {}, ul);
      const lag = tr.meta.t_fault != null ? ` <span class="badge">${((a.t - tr.meta.t_fault) * 1000).toFixed(0)} ms after onset</span>` : "";
      li.innerHTML = `<span class="t">${a.t.toFixed(3)} s</span><b>${a.component}</b><span class="badge">${a.kind}</span>${lag}` +
        `<div>${a.action}</div><div class="ev">evidence: ${Object.entries(a.evidence).map(([k, v]) => `${k} = ${v}`).join(", ")}</div>`;
    }
    for (const mg of tr.migrations) {
      el("li", {}, ul).innerHTML = `<span class="t">${mg.t.toFixed(3)} s</span><b>scheduler</b>` +
        `<div>moved the worker from host h${mg.from} to spare host h${mg.to} at the next iteration boundary</div>`;
    }
  }
  if (!tr.suspects.length) {
    el("p", { className: "note", textContent: "No transfer was slower than its path sharing explains." }, sus);
  } else {
    const rows = tr.suspects.map((s) => `<tr><td>${s.component}${s.component === tr.meta.fault ? '<span class="badge">injected fault</span>' : ""}</td>` +
      `<td></td><td>${s.score.toFixed(2)}</td><td>${s.slow_transfers} / ${s.transfers}</td></tr>`).join("");
    sus.innerHTML = `<table><tr><th>component</th><th></th><th>score</th><th>slow / all transfers</th></tr>${rows}</table>`;
  }
}
function renderTable() {
  const head = ["scenario", "policy", "MFU %", "lost %", "iter p50 ms", "iter p99 ms", "reroutes", "detected", "detect ms", "inference p99 ×"];
  const rows = summary.aggregate.map((a) => `<tr><td>${SCENARIOS[a.scenario]?.[0] || a.scenario}</td><td>${a.policy}</td>` +
    `<td>${fmt(a.mfu_pct)}</td><td>${fmt(a.mfu_loss_rel_pct)}</td><td>${fmt(a.iter_ms_p50)}</td><td>${fmt(a.iter_ms_p99)}</td>` +
    `<td>${fmt(a.reroutes, 0)}</td><td>${a.faulted_runs ? `${a.detected}/${a.faulted_runs}` : "–"}</td>` +
    `<td>${fmt(a.detect_ms, 0)}</td><td>${fmt(a.inference_p99_slowdown, 2)}</td></tr>`).join("");
  $("table").innerHTML = `<table><tr>${head.map((h) => `<th>${h}</th>`).join("")}</tr>${rows}</table>`;
}

// ------------------------------------------------------------- controller
async function select(patch) {
  const before = { ...state };
  Object.assign(state, patch);
  try {
    await loadScenario(state.scenario);
  } catch (e) {
    $("error").hidden = false;
    $("error").textContent = `Could not load ${e.message}. Run "python -m fabricpulse.run" and serve the repository root (python -m fabricpulse.serve).`;
    return;
  }
  const tr = trace();
  if (state.link == null) state.link = hottestLink(tr);
  state.t = Math.min(state.t, tr.t_ms.length - 1);
  const dataChanged = before.scenario !== state.scenario || before.policy !== state.policy || !fabricLinks.length;
  if (dataChanged) {
    renderPickers(); buildFabric(); renderEvents(); renderIter(); renderFeed();
    $("scrub").max = tr.t_ms.length - 1;
  }
  if (dataChanged || before.link !== state.link) renderRes();
  paintFabric();
}
function play(on) {
  state.playing = on;
  $("play").textContent = on ? "Pause" : "Play";
  clearInterval(timer);
  if (on) timer = setInterval(() => {
    const n = trace().t_ms.length;
    if (state.t >= n - 1) return play(false);
    state.t = Math.min(n - 1, state.t + 1);
    paintFabric();
  }, 50);
}

async function init() {
  try {
    summary = await getJSON(`${RESULTS}/summary.json`);
  } catch (e) {
    $("error").hidden = false;
    $("error").textContent = 'No results found. Run "python -m fabricpulse.run", then "python -m fabricpulse.serve".';
    return;
  }
  sweep = await getJSON(`${RESULTS}/latency_sweep.json`).catch(() => null);
  $("play").onclick = () => { if (!state.playing && state.t >= trace().t_ms.length - 1) state.t = 0; play(!state.playing); };
  $("scrub").oninput = (e) => { play(false); state.t = +e.target.value; paintFabric(); };
  let rz;
  addEventListener("resize", () => { clearTimeout(rz); rz = setTimeout(() => { fabricLinks = []; select({}); renderSweep(); }, 150); });
  renderTable();
  await select({});
  renderSweep();
  // Open on the moment the fault lands.
  const tf = trace().meta.t_fault;
  if (tf != null) select({ t: Math.round((tf * 1000) / trace().bucket_ms) + 5 });
}
init();
