/* Signal Scanner front end - vanilla JS, Plotly for charts. */
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
/* Read-only static build (GitHub Pages): GET calls map to pre-exported JSON files. */
const STATIC = !!window.STATIC_SITE;
const staticUrl = url => {
  const u = new URL(url, "http://x");
  if (u.pathname === "/api/ranking") return `api/ranking_${u.searchParams.get("horizon") || "medium"}.json`;
  if (u.pathname.startsWith("/api/stock/")) return `api/stock/${decodeURIComponent(u.pathname.slice(11)).trim().toUpperCase().replace(".", "-")}.json`;
  return u.pathname.replace(/^\//, "") + ".json";
};
const api = async (url, opts) => {
  if (STATIC) {
    if (opts && opts.method && opts.method !== "GET") throw new Error("Read-only demo: data updates automatically after each market close.");
    if (url.startsWith("/api/jobs")) return { refresh: { running: false }, backtest: { running: false } };
    let r = await fetch(staticUrl(url));
    if (!r.ok && url.startsWith("/api/stock/")) {
      const q = decodeURIComponent(url.slice(11)).trim().toLowerCase();
      const hit = (state.rows || []).find(x => (x.name || "").toLowerCase().includes(q));
      if (hit) r = await fetch(staticUrl("/api/stock/" + hit.ticker));
    }
    if (!r.ok) throw new Error(url.startsWith("/api/stock/") ? "That stock isn't in the S&P 500 list this scanner tracks." : `Couldn't load data (${r.status})`);
    return r.json();
  }
  const r = await fetch(url, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || `Request failed (${r.status})`);
  return j;
};
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const num = (v, d = 2) => (v == null || !isFinite(v)) ? "–" : Number(v).toFixed(d);
const pct = (v, d = 1, sign = true) => (v == null || !isFinite(v)) ? "–" : `${sign && v > 0 ? "+" : ""}${(v * 100).toFixed(d)}%`;
const signed = (v, d = 2) => (v == null || !isFinite(v)) ? "–" : `${v > 0 ? "+" : ""}${Number(v).toFixed(d)}`;
const cls = v => v == null ? "" : v > 0 ? "p" : v < 0 ? "n" : "";
const money = v => v == null ? "–" : v >= 1e12 ? `$${(v / 1e12).toFixed(2)}T` : v >= 1e9 ? `$${(v / 1e9).toFixed(1)}B` : `$${(v / 1e6).toFixed(0)}M`;
const css = n => getComputedStyle(document.documentElement).getPropertyValue(n).trim();
const ago = iso => {
  if (!iso) return "";
  const m = (Date.now() - new Date(iso).getTime()) / 60000;
  if (m < 60) return `${Math.max(1, Math.round(m))}m ago`;
  if (m < 60 * 24) return `${Math.round(m / 60)}h ago`;
  return `${Math.round(m / 1440)}d ago`;
};
function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("on");
  clearTimeout(toast._t); toast._t = setTimeout(() => t.classList.remove("on"), 3200);
}
function verdict(s) {
  if (s == null) return "No score";
  if (s >= 0.6) return "Strong buy signal vs peers";
  if (s >= 0.25) return "Leaning buy";
  if (s > -0.25) return "Neutral";
  if (s > -0.6) return "Leaning avoid";
  return "Strong avoid signal vs peers";
}

const state = { meta: null, rows: [], horizon: "medium", sortCol: "score_medium", sortDir: -1, sector: "", filter: "", selected: null, weights: null, bt: null, btH: "medium" };
const HKEYS = ["short", "medium", "long"];

/* ---------------- header / meta ---------------- */
async function loadMeta() {
  state.meta = await api("/api/meta");
  const m = state.meta, mac = m.macro || {};
  const item = (label, v, chg, unit = "%", d = 2, invert = false) => {
    if (v == null) return "";
    const c = chg == null ? "" : `<span class="d ${(invert ? -chg : chg) > 0 ? "up" : (invert ? -chg : chg) < 0 ? "down" : ""}">${signed(chg, 2)} 3m</span>`;
    return `<div class="m"><small>${label}</small><b>${num(v, d)}${unit}</b>${c}</div>`;
  };
  let h = "";
  if (m.benchmark) {
    const b = m.benchmark;
    h += `<div class="m"><small>${esc(b.ticker)}</small><b>${num(b.close, 2)}</b><span class="d ${cls(b.chg_1d) === "p" ? "up" : "down"}">${pct(b.chg_1d, 2)} today</span></div>`;
  }
  h += item("10Y yield", mac.DGS10?.value, mac.DGS10?.change);
  h += item("2Y yield", mac.DGS2?.value, mac.DGS2?.change);
  h += item("Breakeven infl.", mac.T10YIE?.value, mac.T10YIE?.change);
  h += item("Fed funds", mac.DFF?.value, mac.DFF?.change);
  h += item("CPI y/y", mac.CPI_YOY?.value, null, "%", 1);
  h += item("VIX", mac.VIX?.value, mac.VIX?.change, "", 1, true);
  $("#macro").innerHTML = h;
  const lr = m.last_refresh ? new Date(m.last_refresh) : null;
  $("#asof").innerHTML = m.last_bar ? `Prices to ${esc(m.last_bar)}<br>${lr ? "refreshed " + ago(lr.toISOString()) : ""}` : "No data yet";
  // horizon toggle + sector list
  $("#horizonSeg").innerHTML = HKEYS.map(k => `<button aria-pressed="${k === state.horizon}" data-h="${k}">${esc(m.horizons[k].label)}</button>`).join("");
  $$("#horizonSeg button").forEach(b => b.onclick = () => setHorizon(b.dataset.h));
  const sel = $("#sectorSel"), cur = sel.value;
  sel.innerHTML = `<option value="">All sectors</option>` + (m.sectors || []).map(s => `<option>${esc(s)}</option>`).join("");
  sel.value = cur;
}
function setHorizon(h) {
  state.horizon = h; state.sortCol = `score_${h}`; state.sortDir = -1;
  $$("#horizonSeg button").forEach(b => b.setAttribute("aria-pressed", b.dataset.h === h));
  renderRanking();
}

/* ---------------- ranking ---------------- */
async function loadRanking() {
  const r = await api("/api/ranking?horizon=" + state.horizon);
  state.rows = r.rows || [];
  renderRanking();
}
const COLS = [
  { k: "_pos", t: "#", l: false },
  { k: "ticker", t: "Stock", l: true },
  { k: "sector", t: "Sector", l: true },
  { k: "close", t: "Price" },
  { k: "chg_1d", t: "1 day" },
  { k: "pe", t: "P/E" },
  { k: "pe_rel", t: "P/E vs sector" },
  { k: "_glyph", t: "1W · 1M · 6M", sortable: false },
  { k: "score_short", t: "1 Week" },
  { k: "score_medium", t: "1 Month" },
  { k: "score_long", t: "6 Months" },
  { k: "news_sentiment", t: "News" },
];
function glyph(r) {
  return `<span class="glyph" aria-hidden="true">${HKEYS.map(h => {
    const v = r["score_" + h];
    if (v == null) return `<i></i>`;
    const w = Math.min(50, Math.abs(v) * 50);
    return `<i class="${v >= 0 ? "p" : "n"} ${h === state.horizon ? "a" : ""}" style="width:${w}%"></i>`;
  }).join("")}</span>`;
}
function renderRanking() {
  const body = $("#rankingBody");
  if (!state.meta?.has_data) {
    body.innerHTML = `<div class="empty"><h2>No market data yet</h2>
      <p>Press <b>Refresh data</b> to download about 12 years of daily prices for the S&amp;P 500, Treasury yields and inflation expectations from FRED, P/E history, and today's headlines.
      The first run takes roughly 10–20 minutes. After that, each daily refresh only adds the new days and takes a few minutes.</p>
      <button class="btn primary" onclick="startRefresh()">Refresh data</button></div>`;
    $("#count").textContent = "";
    return;
  }
  let rows = state.rows.slice();
  if (state.sector) rows = rows.filter(r => r.sector === state.sector);
  if (state.filter) {
    const f = state.filter.toLowerCase();
    rows = rows.filter(r => r.ticker.toLowerCase().includes(f) || (r.name || "").toLowerCase().includes(f));
  }
  const k = state.sortCol, d = state.sortDir;
  rows.sort((a, b) => {
    const x = a[k], y = b[k];
    if (x == null && y == null) return 0; if (x == null) return 1; if (y == null) return -1;
    return typeof x === "string" ? d * x.localeCompare(y) : d * (x - y);
  });
  // rank number follows the active horizon's score order
  const order = state.rows.slice().filter(r => r["score_" + state.horizon] != null)
    .sort((a, b) => b["score_" + state.horizon] - a["score_" + state.horizon]);
  const rankOf = new Map(order.map((r, i) => [r.ticker, i + 1]));
  $("#count").textContent = `${rows.length} stocks · ranked ${state.meta.horizons[state.horizon].label.toLowerCase()} ahead`;
  const th = COLS.map(c => {
    const sortable = c.sortable !== false && c.k !== "_pos";
    const aria = state.sortCol === c.k ? `aria-sort="${state.sortDir < 0 ? "descending" : "ascending"}"` : "";
    return `<th class="${c.l ? "l" : ""}" ${aria} ${sortable ? `data-k="${c.k}" tabindex="0"` : ""}>${c.t}</th>`;
  }).join("");
  const tr = rows.map(r => {
    const sc = h => { const v = r["score_" + h]; return `<span class="sc ${cls(v)} ${h === state.horizon ? "active" : ""}">${signed(v)}</span>`; };
    return `<tr data-t="${esc(r.ticker)}" class="${state.selected === r.ticker ? "sel" : ""}" tabindex="0">
      <td class="pos">${rankOf.get(r.ticker) ?? ""}</td>
      <td class="l"><span class="tkr">${esc(r.ticker)}</span><span class="nm">${esc(r.name)}</span></td>
      <td class="l"><span class="sec">${esc(r.sector)}</span></td>
      <td>${num(r.close)}</td>
      <td class="${cls(r.chg_1d) === "p" ? "up" : cls(r.chg_1d) === "n" ? "down" : ""}">${pct(r.chg_1d, 2)}</td>
      <td>${r.pe ? num(r.pe, 1) : "–"}</td>
      <td class="${r.pe_rel == null ? "" : r.pe_rel < 0 ? "up" : "down"}">${pct(r.pe_rel, 0)}</td>
      <td>${glyph(r)}</td>
      <td>${sc("short")}</td><td>${sc("medium")}</td><td>${sc("long")}</td>
      <td>${r.news_sentiment == null ? "–" : `<span class="dot ${cls(r.news_sentiment)}"></span>${signed(r.news_sentiment)}`}</td></tr>`;
  }).join("");
  body.innerHTML = `<div class="tablewrap"><table class="rank"><thead><tr>${th}</tr></thead><tbody>${tr}</tbody></table></div>
    <p class="note">Scores run from −1 to +1 and are relative to the other S&amp;P 500 stocks on the same day: 0 is average. P/E vs sector compares to the median P/E of the stock's GICS sector, so banks are judged against banks and tech against tech (green = cheaper than its sector).</p>`;
  $$(".rank th[data-k]").forEach(h => {
    const go = () => { const kk = h.dataset.k; state.sortDir = state.sortCol === kk ? -state.sortDir : (["ticker", "sector", "pe"].includes(kk) ? 1 : -1); state.sortCol = kk; renderRanking(); };
    h.onclick = go; h.onkeydown = e => { if (e.key === "Enter") go(); };
  });
  $$(".rank tbody tr").forEach(tr => {
    tr.onclick = () => openStock(tr.dataset.t);
    tr.onkeydown = e => { if (e.key === "Enter") openStock(tr.dataset.t); };
  });
}
$("#sectorSel").onchange = e => { state.sector = e.target.value; renderRanking(); };
$("#filterTxt").oninput = e => { state.filter = e.target.value.trim(); renderRanking(); };

/* ---------------- search ---------------- */
let hl = -1;
$("#search").oninput = () => {
  const q = $("#search").value.trim().toLowerCase(), box = $("#suggest");
  if (!q) { box.classList.remove("on"); return; }
  const m = state.rows.filter(r => r.ticker.toLowerCase().startsWith(q) || (r.name || "").toLowerCase().includes(q))
    .sort((a, b) => (b.ticker.toLowerCase() === q) - (a.ticker.toLowerCase() === q) || (b.ticker.toLowerCase().startsWith(q)) - (a.ticker.toLowerCase().startsWith(q)))
    .slice(0, 10);
  hl = -1;
  box.innerHTML = m.length ? m.map(r => `<button data-t="${esc(r.ticker)}"><span class="tk">${esc(r.ticker)}</span><span class="nm">${esc(r.name)}</span><span class="sc ${cls(r["score_" + state.horizon])}" style="margin-left:auto">${signed(r["score_" + state.horizon])}</span></button>`).join("")
    : `<div style="padding:10px 12px;color:var(--muted)">No S&amp;P 500 match for “${esc(q)}”.</div>`;
  box.classList.add("on");
  $$("#suggest button").forEach(b => b.onclick = () => { openStock(b.dataset.t); box.classList.remove("on"); $("#search").value = ""; });
};
$("#search").onkeydown = e => {
  const items = $$("#suggest button");
  if (e.key === "ArrowDown") { hl = Math.min(items.length - 1, hl + 1); e.preventDefault(); }
  else if (e.key === "ArrowUp") { hl = Math.max(0, hl - 1); e.preventDefault(); }
  else if (e.key === "Enter") {
    const t = items[Math.max(0, hl)]?.dataset.t || $("#search").value.trim();
    if (t) { openStock(t); $("#suggest").classList.remove("on"); $("#search").value = ""; }
    return;
  } else if (e.key === "Escape") { $("#suggest").classList.remove("on"); return; }
  items.forEach((b, i) => b.classList.toggle("hl", i === hl));
};
document.addEventListener("click", e => { if (!e.target.closest(".search")) $("#suggest").classList.remove("on"); });

/* ---------------- stock detail ---------------- */
function plotLayout(extra = {}) {
  return Object.assign({
    margin: { l: 46, r: 12, t: 8, b: 30 }, paper_bgcolor: "rgba(0,0,0,0)", plot_bgcolor: "rgba(0,0,0,0)",
    font: { family: "Archivo, system-ui, sans-serif", size: 11.5, color: css("--muted") },
    xaxis: { gridcolor: css("--line"), linecolor: css("--line"), zeroline: false },
    yaxis: { gridcolor: css("--line"), linecolor: css("--line"), zeroline: false },
    showlegend: true, legend: { orientation: "h", y: 1.08, x: 0 }, hovermode: "x unified",
  }, extra);
}
const PCFG = { displayModeBar: false, responsive: true };

async function openStock(t) {
  const panel = $("#panel");
  state.selected = t;
  $$(".rank tbody tr").forEach(tr => tr.classList.toggle("sel", tr.dataset.t === t));
  panel.innerHTML = `<div class="panel-head"><h2>${esc(t)}</h2><button class="close" aria-label="Close" style="margin-left:auto">×</button></div><div class="panel-body"><p class="note">Loading…</p></div>`;
  openPanel();
  $(".close", panel).onclick = closePanel;
  let d;
  try { d = await api("/api/stock/" + encodeURIComponent(t)); }
  catch (e) { $(".panel-body", panel).innerHTML = `<p>${esc(e.message)}</p>`; return; }
  renderStock(d);
}
function openPanel() { $("#panel").classList.add("on"); $("#panel").setAttribute("aria-hidden", "false"); $("#scrim").classList.add("on"); }
function closePanel() { $("#panel").classList.remove("on"); $("#panel").setAttribute("aria-hidden", "true"); $("#scrim").classList.remove("on"); }
$("#scrim").onclick = closePanel;
document.addEventListener("keydown", e => { if (e.key === "Escape" && $("#panel").classList.contains("on")) closePanel(); });

function renderStock(d) {
  const r = d.row, info = d.info || {}, H = state.meta.horizons, panel = $("#panel");
  const gauge = h => {
    const v = r["score_" + h], w = v == null ? 0 : Math.abs(v) * 50;
    const pos = v >= 0 ? `left:50%;width:${w}%;background:var(--bull)` : `right:50%;width:${w}%;background:var(--bear)`;
    return `<div class="gauge"><div class="lab">${esc(H[h].label)} ahead</div>
      <div class="val ${v >= 0 ? "up" : "down"}">${signed(v)}</div>
      <div class="bar"><i style="${pos}"></i></div><div class="verdict">${verdict(v)}</div></div>`;
  };
  const rets = Object.entries(d.returns).map(([k, v]) => `<div><small>${k} return</small><b class="${cls(v) === "p" ? "up" : cls(v) === "n" ? "down" : ""}">${pct(v)}</b></div>`).join("");
  const target = info.targetMeanPrice ? `${num(info.targetMeanPrice)} <span class="${info.targetMeanPrice > r.close ? "up" : "down"}">(${pct(info.targetMeanPrice / r.close - 1, 0)})</span>` : "–";
  const mac = d.macro || {};
  // betas are % return per +1pp; show per +0.25pp, a typical sizeable move
  const sens = v => v == null ? "–" : `${signed(v * 0.25, 2)}%`;
  const peersRows = (d.peers || []).filter(p => p.pe).slice(0, 60);
  const peersHtml = peersRows.map(p => `<tr class="${p.ticker === d.ticker ? "me" : ""}"><td class="l">${esc(p.ticker)}</td><td class="l" style="max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(p.name)}</td><td>${num(p.pe, 1)}</td><td>${num(p.fwd_pe, 1)}</td><td class="${cls(p.score_long) === "p" ? "up" : "down"}">${signed(p.score_long)}</td></tr>`).join("");
  const heads = (d.headlines || []).map(h => `<li><span class="dot ${cls(h.sentiment)}" title="sentiment ${signed(h.sentiment)}"></span>
    <div><a href="${esc(h.url)}" target="_blank" rel="noopener">${esc(h.title)}</a><div class="meta">${esc(h.source || "")} · ${ago(h.published)} · via ${esc(h.via)}</div></div>
    <span class="s ${cls(h.sentiment) === "p" ? "up" : cls(h.sentiment) === "n" ? "down" : ""}">${signed(h.sentiment)}</span></li>`).join("");

  panel.innerHTML = `
  <div class="panel-head">
    <div><h2>${esc(d.ticker)}</h2><div class="sub">${esc(r.name)} · ${esc(r.sector)}${r.sub_industry ? " · " + esc(r.sub_industry) : ""}</div></div>
    <div class="px"><b>${num(r.close)}</b><div class="${cls(r.chg_1d) === "p" ? "up" : "down"}">${pct(r.chg_1d, 2)} on ${esc(String(r.date).slice(0, 10))}</div></div>
    <button class="close" aria-label="Close">×</button>
  </div>
  <div class="panel-body">
    <div class="block"><h3>Buy scores <small>−1 to +1, relative to the S&amp;P 500 today</small></h3>
      <div class="gauges">${HKEYS.map(gauge).join("")}</div></div>

    <div class="block"><h3>Price <small>2 years, with 50- and 200-day averages</small></h3>
      <div class="chart" id="cPrice"></div><div class="kv" style="margin-top:8px">${rets}</div></div>

    <div class="block"><h3>Why this score <small>each feature's push on the composite</small></h3>
      <div class="htabs seg" id="cTabs">${HKEYS.map(h => `<button data-h="${h}" aria-pressed="${h === state.horizon}">${esc(H[h].label)}</button>`).join("")}</div>
      <div class="contrib" id="contrib"></div>
      <p class="note">Bars to the right push the score up, to the left pull it down. Each value is the feature's z-score versus all S&amp;P 500 stocks times its weight.</p></div>

    <div class="block"><h3>Score history <small>last year, current weights</small></h3><div class="chart sm" id="cHist"></div></div>

    <div class="grid2">
      <div class="block"><h3>Valuation vs sector</h3>
        <div class="kv">
          <div><small>P/E (trailing)</small><b>${num(r.pe, 1)}</b></div>
          <div><small>P/E (forward)</small><b>${num(r.fwd_pe, 1)}</b></div>
          <div><small>${esc(r.sector)} median P/E</small><b>${num(r.sector_pe, 1)}</b></div>
          <div><small>Premium to sector</small><b class="${r.pe_rel == null ? "" : r.pe_rel < 0 ? "up" : "down"}">${pct(r.pe_rel, 0)}</b></div>
          <div><small>Price / book</small><b>${num(info.priceToBook, 1)}</b></div>
          <div><small>EV / EBITDA</small><b>${num(info.enterpriseToEbitda, 1)}</b></div>
          <div><small>Market cap</small><b>${money(info.marketCap ?? r.market_cap)}</b></div>
          <div><small>Dividend yield</small><b>${info.dividendYield == null ? "–" : num(info.dividendYield > 1 ? info.dividendYield : info.dividendYield * 100, 2) + "%"}</b></div>
        </div></div>
      <div class="block"><h3>Rates &amp; inflation sensitivity <small>2-year weekly</small></h3>
        <div class="kv">
          <div><small>Market beta</small><b>${num(mac.beta_mkt, 2)}</b></div>
          <div><small>Move per +0.25pp 10Y yield</small><b>${sens(mac.beta_rate)}</b></div>
          <div><small>Move per +0.25pp breakeven</small><b>${sens(mac.beta_infl)}</b></div>
          <div><small>Correlation with yield changes</small><b>${num(mac.corr_rate, 2)}</b></div>
        </div>
        <p class="note">Weekly returns after removing the market's move, regressed on changes in the 10-year Treasury yield and 10-year breakeven inflation. Banks typically gain when yields rise; long-duration growth stocks usually lose.</p></div>
    </div>

    <div class="grid2">
      <div class="block"><h3>Fundamentals &amp; analysts</h3>
        <div class="kv">
          <div><small>Revenue growth</small><b>${pct(info.revenueGrowth)}</b></div>
          <div><small>Earnings growth</small><b>${pct(info.earningsGrowth)}</b></div>
          <div><small>Profit margin</small><b>${pct(info.profitMargins, 1, false)}</b></div>
          <div><small>Return on equity</small><b>${pct(info.returnOnEquity, 1, false)}</b></div>
          <div><small>Analyst view</small><b>${esc(info.recommendationKey ?? "–")}</b></div>
          <div><small>Mean target</small><b>${target}</b></div>
        </div>
        ${info.longBusinessSummary ? `<p class="about">${esc(info.longBusinessSummary)}</p>` : ""}</div>
      <div class="block"><h3>All features <small>raw value and z-score today</small></h3>
        <table class="mini"><thead><tr><th class="l">Feature</th><th>Value</th><th>z</th></tr></thead><tbody>
        ${d.features.map(f => `<tr><td class="l" title="${esc(f.desc)}">${esc(f.feature)}</td><td>${num(f.raw, 3)}</td><td class="${cls(f.z) === "p" ? "up" : cls(f.z) === "n" ? "down" : ""}">${signed(f.z)}</td></tr>`).join("")}
        </tbody></table></div>
    </div>

    <div class="block"><h3>Headlines <small>newest first, last 7 days</small>
      <button class="btn" id="newsNow" style="float:right;padding:3px 10px">Fetch latest</button></h3>
      ${heads ? `<ul class="news">${heads}</ul>` : `<p class="note">No headlines collected for this stock yet. Fetch latest pulls Google News and Yahoo now.</p>`}</div>

    <div class="block"><h3>${esc(r.sector)} peers by P/E</h3>
      <div style="max-height:320px;overflow:auto"><table class="mini"><thead><tr><th class="l">Ticker</th><th class="l">Name</th><th>P/E</th><th>Fwd P/E</th><th>6M score</th></tr></thead><tbody>${peersHtml}</tbody></table></div></div>
  </div>`;
  $(".close", panel).onclick = closePanel;
  if (STATIC) $("#newsNow").hidden = true;
  $("#newsNow").onclick = async e => {
    e.target.disabled = true; e.target.textContent = "Fetching…";
    try { await api("/api/news/" + d.ticker, { method: "POST" }); toast("Headlines updated"); openStock(d.ticker); loadRanking(); }
    catch (err) { toast(err.message); e.target.disabled = false; e.target.textContent = "Fetch latest"; }
  };
  const showContrib = h => {
    $$("#cTabs button").forEach(b => b.setAttribute("aria-pressed", b.dataset.h === h));
    const parts = d.contributions[h] || [];
    const mx = Math.max(0.3, ...parts.map(p => Math.abs(p.contribution || 0)));
    $("#contrib").innerHTML = parts.map(p => {
      const v = p.contribution || 0, w = Math.abs(v) / mx * 50;
      const st = v >= 0 ? `left:50%;width:${w}%;background:var(--bull)` : `right:50%;width:${w}%;background:var(--bear)`;
      return `<div class="crow"><span class="f" title="${esc(p.desc)} (weight ${signed(p.weight)})">${esc(p.feature)}</span><span class="track"><i style="${st}"></i></span><span class="v">${signed(v)}</span></div>`;
    }).join("") || `<p class="note">No features weighted for this horizon.</p>`;
  };
  $$("#cTabs button").forEach(b => b.onclick = () => showContrib(b.dataset.h));
  showContrib(state.horizon);

  const P = d.price;
  Plotly.newPlot("cPrice", [
    { x: P.dates, y: P.close, name: "Close", type: "scatter", mode: "lines", line: { color: css("--ink"), width: 1.6 } },
    { x: P.dates, y: P.ema50, name: "50-day", type: "scatter", mode: "lines", line: { color: css("--bull"), width: 1.2 } },
    { x: P.dates, y: P.ema200, name: "200-day", type: "scatter", mode: "lines", line: { color: css("--bear"), width: 1.2 } },
  ], plotLayout(), PCFG);
  const SH = d.score_history, colors = { short: css("--faint"), medium: css("--ink"), long: css("--bull") };
  Plotly.newPlot("cHist", HKEYS.filter(h => SH[h]).map(h => ({
    x: SH[h].dates, y: SH[h].values, name: H[h].label, type: "scatter", mode: "lines",
    line: { color: colors[h], width: h === state.horizon ? 2.2 : 1.2 },
  })), plotLayout({ yaxis: { range: [-1, 1], gridcolor: css("--line"), zeroline: true, zerolinecolor: css("--faint") } }), PCFG);
}

/* ---------------- refresh job ---------------- */
async function startRefresh() {
  try {
    const r = await api("/api/refresh", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    if (!r.started) toast("A refresh is already running");
    pollJobs();
  } catch (e) { toast(e.message); }
}
$("#refreshBtn").onclick = startRefresh;
let polling = false;
async function pollJobs() {
  if (polling) return; polling = true;
  let wasRunning = { refresh: false, backtest: false };
  const tick = async () => {
    let j;
    try { j = await api("/api/jobs"); } catch { setTimeout(tick, 3000); return; }
    const rf = j.refresh, bt = j.backtest, active = rf.running ? rf : bt.running ? bt : null;
    $("#refreshBtn").disabled = rf.running;
    $("#refreshBtn").textContent = rf.running ? "Refreshing…" : "Refresh data";
    $("#btRun").disabled = bt.running;
    $("#btRun").textContent = bt.running ? "Running…" : "Run backtest";
    if (active) {
      const p = active.total ? active.done / active.total : 0;
      $("#progBar").style.width = `${Math.max(3, p * 100)}%`;
      $("#progText").textContent = `${active === rf ? "Refreshing" : "Backtest"}: ${active.stage}${active.total ? ` ${active.done}/${active.total}` : ""}`;
      $("#progText").classList.add("on");
    } else {
      $("#progBar").style.width = "0"; $("#progText").classList.remove("on");
    }
    if (wasRunning.refresh && !rf.running) {
      if (rf.error) toast("Refresh failed: " + rf.error);
      else toast("Data refreshed and re-ranked");
      await loadMeta(); await loadRanking();
    }
    if (wasRunning.backtest && !bt.running) {
      if (bt.error) toast("Backtest failed: " + bt.error); else { toast("Backtest finished"); loadBacktest(); }
    }
    wasRunning = { refresh: rf.running, backtest: bt.running };
    if (rf.running || bt.running) setTimeout(tick, 1200);
    else polling = false;
  };
  tick();
}

/* ---------------- backtest view ---------------- */
$("#btRun").onclick = async () => {
  try { const r = await api("/api/backtest", { method: "POST" }); if (!r.started) toast("Backtest already running"); pollJobs(); }
  catch (e) { toast(e.message); }
};
async function loadBacktest() {
  state.bt = await api("/api/backtest");
  renderBacktest();
}
function renderBacktest() {
  const b = state.bt, body = $("#btBody");
  if (!b || !b.horizons) {
    if (STATIC) { body.innerHTML = `<div class="empty"><h2>Backtest results coming soon</h2><p>The walk-forward backtest is re-run every weekend and will appear here.</p></div>`; return; }
    body.innerHTML = `<div class="empty"><h2>No backtest yet</h2><p>Run it after the first data refresh. It replays every week since ${esc("2016")} using only data available at the time, so you can see whether each horizon's score actually ranked winners above losers before you rely on it.</p></div>`;
    return;
  }
  $("#btInfo").textContent = `${b.n_dates} weekly score dates from ${b.first_date} to ${b.last_date}, ${b.n_tickers} stocks. Run ${ago(b.generated_at)}.`;
  const H = b.horizons, h = state.btH in H ? state.btH : "medium", v = H[h];
  const icq = x => x == null ? "" : Math.abs(x) >= 0.05 ? "strong" : Math.abs(x) >= 0.02 ? "useful" : "weak";
  const oosRow = (lab, o) => o ? `<tr><td class="l">${lab}</td><td>${num(o.ic?.mean, 3)}</td><td>${num(o.ic?.t, 1)}</td><td>${pct(o.top_minus_bottom, 2)}</td></tr>` : "";
  const fi = Object.entries(v.feature_ic || {}).sort((a, b2) => Math.abs(b2[1].mean || 0) - Math.abs(a[1].mean || 0));
  body.innerHTML = `
    <div class="seg" id="btTabs" style="margin-bottom:14px">${HKEYS.filter(k => H[k]).map(k => `<button data-h="${k}" aria-pressed="${k === h}">${esc(H[k].label)}</button>`).join("")}</div>
    <div class="block" style="margin-bottom:16px">
      <div class="kpis">
        <div class="kpi"><small>Mean IC</small><b class="${cls(v.ic.mean) === "p" ? "up" : "down"}">${num(v.ic.mean, 3)}</b><span>${icq(v.ic.mean)} · ${pct(v.ic.pos_frac, 0, false)} of weeks positive</span></div>
        <div class="kpi"><small>IC t-stat</small><b>${num(v.ic.t, 1)}</b><span>${v.ic.n} non-overlapping periods; above 2 is significant</span></div>
        <div class="kpi"><small>Top minus bottom quintile</small><b class="${cls(v.top_minus_bottom) === "p" ? "up" : "down"}">${pct(v.top_minus_bottom, 2)}</b><span>per ${v.days}-day period · ${pct(v.top_minus_bottom_annualised, 1)} a year</span></div>
        <div class="kpi"><small>Top quintile beat the average</small><b>${pct(v.top_hit_rate, 0, false)}</b><span>of score dates</span></div>
      </div></div>
    <div class="grid2">
      <div class="block"><h3>Excess return by score quintile <small>vs equal-weight universe</small></h3><div class="chart sm" id="bQ"></div></div>
      <div class="block"><h3>Top quintile, cumulative excess <small>non-overlapping ${v.days}-day periods</small></h3><div class="chart sm" id="bCurve"></div></div>
    </div>
    <div class="block" style="margin-top:16px"><h3>Rolling 1-year IC</h3><div class="chart sm" id="bIC"></div></div>
    <div class="grid2" style="margin-top:16px">
      <div class="block"><h3>Which features predicted ${esc(v.label.toLowerCase())} returns</h3>
        <table class="mini"><thead><tr><th class="l">Feature</th><th>Mean IC</th><th>t-stat</th><th>Coverage</th><th>Your weight</th></tr></thead><tbody>
        ${fi.map(([f, s]) => `<tr><td class="l" title="${esc(state.meta.features[f] || "")}">${esc(f)}</td><td class="${cls(s.mean) === "p" ? "up" : "down"}">${num(s.mean, 3)}</td><td>${num(s.t, 1)}</td><td>${pct(b.coverage[f], 0, false)}</td><td>${state.weights?.weights?.[h]?.[f] != null ? signed(state.weights.weights[h][f]) : "–"}</td></tr>`).join("")}
        </tbody></table>
        <p class="note">A feature with a negative IC is bearish: give it a negative weight. Low coverage means little history (e.g. news).</p></div>
      <div class="block"><h3>Out-of-sample check <small>from ${esc(b.oos_start)}</small></h3>
        <table class="mini"><thead><tr><th class="l">Weights</th><th>Mean IC</th><th>t-stat</th><th>Top − bottom</th></tr></thead><tbody>
        ${oosRow("Your current weights", v.oos?.current)}${oosRow("Learned from earlier years", v.oos?.learned)}</tbody></table>
        <p class="note">Learned weights come only from data before the hold-out window, so this is a fair comparison. If they win here, apply them on the Weights tab.</p>
        <h3 style="margin-top:14px">Caveats</h3><ul class="caveats">${(b.caveats || []).map(c => `<li>${esc(c)}</li>`).join("")}</ul></div>
    </div>`;
  $$("#btTabs button").forEach(x => x.onclick = () => { state.btH = x.dataset.h; renderBacktest(); });
  const q = v.quantile_excess;
  Plotly.newPlot("bQ", [{ x: q.map((_, i) => `Q${i + 1}${i === 0 ? " (lowest)" : i === q.length - 1 ? " (highest)" : ""}`), y: q.map(x => x * 100), type: "bar",
    marker: { color: q.map(x => x >= 0 ? css("--bull") : css("--bear")) }, hovertemplate: "%{y:.2f}%<extra></extra>" }],
    plotLayout({ showlegend: false, yaxis: { ticksuffix: "%", gridcolor: css("--line"), zeroline: true, zerolinecolor: css("--faint") } }), PCFG);
  Plotly.newPlot("bCurve", [{ x: v.top_curve.dates, y: v.top_curve.values, type: "scatter", mode: "lines", line: { color: css("--bull"), width: 1.8 }, name: "Top quintile" }],
    plotLayout({ showlegend: false }), PCFG);
  Plotly.newPlot("bIC", [{ x: v.ic_series.dates, y: v.ic_series.values, type: "scatter", mode: "lines", line: { color: css("--ink"), width: 1.5 }, name: "IC" }],
    plotLayout({ showlegend: false, yaxis: { gridcolor: css("--line"), zeroline: true, zerolinecolor: css("--faint") } }), PCFG);
}

/* ---------------- weights view ---------------- */
async function loadWeights() {
  state.weights = await api("/api/weights");
  renderWeights(state.weights.weights);
}
function renderWeights(w) {
  const W = state.weights, H = W.horizons;
  const head = `<thead><tr><th>Feature</th>${HKEYS.map(h => `<th>${esc(H[h].label)}</th>`).join("")}</tr></thead>`;
  const rows = Object.entries(W.features).map(([f, desc]) => `<tr><td class="f"><b>${esc(f)}</b><small>${esc(desc)}</small></td>${HKEYS.map(h => {
    const ic = W.feature_ic?.[h]?.[f];
    return `<td><input type="number" step="0.05" min="-1" max="1" data-h="${h}" data-f="${f}" value="${w[h]?.[f] ?? 0}" aria-label="${esc(f)} weight, ${esc(H[h].label)}">
      ${ic == null ? "" : `<span class="ic">backtest IC <span class="${cls(ic) === "p" ? "up" : "down"}">${signed(ic, 3)}</span></span>`}</td>`;
  }).join("")}</tr>`).join("");
  $("#wTable").innerHTML = head + `<tbody>${rows}</tbody>`;
  if (STATIC) $$("#wTable input").forEach(i => i.disabled = true);
}
function readWeights() {
  const w = { short: {}, medium: {}, long: {} };
  $$("#wTable input").forEach(i => { const v = parseFloat(i.value); if (v && isFinite(v)) w[i.dataset.h][i.dataset.f] = v; });
  return w;
}
$("#wSave").onclick = async () => {
  try {
    await api("/api/weights", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ weights: readWeights() }) });
    toast("Weights saved and stocks re-scored"); await loadWeights(); await loadRanking();
  } catch (e) { toast(e.message); }
};
$("#wRevert").onclick = () => renderWeights(state.weights.weights);
$("#wLearned").onclick = () => {
  const L = state.weights.learned || {};
  if (!Object.keys(L).length || !Object.values(L).some(x => Object.keys(x).length)) { toast("Run a backtest first to learn weights"); return; }
  const w = JSON.parse(JSON.stringify(state.weights.weights));
  for (const h of HKEYS) if (L[h] && Object.keys(L[h]).length) w[h] = L[h];
  renderWeights(w); toast("Learned weights loaded. Review, then save");
};

/* ---------------- tabs + boot ---------------- */
$$(".tab").forEach(t => t.onclick = () => {
  $$(".tab").forEach(x => x.setAttribute("aria-selected", x === t));
  $$(".view").forEach(v => v.hidden = v.id !== "view-" + t.dataset.view);
  if (t.dataset.view === "backtest") loadBacktest();
  if (t.dataset.view === "weights") loadWeights();
});

(async function boot() {
  if (STATIC) {
    ["#refreshBtn", "#btRun", ".wt-actions"].forEach(sel => { const el = $(sel); if (el) el.hidden = true; });
    $$("#wTable").forEach(t => t.classList.add("readonly"));
    const note = document.createElement("div");
    note.className = "demo-note";
    note.innerHTML = `Read-only demo, updated automatically after each U.S. market close. Scores rank S&amp;P 500 stocks against each other; for information only, not investment advice. <a href="https://github.com/paudelajib/call-signal-scanner2" target="_blank" rel="noopener">Source &amp; methodology</a>`;
    document.querySelector("main").prepend(note);
  }
  try {
    await loadMeta();
    await loadRanking();
    loadWeights().catch(() => {});
    pollJobs();
  } catch (e) {
    if (STATIC) { $("#rankingBody").innerHTML = `<div class="empty"><h2>Data is being updated</h2><p>${esc(e.message)}. Please try again in a few minutes.</p></div>`; return; }
    $("#rankingBody").innerHTML = `<div class="empty"><h2>Can't reach the scanner server</h2><p>${esc(e.message)}. Start it with <b>python app.py</b> and reload this page.</p></div>`;
  }
})();
