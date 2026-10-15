// CRYPTO panel. All numbers arrive from the server already computed;
// this file only displays them. Data reaches the DOM only via textContent.

const REFRESH_MS = 60_000;
const MSK_MS = 3 * 3600_000;
const SVG_NS = "http://www.w3.org/2000/svg";

const VIEWS = [
  { id: "overview", title: "Overview", icon: "i-overview", tab: true },
  { id: "bots", title: "Bots", icon: "i-bots", tab: true },
  { id: "paths", title: "Trade path", icon: "i-paths", tab: true },
  { id: "positions", title: "Positions", icon: "i-positions" },
  { id: "trades", title: "Trades", icon: "i-trades", tab: true },
  { id: "risk", title: "Risk", icon: "i-risk" },
  { id: "journal", title: "Decisions", icon: "i-journal" },
  { id: "runs", title: "Runs", icon: "i-runs" },
  { id: "strategy", title: "Strategy", icon: "i-strategy" },
];

const BOOK_NAMES = {
  "setup_e:paper": "Setup E · paper",
  "setup_e:testnet": "Setup E · testnet",
  "setup_d:paper": "Setup D · paper",
  "setup_d:testnet": "Setup D · testnet",
};

// [name, tone, icon]
const ACTIONS = {
  OPENED: ["Position opened", "good", "↗"],
  WOULD_OPEN: ["Would open (preview)", "", "↗"],
  CLOSED: ["Position closed", "", "✓"],
  SKIP: ["Signal skipped", "", "·"],
  HALT: ["Trading halted", "bad", "!"],
  NO_ENTRIES: ["No entries", "", "·"],
  STOP_PLACED: ["Stop restored", "bad", "!"],
  STOP_REPAIR_FAILED: ["Stop not placed", "bad", "!"],
  STOP_REPAIR_ERROR: ["Stop error", "bad", "!"],
  EMERGENCY_CLOSE: ["Emergency close", "bad", "!"],
  ENTRY_FAILED: ["Entry failed", "bad", "!"],
  CLOSE_FAILED: ["Close failed", "bad", "!"],
  CLOSE_UNPRICED: ["No close price", "bad", "!"],
  CANCEL_ORPHAN: ["Orphan order cancelled", "bad", "!"],
  VOID: ["Record voided", "", "·"],
  RESUME: ["Resumed manually", "good", "✓"],
};

const CODES = {
  MAX_POSITIONS: "all slots taken",
  MAX_MARGIN: "margin ceiling",
  CORRELATION: "correlated with an open position",
  DAILY_STOP: "daily loss limit",
  WEEKLY_STOP: "weekly loss limit",
  MODE_OFF: "trading switched off",
  MIN_NOTIONAL: "below the exchange minimum order",
  LIQ_BUFFER: "stop too close to liquidation",
  PRICE_DIVERGENCE: "price moved more than 3% from the close",
  NO_PRICE: "no fresh price",
  NO_INSTRUMENT: "instrument not trading",
  STALE_SIGNAL: "signal on an old bar",
  STALE_DATA: "stale data",
  SHORT_HISTORY: "short history",
  ENTRY_WINDOW_MISSED: "run after the entry window",
  TIME_STOP: "time exit",
  STOP: "stop hit",
  EMERGENCY: "emergency",
  RISK_NOT_APPROVED: "no Risk Manager approval",
  RISK_MISMATCH: "plan did not match the approval",
  SIGNAL_MISMATCH: "signals diverged from the Analyst's",
  DECISION_DONE: "day's decision already made",
  HALTED: "halted by reconciliation",
  EXCHANGE_ERROR: "exchange error",
  INSUFFICIENT_BALANCE: "insufficient balance",
  QTY_GRANULARITY: "quantity is not a multiple of the lot step",
};

const FILTERS = [
  ["all", "All", () => true],
  ["trades", "Trades", (e) => ["OPENED", "CLOSED", "EMERGENCY_CLOSE", "WOULD_OPEN"].includes(e.action)],
  ["skips", "Skips", (e) => e.action === "SKIP"],
  ["problems", "Failures", (e) => (ACTIONS[e.action] || [])[1] === "bad"],
];

const state = {
  book: localGet("book") || "setup_e:paper",
  view: "overview",
  filter: "all",
  query: "",
  shown: 150,
  data: null,
  loadedAt: null,
  pathMoment: null,
  pathFilter: "all",
};

// ── utilities ──────────────────────────────────────────────
function localGet(key) { try { return localStorage.getItem(key); } catch { return null; } }
function localSet(key, v) { try { localStorage.setItem(key, v); } catch { /* private mode */ } }
const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") node.className = v; else node.setAttribute(k, v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}
function svg(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}
function icon(id) {
  const s = svg("svg", { class: "icon", "aria-hidden": "true" });
  const use = svg("use");
  use.setAttribute("href", `#${id}`);
  s.append(use);
  return s;
}

const isNum = (v) => v !== null && v !== undefined && !Number.isNaN(v);
const money = (v, sign = false) => !isNum(v) ? "—"
  : (sign && v > 0 ? "+" : v < 0 ? "−" : "") + "$" + Math.abs(v).toFixed(2);
const pct = (v, d = 1) => (!isNum(v) ? "—" : `${v < 0 ? "−" : ""}${Math.abs(v).toFixed(d)}%`);
const rfmt = (v) => (!isNum(v) ? "—" : `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(2)}R`);
function price(v) {
  if (!isNum(v)) return "—";
  const digits = v >= 1000 ? 2 : v >= 1 ? 4 : v >= 0.01 ? 5 : 8;
  return v.toLocaleString("ru-RU", { maximumFractionDigits: digits });
}
const tone = (v) => (v > 0 ? "pos-txt" : v < 0 ? "neg" : "");

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const mskDate = (iso) => new Date(new Date(iso).getTime() + MSK_MS);
function hm(iso) {
  const d = mskDate(iso);
  return `${String(d.getUTCHours()).padStart(2, "0")}:${String(d.getUTCMinutes()).padStart(2, "0")}`;
}
function msk(iso, withDate = true) {
  if (!iso) return "—";
  const d = mskDate(iso);
  return withDate ? `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}, ${hm(iso)} MSK` : `${hm(iso)} MSK`;
}
function dayLabel(iso) {
  const d = mskDate(iso), today = mskDate(new Date().toISOString());
  const key = (x) => `${x.getUTCFullYear()}-${x.getUTCMonth()}-${x.getUTCDate()}`;
  const yest = new Date(today.getTime() - 86400_000);
  if (key(d) === key(today)) return "Today";
  if (key(d) === key(yest)) return "Yesterday";
  return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`;
}
const shortDate = (iso) => { const d = mskDate(iso); return `${d.getUTCDate()} ${MONTHS[d.getUTCMonth()]}`; };
function ago(iso) {
  if (!iso) return "";
  const min = Math.round((Date.now() - new Date(iso).getTime()) / 60_000);
  if (min < 1) return "just now";
  if (min < 60) return `${min} min ago`;
  const h = Math.round(min / 60);
  return h < 48 ? `${h} h ago` : `${Math.round(h / 24)} d ago`;
}
function until(iso) {
  const h = (new Date(iso).getTime() - Date.now()) / 3600_000;
  if (h <= 0) return "today";
  return h < 24 ? `in ${Math.ceil(h)} h` : `in ${Math.ceil(h / 24)} d`;
}

async function api(path) {
  const sep = path.includes("?") ? "&" : "?";
  const res = await fetch(`${path}${sep}book=${encodeURIComponent(state.book)}`);
  if (!res.ok) {
    let detail = `error ${res.status}`;
    try { detail = (await res.json()).detail || detail; } catch { /* not json */ }
    throw new Error(detail);
  }
  return res.json();
}

// ── tooltip ────────────────────────────────────────────────
const tip = $("tooltip");
function showTip(x, y, value, label, color) {
  tip.replaceChildren(
    el("div", { class: "tv" }, color ? el("span", { class: "key", style: `background:${color}` }) : null, value),
    el("div", { class: "tl" }, label),
  );
  tip.hidden = false;
  const w = tip.offsetWidth, h = tip.offsetHeight;
  tip.style.left = `${Math.max(8, Math.min(window.innerWidth - w - 8, x + 14))}px`;
  tip.style.top = `${y - h - 12 < 8 ? y + 16 : y - h - 12}px`;
}
const hideTip = () => { tip.hidden = true; };

// ── navigation ─────────────────────────────────────────────
function renderNav() {
  const d = state.data;
  const badge = (id) => {
    if (!d) return null;
    if (id === "positions" && d.positions.length) return el("span", { class: "badge" }, d.positions.length);
    if (id === "trades" && d.trades.length) return el("span", { class: "badge" }, d.trades.length);
    if (id === "journal") {
      const bad = d.journal.filter(FILTERS[3][2]).length;
      if (bad) return el("span", { class: "badge alert", title: "failures" }, bad);
    }
    if (id === "risk" && d.summary.started && d.summary.mode !== "ON") return el("span", { class: "badge alert" }, "!");
    if (id === "bots") {
      const bad = d.bots.bots.filter((b) => b.latest && b.latest.status === "failed").length;
      if (bad) return el("span", { class: "badge alert", title: "bot failure" }, bad);
    }
    return null;
  };
  const link = (v, cls) => el("a", { href: `#${v.id}`, class: cls, "aria-current": v.id === state.view ? "page" : null },
    icon(v.icon), el("span", {}, v.title), cls ? null : badge(v.id));
  $("nav").replaceChildren(...VIEWS.map((v) => link(v)));

  const tabs = VIEWS.filter((v) => v.tab);
  const extra = VIEWS.filter((v) => !v.tab);
  const moreActive = state.view === "more" || extra.some((v) => v.id === state.view);
  $("tabbar").replaceChildren(
    ...tabs.map((v) => link(v, "tab")),
    el("a", { href: "#more", class: "tab", "aria-current": moreActive ? "page" : null }, icon("i-strategy"), el("span", {}, "More")),
  );
  $("more-list").replaceChildren(...extra.map((v) => el("a", { href: `#${v.id}` }, icon(v.icon), v.title)));
}

function route() {
  const id = location.hash.slice(1);
  state.view = [...VIEWS.map((v) => v.id), "more"].includes(id) ? id : "overview";
  for (const sec of document.querySelectorAll(".view")) sec.hidden = sec.dataset.view !== state.view;
  const title = state.view === "more" ? "More" : VIEWS.find((v) => v.id === state.view).title;
  document.title = `${title} — CRYPTO`;
  renderNav();
  renderCharts();  // charts measure their width, so only the visible section draws
  window.scrollTo(0, 0);
}

// ── shared pieces ──────────────────────────────────────────
function tile(label, value, foot, meter) {
  return el("article", { class: "tile card" },
    el("div", { class: "tile-label" }, label),
    el("div", { class: "tile-value" }, value),
    meter ? el("div", { class: "meter", "aria-hidden": "true" },
      el("span", { class: meter.cls || "", style: `width:${Math.max(0, Math.min(1, meter.ratio)) * 100}%` })) : null,
    el("div", { class: "tile-foot" }, foot));
}
const severity = (r) => (r >= 1 ? "critical" : r >= 0.6 ? "warn" : "");
const side = (s) => el("span", { class: `side ${s}` }, s === "long" ? "Long" : "Short");
const signed = (v, text) => el("span", { class: tone(v) }, text);
function empty(title, text) { return el("div", { class: "empty" }, el("div", {}, el("strong", {}, title), text)); }

function table(node, headers, rows, emptyText) {
  const head = el("tr", {}, ...headers.map(([label, num]) => el("th", { class: num ? "num" : "" }, label)));
  const body = rows.length ? rows : [el("tr", { class: "empty-row" }, el("td", { colspan: headers.length }, emptyText))];
  node.replaceChildren(el("thead", {}, head), el("tbody", {}, ...body));
}

// ── header: status, books, banner ──────────────────────────
function renderChrome(books, s) {
  const buttons = () => books.map((b) => {
    // The menu is narrow: if all books share one setup, only the venue
    // differs, so show just that. Otherwise the setup is needed too.
    const [setup, venue] = b.split(":");
    const venueName = venue === "paper" ? "Paper" : venue === "testnet" ? "Testnet" : venue;
    const oneSetup = new Set(books.map((x) => x.split(":")[0])).size === 1;
    const short = oneSetup ? venueName : `${setup.replace("setup_", "").toUpperCase()} · ${venueName}`;
    const btn = el("button", { role: "tab", "aria-selected": String(b === state.book), title: BOOK_NAMES[b] || b }, short);
    btn.addEventListener("click", () => { state.book = b; state.pathMoment = null; localSet("book", b); refresh(); });
    return btn;
  });
  $("books").hidden = books.length < 2;
  $("books").replaceChildren(...buttons());
  $("more-books-card").hidden = books.length < 2;
  $("more-books").replaceChildren(...buttons());
  const name = BOOK_NAMES[state.book] || state.book;
  $("book-title").textContent = name;
  $("overview-kicker").textContent = name;

  const on = s.mode === "ON";
  const label = !s.started ? "Not started yet" : on ? "Trading on"
    : s.mode === "OFF_DAILY" ? "Paused until end of day" : s.mode === "OFF_WEEKLY" ? "Weekly limit" : "Needs a human";
  $("status").className = `status ${!s.started ? "idle" : on ? "on" : "off"}`;
  $("status").replaceChildren(el("span", { class: "dot", "aria-hidden": "true" }), (on || !s.started ? "" : "⚠ ") + label);

  const banner = $("banner");
  banner.hidden = true;
  if (s.started && !on) {
    banner.className = "banner";
    banner.textContent = s.mode === "OFF_DAILY"
      ? "⚠ Daily loss limit reached. No entries until 03:00 MSK; stops and time exits still work."
      : "⚠ Trading is off. Investigate the cause under “Decisions”, then: python -m src.cli trade-resume --reason '…'";
    banner.hidden = false;
  } else if (!s.started) {
    banner.className = "banner info";
    banner.textContent = `The book is still empty. First run: ${msk(s.next_run)}.`;
    banner.hidden = false;
  }
}

// ── Overview ───────────────────────────────────────────────
function renderOverview() {
  const { summary: s, positions } = state.data;
  $("equity").textContent = money(s.equity);
  const delta = s.equity - s.initial, deltaPct = (delta / s.initial) * 100;
  $("equity-delta").replaceChildren(
    el("span", { class: delta > 0 ? "up" : delta < 0 ? "down" : "" },
      `${delta > 0 ? "▲ " : delta < 0 ? "▼ " : ""}${money(delta, true)} · ${pct(deltaPct, 2)}`),
    ` from the starting ${money(s.initial)}`);
  const fact = (k, v) => el("div", {}, el("dt", {}, k), el("dd", {}, v));
  $("hero-facts").replaceChildren(
    fact("Last run", s.last_run ? `${msk(s.last_run)}` : "none yet"),
    fact("Next run", msk(s.next_run)),
    fact("Entry window", `${hm(s.next_run)}–${msk(s.entry_window_end, false)}`),
    fact("Prices up to", s.data_1h_until ? msk(s.data_1h_until) : "—"));

  const S = s.stats, L = s.limits;
  $("overview-tiles").replaceChildren(
    tile("Open positions", `${s.open_positions} of ${L.max_positions}`,
      `at risk ${money(s.risk_open)} · margin ${money(s.margin_used)}`),
    tile("Forward sample", `${S.closed} / ${S.forward_target}`,
      S.closed >= S.forward_target ? "thresholds can be checked" : "trades before the threshold check",
      { ratio: S.closed / S.forward_target }),
    tile("Average result", rfmt(S.avg_r), S.closed ? `${S.wins} of ${S.closed} winning · threshold +0.15R` : "acceptance threshold +0.15R"),
    tile("Closed P&L", money(S.total_pnl, true), S.closed ? `drawdown from peak ${pct(S.max_drawdown_pct)}` : "no trades yet"));

  const box = $("overview-positions");
  box.replaceChildren(...(positions.length ? positions.map((p) => positionCard(p)) :
    [empty("No positions", `New entries every night ${hm(s.next_run)}–${msk(s.entry_window_end, false)}.`)]));
}

function positionCard(p) {
  const opened = new Date(p.opened_at).getTime(), due = new Date(p.due_at).getTime();
  const held = Math.max(0, Math.min(1, (Date.now() - opened) / (due - opened)));
  const day = Math.min(7, Math.max(1, Math.ceil((Date.now() - opened) / 86400_000)));
  const near = isNum(p.to_stop_pct) && p.to_stop_pct < 3;
  return el("article", { class: "pos" },
    el("div", { class: "pos-top" },
      el("span", { class: "pos-sym" }, p.symbol),
      el("span", { class: `pos-pnl ${tone(p.unrealized)}` }, isNum(p.unrealized) ? money(p.unrealized, true) : "—")),
    el("div", { class: "pos-meta" }, side(p.side),
      p.status === "pending" ? "entry not confirmed" : `day ${day} of 7 · closes ${until(p.due_at)}`),
    el("div", { class: "track", role: "img", "aria-label": `${Math.round(held * 100)}% of the holding period elapsed` },
      el("span", { class: "fill", style: `left:0;width:${held * 100}%` })),
    el("dl", { class: "pos-grid" },
      el("div", {}, el("dt", {}, "Entry"), el("dd", {}, price(p.entry))),
      el("div", {}, el("dt", {}, "Price"), el("dd", {}, price(p.price))),
      el("div", {}, el("dt", {}, "Stop"), el("dd", {}, price(p.stop))),
      el("div", {}, el("dt", {}, "To stop"), el("dd", { class: near ? "neg" : "" },
        !isNum(p.to_stop_pct) ? "—" : p.to_stop_pct < 0 ? "⚠ crossed" : pct(p.to_stop_pct))),
      el("div", {}, el("dt", {}, "Position"), el("dd", {}, money(p.notional))),
      el("div", {}, el("dt", {}, "Risk"), el("dd", {}, money(p.risk)))));
}

// ── Positions ──────────────────────────────────────────────
function renderPositions() {
  const { positions, summary: s } = state.data;
  $("positions-cards").replaceChildren(...(positions.length ? positions.map(positionCard)
    : [empty("No open positions", `Next run: ${msk(s.next_run)}.`)]));
  table($("positions"), [
    ["Symbol"], ["Side"], ["Entry", 1], ["Price", 1], ["Stop", 1], ["To stop", 1],
    ["Position", 1], ["Risk", 1], ["P&L", 1], ["Closes", 1],
  ], positions.map((p) => el("tr", {},
    el("td", { class: "sym" }, p.symbol), el("td", {}, side(p.side)),
    el("td", { class: "num" }, price(p.entry)), el("td", { class: "num" }, price(p.price)),
    el("td", { class: "num" }, price(p.stop)),
    el("td", { class: "num" }, !isNum(p.to_stop_pct) ? "—" : p.to_stop_pct < 0
      ? el("span", { class: "neg" }, "⚠ crossed") : pct(p.to_stop_pct)),
    el("td", { class: "num" }, money(p.notional)), el("td", { class: "num" }, money(p.risk)),
    el("td", { class: "num" }, isNum(p.unrealized) ? signed(p.unrealized, money(p.unrealized, true)) : "—"),
    el("td", { class: "num" }, p.status === "pending" ? "not confirmed" : until(p.due_at)))),
  "No open positions.");
}

// ── Trades ─────────────────────────────────────────────────
function renderTrades() {
  const { trades, summary: s } = state.data;
  const S = s.stats;
  $("trades-tiles").replaceChildren(
    tile("Closed trades", String(S.closed), `${S.wins} winning${S.closed ? ` · ${pct(S.win_rate, 0)}` : ""}`),
    tile("Average result", rfmt(S.avg_r), "acceptance threshold +0.15R"),
    tile("Best trade", S.closed ? money(S.best, true) : "—", S.closed ? `worst ${money(S.worst, true)}` : "no trades yet"),
    tile("Drawdown from peak", pct(S.max_drawdown_pct), "on closed trades"));
  table($("trades"), [
    ["Closed"], ["Symbol"], ["Side"], ["Entry", 1], ["Exit", 1], ["Reason"], ["R", 1], ["P&L", 1],
  ], trades.map((t) => el("tr", {},
    el("td", {}, msk(t.closed_at)), el("td", { class: "sym" }, t.symbol), el("td", {}, side(t.side)),
    el("td", { class: "num" }, price(t.entry)), el("td", { class: "num" }, price(t.exit)),
    el("td", {}, CODES[t.reason] || t.reason || "—"),
    el("td", { class: "num" }, signed(t.r, rfmt(t.r))),
    el("td", { class: "num" }, signed(t.pnl, money(t.pnl, true))))),
  "No closed trades yet. The first one closes in 7 days at the earliest, or on its stop.");
}

// ── Risk ───────────────────────────────────────────────────
function renderRisk() {
  const s = state.data.summary, L = s.limits;
  const on = s.mode === "ON" || !s.started;
  const card = $("mode-card");
  card.className = `card mode-card ${on ? "" : "off"}`;
  const text = !s.started ? "The book is created on the first night run."
    : on ? "Limits not reached. New entries are allowed in the 03:05–05:00 MSK window."
    : s.mode === "OFF_DAILY" ? "Daily limit reached. Entries resume on their own at 03:00 MSK."
    : "Entries halted until resumed manually: python -m src.cli trade-resume --reason '…'";
  card.replaceChildren(el("div", { class: "mode-icon" }, icon("i-risk")),
    el("div", {}, el("div", { class: "mode-title" }, on ? "Trading allowed" : "Entries halted"),
      el("div", { class: "mode-text" }, text)));

  const limit = (name, value, max, note) => {
    const ratio = Math.max(0, value) / max;
    return el("div", { class: "limit" },
      el("div", { class: "limit-row" }, el("span", { class: "limit-name" }, name),
        el("span", { class: "limit-val" }, `${pct(Math.max(0, value))} of ${max}%`)),
      el("div", { class: "meter", "aria-hidden": "true" },
        el("span", { class: severity(ratio), style: `width:${Math.min(1, ratio) * 100}%` })),
      el("div", { class: "limit-note" }, note));
  };
  $("loss-card").replaceChildren(
    el("div", { class: "card-head" }, el("div", {}, el("h2", {}, "Loss"), el("p", { class: "sub" }, "Relative to equity at the start of the day and week"))),
    limit("Day", s.day_loss_pct, L.daily_loss_pct, "if reached, no entries until 03:00 MSK"),
    limit("Week", s.week_loss_pct, L.weekly_loss_pct, "if reached, trading resumes only manually"));

  const marginPct = s.equity > 0 ? (s.margin_used / s.equity) * 100 : 0;
  $("exposure-card").replaceChildren(
    el("div", { class: "card-head" }, el("div", {}, el("h2", {}, "Account usage"), el("p", { class: "sub" }, "What is in use right now"))),
    el("div", { class: "limit" },
      el("div", { class: "limit-row" }, el("span", { class: "limit-name" }, "Positions"),
        el("span", { class: "limit-val" }, `${s.open_positions} of ${L.max_positions}`)),
      el("div", { class: "meter" }, el("span", { style: `width:${(s.open_positions / L.max_positions) * 100}%` }))),
    el("div", { class: "limit" },
      el("div", { class: "limit-row" }, el("span", { class: "limit-name" }, "Margin"),
        el("span", { class: "limit-val" }, `${pct(marginPct)} of ${L.max_total_margin_pct}%`)),
      el("div", { class: "meter" }, el("span", { class: severity(marginPct / L.max_total_margin_pct), style: `width:${Math.min(1, marginPct / L.max_total_margin_pct) * 100}%` }))),
    el("div", { class: "limit" },
      el("div", { class: "limit-row" }, el("span", { class: "limit-name" }, "At risk to stops"),
        el("span", { class: "limit-val" }, money(s.risk_open))),
      el("div", { class: "limit-note" }, "the loss if every stop is hit")));

  const spec = (k, v) => el("div", {}, el("dt", {}, k), el("dd", {}, v));
  const riskUsd = (s.equity * L.risk_per_trade_pct) / 100;
  $("risk-rules").replaceChildren(
    spec("Risk per trade", `${L.risk_per_trade_pct}% ≈ ${money(riskUsd)}`),
    spec("Position size", `$5 – ${money((s.equity * L.max_position_pct) / 100)} (up to ${L.max_position_pct}%)`),
    spec("Margin per trade", `${L.margin_per_trade_pct}%`),
    spec("Concurrent positions", `up to ${L.max_positions}`),
    spec("Stop", "on the exchange right after entry"),
    spec("Exit", "after 7 days or on stop"));
}

// ── Decisions ──────────────────────────────────────────────
function renderFilters() {
  const all = state.data.journal;
  $("journal-filter").replaceChildren(...FILTERS.map(([key, label, pass]) => {
    const b = el("button", { role: "tab", "aria-selected": String(state.filter === key) },
      label, el("span", { class: "count" }, all.filter(pass).length));
    b.addEventListener("click", () => { state.filter = key; state.shown = 150; renderFilters(); renderJournal(); });
    return b;
  }));
}

function renderJournal() {
  const pass = FILTERS.find(([k]) => k === state.filter)[2];
  const q = state.query.trim().toLowerCase();
  const rows = state.data.journal.filter(pass).filter((e) => !q || [e.symbol, e.code, CODES[e.code], e.note,
    (ACTIONS[e.action] || [])[0]].filter(Boolean).join(" ").toLowerCase().includes(q));
  const box = $("journal");
  if (!rows.length) {
    box.replaceChildren(empty("No entries", q ? "Nothing matches the query." : "The journal fills up after the first run."));
    return;
  }
  const out = [];
  let lastDay = null;
  for (const e of rows.slice(0, state.shown)) {
    const day = dayLabel(e.ts);
    if (day !== lastDay) { out.push(el("div", { class: "day" }, day)); lastDay = day; }
    const [name, t, mark] = ACTIONS[e.action] || [e.action, "", "·"];
    out.push(el("div", { class: "entry" },
      el("span", { class: "when" }, hm(e.ts)),
      el("span", { class: `mark ${t}`, "aria-hidden": "true" }, mark),
      el("div", {},
        el("div", { class: "title" }, name, e.symbol ? el("span", { class: "sym" }, e.symbol) : null),
        el("div", { class: "note" }, [CODES[e.code] || e.code, e.note].filter(Boolean).join(" — ")))));
  }
  if (rows.length > state.shown) {
    const more = el("button", { class: "more" }, `Show more (${rows.length - state.shown})`);
    more.addEventListener("click", () => { state.shown += 300; renderJournal(); });
    out.push(more);
  }
  box.replaceChildren(...out);
}

// ── Runs ───────────────────────────────────────────────────
function inline(text) {
  const frag = document.createDocumentFragment();
  text.split(/(\*\*[^*]+\*\*|`[^`]+`)/).forEach((part) => {
    if (part.startsWith("**") && part.endsWith("**")) frag.append(el("strong", {}, part.slice(2, -2)));
    else if (part.startsWith("`") && part.endsWith("`")) frag.append(el("code", {}, part.slice(1, -1)));
    else if (part) frag.append(document.createTextNode(part));
  });
  return frag;
}

function renderRuns() {
  const { health: h, runlog, summary: s } = state.data;
  const fresh = (iso, hours) => iso && Date.now() - new Date(iso).getTime() < hours * 3600_000;
  const check = (label, value, note, level) => el("article", { class: "card check" },
    el("div", { class: "check-top" }, el("span", { class: `dot ${level}` }), label),
    el("div", { class: "check-val" }, value), el("div", { class: "check-note" }, note));
  $("health").replaceChildren(
    check("Scheduler", h.scheduler.installed ? "Installed" : "Not installed",
      h.scheduler.last_done_day ? `last full cycle: ${h.scheduler.last_done_day.replace(/(\d{4})(\d\d)(\d\d)/, "$3.$2")}` : "no full cycles yet",
      h.scheduler.installed ? "" : "bad"),
    check("Prices (1h)", h.data["1h"] ? msk(h.data["1h"]) : "none", fresh(h.data["1h"], 3) ? "fresh" : "refreshed on the next run",
      fresh(h.data["1h"], 26) ? "" : "warn"),
    check("Monthly universe", `${h.universe.symbols} symbols`, h.universe.month,
      h.universe.symbols ? "" : "bad"),
    check("Next run", msk(s.next_run), `entry window until ${msk(s.entry_window_end, false)}`, ""),
    check("Instrument parameters", h.instruments_updated ? msk(h.instruments_updated) : "none",
      "price and lot steps; refreshed by the nightly collection", fresh(h.instruments_updated, 50) ? "" : "warn"));

  $("runlog-path").textContent = `${runlog.path} — the same text as in the file, newest first`;
  const box = $("runlog");
  if (!runlog.blocks.length) { box.replaceChildren(empty("No runs yet", "")); return; }
  box.replaceChildren(...runlog.blocks.map((block) => {
    const lines = block.split("\n").filter((l) => l.trim());
    const title = lines.shift().replace(/^##\s*/, "");
    const card = el("article", { class: `run ${/NOT EXECUTED/.test(title) ? "failed" : ""}` }, el("h3", {}, title));
    let ul = null;
    for (const line of lines) {
      if (line.startsWith("- ")) {
        if (!ul) { ul = el("ul"); card.append(ul); }
        ul.append(el("li", {}, inline(line.slice(2))));
      } else { ul = null; card.append(el("p", {}, inline(line))); }
    }
    return card;
  }));
}

// ── Strategy ───────────────────────────────────────────────
function renderStrategy() {
  const st = state.data.strategy, P = st.params, R = st.risk, B = st.backtest, F = st.forward;
  if (!isNum(P.lookback_days)) {
    $("strategy-about").replaceChildren(el("h2", {}, "Parameters not found"),
      el("p", {}, `Book ${state.book} has no setup section in config/settings.yaml.`));
    $("strategy-params").replaceChildren();
    $("forward-card").replaceChildren();
    table($("acceptance"), [["Condition"]], [], "—");
    return;
  }
  $("strategy-about").replaceChildren(
    el("h2", {}, `Setup ${st.setup.replace("setup_", "").toUpperCase()} — momentum on daily bars`),
    el("p", {}, `Hypothesis: the price direction over the last ${P.lookback_days} days continues for another week.`),
    el("ol", {},
      el("li", {}, `Every night at 00:00 UTC (03:00 MSK) the return over ${P.lookback_days} days is computed for each coin in the universe.`),
      el("li", {}, "Up — buy (long), down — sell (short)."),
      el("li", {}, `Stop: ${P.stop_atr_mult} × ATR(${P.stop_atr_period}) from the entry price, no further than ${P.max_stop_pct}%. Placed on the exchange immediately.`),
      el("li", {}, `The position closes after ${P.hold_days} days or on stop. There is no target.`),
      el("li", {}, `Sized so that the stop costs ${R.risk_per_trade_pct}% of the deposit; at most ${R.max_position_pct}% of the deposit per position and at most ${R.max_positions} positions.`)),
    el("p", {}, `Backtest ${B.window}: ${B.trades.toLocaleString("ru-RU")} signals, a portfolio with ${B.slots} slots took ${B.taken_by_portfolio}; $${B.equity_from} → $${B.equity_to}. `
      + `The signal is real (better than a random direction by 2.63 SE), but the average result of +0.024R is below the preregistered threshold of +0.15R. So the strategy is ${B.verdict}, and a forward test on paper is now under way.`));

  const spec = (k, v) => el("div", {}, el("dt", {}, k), el("dd", {}, v));
  $("strategy-params").replaceChildren(
    spec("Return window", `${P.lookback_days} days`), spec("Holding", `${P.hold_days} days`),
    spec("Stop", `${P.stop_atr_mult} × ATR(${P.stop_atr_period})`), spec("Stop cap", `${P.max_stop_pct}%`),
    spec("Deposit", money(R.equity_usd)), spec("Risk per trade", `${R.risk_per_trade_pct}%`),
    spec("Fee", `${R.taker_fee_pct}%`), spec("Slippage", `${R.slippage_pct}%`));

  $("forward-card").replaceChildren(
    el("div", { class: "card-head" }, el("div", {}, el("h2", {}, "Forward test"),
      el("p", { class: "sub" }, "New trades after the rules were frozen"))),
    el("div", { class: "big-progress" }, `${F.closed} / ${F.target}`),
    el("div", { class: "meter" }, el("span", { style: `width:${Math.min(1, F.closed / F.target) * 100}%` })),
    el("p", { class: "sub" }, F.closed >= F.target
      ? "Sample complete — time to check it against the thresholds."
      : `Average result so far: ${rfmt(F.avg_r)}. Real money is decided only once ${F.target} trades have accumulated and the thresholds are passed.${F.pace_weeks ? ` At the backtest pace that is about ${F.pace_weeks} weeks.` : ""}`));

  table($("acceptance"), [["Condition"], ["Required"], ["Backtest"], ["", 1]],
    st.acceptance.map((a) => el("tr", {}, el("td", {}, a.name), el("td", {}, a.need), el("td", {}, a.backtest),
      el("td", { class: "num" }, el("span", { class: a.ok ? "ok-mark" : "bad-mark" }, a.ok ? "✓ passed" : "✕ failed")))), "");
}

// ── Bots ───────────────────────────────────────────────────
const BOT_INFO = {
  collector: ["Collector", "Co", "Downloads 4h and 1h candles and funding for the universe and open positions; builds the universe once a month."],
  analyst: ["Analyst", "A", "Computes the 00:00 UTC signals with the same code as the backtest."],
  risk: ["Risk Manager", "R", "Rehearsal of the decision on a copy of the book: size, limits, correlation; approval or rejection for each."],
  executor: ["Executor", "E", "The only one that trades: exits and stops always, entries only with the Risk Manager's approval."],
  controller: ["Controller", "Ct", "Independently re-checks data, signals, accounting and position protection. Reports only, for now."],
};
const STATUS = { ok: "done", refused: "refused", failed: "failed", skipped: "skipped", running: "running" };
const SHIFT = { night: "night shift", day: "day shift" };
const secs = (v) => (!isNum(v) ? "—" : v < 60 ? `${v.toFixed(v < 10 ? 1 : 0)} s` : `${Math.floor(v / 60)} min ${Math.round(v % 60)} s`);
const codeText = (c) => CODES[c] || c;
const sideRu = (s) => (s === "long" ? "long" : "short");
const countBy = (obj) => {
  const c = {};
  for (const v of Object.values(obj || {})) c[v] = (c[v] || 0) + 1;
  return Object.entries(c).sort((a, b) => b[1] - a[1]);
};
const stepCode = (c) => (c === 0 ? "done" : c === 3 ? "partial" : c === 5 ? "universe due" : `code ${c}`);

// What the bot received, what it did and what it handed on, from its latest run.
function botIO(bot, run) {
  const o = run.outputs || {}, m = run.metrics || {};
  const li = (text, cls) => ({ text, cls });
  if (run.status === "skipped") return [[li(SHIFT[run.shift])], [li(run.summary)], [li("nothing: there was no work")]];
  if (run.status === "failed" && run.error) return [[li(SHIFT[run.shift])], [li(run.summary, "bad")], [li("no result: retried on schedule")]];
  switch (bot) {
    case "collector": {
      const mf = o.manifest || {};
      return [
        run.shift === "day"
          ? [li(`open positions: ${(mf.held || []).join(", ") || "none"}`)]
          : [li(`universe: ${mf.universe ?? "—"} symbols`), li(`open positions: ${(mf.held || []).join(", ") || "none"}`)],
        (o.steps || []).map((st) => li(`${st.step} — ${stepCode(st.code)}, ${secs(st.seconds)}`, [0, 3, 5].includes(st.code) ? "ok" : "bad")),
        [li(`series checked: ${mf.symbols ?? "—"}`),
          li((mf.stale_4h || []).length ? `4h lagging: ${mf.stale_4h.join(", ")}` : "4h fresh", (mf.stale_4h || []).length ? "bad" : "ok"),
          li((mf.stale_1h || []).length ? `1h lagging: ${mf.stale_1h.join(", ")}` : "1h fresh", (mf.stale_1h || []).length ? "bad" : "ok")],
      ];
    }
    case "analyst": {
      const c = o.candidates || [];
      return [
        [li(`universe: ${o.universe ?? m.universe ?? 0} symbols`), li("4h candles up to 00:00 UTC")],
        o.problem ? [li(o.problem, "bad")] : [li(`signals: ${m.candidates ?? c.length}`), li(`no signal: ${m.no_signal ?? 0}`),
          li(`short history: ${m.short_history ?? 0}`), li(`stale data: ${m.stale ?? 0}`, m.stale ? "bad" : null)],
        c.length ? [...c.slice(0, 6).map((r) => li(`${r.queue}. ${r.symbol} ${sideRu(r.side)}, stop ${price(+r.stop)}`)),
          ...(c.length > 6 ? [li(`and ${c.length - 6} more in “Trade path”`)] : [])] : [li("no signals")],
      ];
    }
    case "risk": {
      const rej = countBy(o.rejected);
      if (o.mine) return [[li("Analyst's signals")], [li("recomputed the signals itself: they did not match", "bad")], [li("no approvals: there will be no entries", "bad")]];
      if (!("approved" in o)) return [[li("Analyst's signals")], [li(run.summary, "bad")], [li("no approvals")]];
      return [
        [li(`signals from the Analyst: ${m.candidates ?? 0}`), li("book and exchange as copies, no writes")],
        [li(o.rehearsal ? "full rehearsal of the decision" : "decision preview (testnet)"),
          ...(o.would_close || []).map(([s, r, p]) => li(`would close ${s} ${codeText(r)}: ${money(+p, true)}`)),
          li(`equity after: ${money(+o.equity_after)}`)],
        [li(o.approved.length ? `approved: ${o.approved.join(", ")}` : "no approvals", o.approved.length ? "ok" : null),
          ...rej.map(([c, n]) => li(`${codeText(c)} — ${n}`))],
      ];
    }
    case "executor": {
      const closed = (o.closed || []).map(([s, r, p]) => li(`closed ${s} ${codeText(r)}: ${money(+p, true)}`));
      if (run.shift === "day") {
        return [[li("open positions and hourly bars")],
          [...(o.events || []).map((e) => li(`stop: ${e}`)), ...closed, ...(closed.length ? [] : [li("stops checked: none triggered", "ok")])],
          [li(`equity ${money(+o.equity)}, mode ${o.mode}`)]];
      }
      const skipped = countBy(o.skipped);
      return [
        [li(`approved by the Risk Manager: ${(o.approved || []).length}`), ...(o.note ? [li(o.note)] : [])],
        [...(o.opened || []).map((s) => li(`opened ${s}`, "ok")), ...closed,
          ...skipped.map(([c, n]) => li(`not entered: ${codeText(c)} — ${n}`)),
          ...(!(o.opened || []).length && !closed.length && !skipped.length ? [li("no trades")] : [])],
        [li(`equity ${money(+o.equity)}`), li(`mode ${o.mode}`, o.halted ? "bad" : null)],
      ];
    }
    case "controller":
      return [
        [li("book, exchange, board"), ...(run.shift === "night" ? [li("candles via raw SQL")] : [])],
        (o.checks || []).map((c) => li(`${c.check}: ${c.detail}`, c.ok ? "ok" : "bad")),
        [li(m.failed ? `failed ${m.failed} of ${m.checks}` : `passed ${m.checks ?? 0} of ${m.checks ?? 0}`, m.failed ? "bad" : "ok"),
          li("reports only for now; does not halt trading")],
      ];
    default:
      return [[], [li(run.summary)], []];
  }
}

// Mini-chart series: [label, value, class].
const BOT_SERIES = {
  collector: [["seconds", (r) => r.seconds, "seg-a"]],
  analyst: [["signals", (r) => r.metrics.candidates, "seg-a"], ["stale", (r) => r.metrics.stale, "seg-warn"]],
  risk: [["approved", (r) => r.metrics.approved, "seg-a"], ["rejected", (r) => r.metrics.rejected, "seg-b"]],
  executor: [["opened", (r) => r.metrics.opened, "seg-a"], ["closed", (r) => r.metrics.closed, "seg-b"]],
  controller: [["passed", (r) => (r.metrics.checks || 0) - (r.metrics.failed || 0), "seg-b"], ["failed", (r) => r.metrics.failed, "seg-bad"]],
};

function renderBots() {
  const B = state.data.bots;
  const byBot = Object.fromEntries(B.bots.map((b) => [b.bot, b]));
  const night = B.last_night;
  $("pipe-title").textContent = night ? `Last night: decision of ${shortDate(night)}` : "No night shifts yet";
  $("pipe-sub").textContent = `next night ${msk(B.next.night)} · day shift every hour, next at ${msk(B.next.day, false)}`;
  $("pipeline").replaceChildren(...Object.keys(BOT_INFO).map((bot) => {
    const run = byBot[bot] && byBot[bot].latest_night;
    const current = run && run.moment === night;
    return el("li", {},
      el("div", { class: "pipe-name" }, el("span", { class: `sdot ${current ? run.status : ""}`, "aria-hidden": "true" }), BOT_INFO[bot][0]),
      el("div", { class: "pipe-sum" }, current ? `${STATUS[run.status] || run.status}: ${run.summary}` : "did not run that night"),
      current ? el("div", { class: "pipe-when" }, `${msk(run.started_at, false)} · ${secs(run.seconds)}`) : null);
  }));

  $("bot-windows").replaceChildren(...Object.keys(BOT_INFO).map((bot) => {
    const [name, letter, role] = BOT_INFO[bot];
    const b = byBot[bot] || { runs: [] };
    const run = b.latest;
    const head = el("div", { class: "bot-head" },
      el("div", { class: "bot-ava", "aria-hidden": "true" }, letter),
      el("div", {}, el("div", { class: "bot-title" }, name), el("div", { class: "bot-role" }, role)),
      run ? el("span", { class: `pill ${run.status}` }, el("span", { class: `sdot ${run.status}` }), STATUS[run.status] || run.status) : null);
    if (!run) {
      return el("article", { class: "card bot" }, head,
        empty("Not run yet", `First night shift: ${msk(B.next.night)}.`));
    }
    const next = bot === "analyst" || bot === "risk" ? B.next.night
      : new Date(B.next.day) < new Date(B.next.night) ? B.next.day : B.next.night;
    const fact = (k, v) => el("div", {}, el("dt", {}, k), el("dd", {}, v));
    const [got, did, gave] = botIO(bot, run);
    const io = (title, items) => el("div", { class: "io" }, el("h3", {}, title),
      el("ul", {}, ...items.map((i) => el("li", { class: i.cls || null }, i.text))));
    const nights = b.runs.filter((r) => r.shift === "night");
    const chart = el("div", { class: "chart chart-mini", "data-bot": bot });
    const history = el("details", {},
      el("summary", {}, `Run history (${b.runs.length})`),
      el("div", { class: "table-wrap" }, el("table", { class: "table" },
        el("thead", {}, el("tr", {}, el("th", {}, "When"), el("th", {}, "Shift"), el("th", {}, "Result"), el("th", { class: "num" }, "Duration"))),
        el("tbody", {}, ...b.runs.map((r) => el("tr", {},
          el("td", {}, msk(r.started_at)), el("td", {}, r.shift === "night" ? "night" : "day"),
          el("td", {}, el("span", { class: `sdot ${r.status}`, style: "display:inline-block;margin-right:6px" }), r.summary),
          el("td", { class: "num" }, secs(r.seconds))))))));
    return el("article", { class: "card bot" }, head,
      el("dl", { class: "bot-meta" },
        fact("Last run", el("span", {}, msk(run.started_at), el("span", { class: "shift-tag" }, SHIFT[run.shift]))),
        fact("Duration", secs(run.seconds)),
        fact("Next", msk(next))),
      el("div", { class: "bot-io" }, io("Received", got), io("Did", did), io("Handed on", gave)),
      el("div", {}, el("div", { class: "sub", style: "margin-bottom:6px" },
        `${BOT_SERIES[bot].map((s) => s[0]).join(" / ")} — per night` + (nights.length ? "" : " (appears after the first night)")), chart),
      history);
  }));
}

// Bars per run; the series are stacked.
function stackedBars(box, items, series, labelOf) {
  if (!box.offsetParent) return;
  if (!items.length) { box.replaceChildren(empty("No data yet", "")); return; }
  const W = box.clientWidth, H = box.clientHeight, m = { l: 30, r: 4, t: 6, b: 16 };
  const totals = items.map((it) => series.reduce((a, [, f]) => a + (+f(it) || 0), 0));
  const ticks = niceTicks(0, Math.max(2, ...totals), 2);  // counters: whole-number ticks
  const hi = ticks[ticks.length - 1];
  const Y = (v) => H - m.b - (v / hi) * (H - m.t - m.b);
  const band = (W - m.l - m.r) / Math.max(items.length, 8), bw = Math.max(3, Math.min(22, band - 3));
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": series.map((s) => s[0]).join(", ") });
  const grid = svg("g", { class: "grid" }), axis = svg("g", { class: "axis" });
  for (const v of ticks) {
    grid.append(svg("line", { x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v) }));
    const t = svg("text", { x: m.l - 6, y: Y(v) + 4, "text-anchor": "end" });
    t.textContent = v >= 1000 ? `${(v / 1000).toFixed(1)}k` : String(v);
    axis.append(t);
  }
  root.append(grid, axis);
  const styles = getComputedStyle(box);
  items.forEach((it, i) => {
    const x = m.l + band * i + (band - bw) / 2;
    let acc = 0;
    const parts = [];
    for (const [label, f, cls] of series) {
      const v = +f(it) || 0;
      if (v > 0) {
        const y = Y(acc + v), h = Math.max(1, Y(acc) - Y(acc + v));
        root.append(svg("rect", { class: cls, x, y, width: bw, height: h, rx: Math.min(3, bw / 3) }));
      }
      acc += v;
      parts.push(`${label} ${Number.isInteger(v) ? v : v.toFixed(1)}`);
    }
    const hit = svg("rect", { class: "bar-hit", x: m.l + band * i, y: m.t, width: band, height: H - m.t - m.b, tabindex: 0 });
    const color = styles.getPropertyValue("--series");
    const on = (e) => showTip(e.clientX, e.clientY, parts.join(" · "), labelOf(it), color);
    hit.addEventListener("pointermove", on);
    hit.addEventListener("focus", () => { const b = hit.getBoundingClientRect(); on({ clientX: b.left + b.width / 2, clientY: b.top + 10 }); });
    hit.addEventListener("pointerleave", hideTip);
    hit.addEventListener("blur", hideTip);
    root.append(hit);
  });
  if (items.length) {
    const first = svg("text", { x: m.l, y: H - 2, "text-anchor": "start" });
    first.textContent = shortDate(items[0].moment || items[0].started_at);
    const last = svg("text", { x: m.l + band * (items.length - 1) + band / 2, y: H - 2, "text-anchor": items.length > 1 ? "end" : "start" });
    last.textContent = items.length > 1 ? shortDate(items[items.length - 1].moment || items[items.length - 1].started_at) : "";
    axis.append(first, last);
  }
  box.replaceChildren(root);
}

function renderBotCharts() {
  if (!state.data || !state.data.bots) return;
  for (const b of state.data.bots.bots) {
    const box = document.querySelector(`.chart-mini[data-bot="${b.bot}"]`);
    if (!box) continue;
    const nights = b.runs.filter((r) => r.shift === "night").slice(0, 20).reverse();
    stackedBars(box, nights, BOT_SERIES[b.bot], (r) => `${shortDate(r.moment)} · ${STATUS[r.status] || r.status} · ${secs(r.seconds)}`);
    if (b.bot === "collector") {  // failures in red
      box.querySelectorAll("rect.seg-a").forEach((rect, i) => { if (nights[i] && nights[i].status === "failed") rect.setAttribute("class", "seg-bad"); });
    }
  }
}

// ── Trade path ─────────────────────────────────────────────
const STAGE_RU = { signal: "signal", risk: "risk", execution: "execution", verify: "verification" };
const OUTCOME_FILTERS = [
  ["all", "All", () => true],
  ["entered", "Entered", (r) => ["entered", "flagged"].includes(r.outcome.kind)],
  ["rule", "Rule rejections", (r) => r.outcome.kind === "rule"],
  ["platform", "Platform rejections", (r) => r.outcome.kind === "platform"],
];

function stageChip(s) {
  if (!s) return el("span", { class: "stage none" }, "—");
  if (s.verdict === "pass") {
    const d = s.detail || {};
    const title = d.qty ? `${sideRu(d.side)} ${d.qty} at ${d.entry}, stop ${d.stop}` : d.side ? `${sideRu(d.side)}, stop ${d.stop}` : null;
    return el("span", { class: "stage pass", title }, "✓", d.side ? el("span", { style: "font-weight:500" }, sideRu(d.side)) : null);
  }
  const cls = s.verdict === "fail" ? "fail" : s.origin === "platform" ? "platform" : "";
  return el("span", { class: `stage ${cls}`, title: s.code },
    el("span", { "aria-hidden": "true" }, s.origin === "platform" ? "⚠" : "✕"), el("span", {}, codeText(s.code)));
}

function outcomeText(o) {
  if (o.kind === "entered") return "Entered";
  if (o.kind === "flagged") return `Entered, but Controller: ${codeText(o.code)}`;
  if (o.kind === "pending") return `Stopped at the “${STAGE_RU[o.stage]}” stage`;
  return `Did not pass: ${STAGE_RU[o.stage]} — ${codeText(o.code)}`;
}

function hbars(node, rows, emptyText) {
  if (!rows.length) { node.replaceChildren(empty(emptyText, "")); return; }
  const max = Math.max(1, ...rows.map((r) => r.value));
  node.replaceChildren(...rows.map((r) => el("div", { class: "hbar" },
    el("div", { class: "hbar-label", title: r.title || null }, r.label, r.small ? el("small", {}, r.small) : null),
    el("div", { class: "hbar-track" }, el("span", { class: r.cls || "", style: `width:${(r.value / max) * 100}%` })),
    el("div", { class: "hbar-val" }, r.value))));
}

function renderPaths() {
  const P = state.data.paths, day = P.day;
  $("path-days").replaceChildren(...P.moments.slice(0, 14).map((m) => {
    const b = el("button", { role: "tab", "aria-selected": String(m === P.moment) }, shortDate(m));
    b.addEventListener("click", () => { state.pathMoment = m; refresh(); });
    return b;
  }));
  if (!day) {
    $("path-tiles").replaceChildren();
    $("path-funnel").replaceChildren(empty("The path appears after the bot platform's first night", `Next night: ${msk(state.data.bots.next.night)}.`));
    $("path-codes").replaceChildren();
    table($("path-table"), [["Symbol"]], [], "Empty so far.");
    $("path-closes").replaceChildren();
    return;
  }
  const F = day.funnel, O = day.outcomes;
  $("path-tiles").replaceChildren(
    tile("Signals", String(F.signal), "from the Analyst"),
    tile("Approved", String(F.risk), "by the Risk Manager"),
    tile("Entered", String(O.entered + O.flagged), O.flagged ? `flagged by the Controller: ${O.flagged}` : "opened by the Executor"),
    tile("Rule rejections", String(O.rule), "limits, size, slots: by design"),
    tile("Platform rejections", String(O.platform), O.platform ? "data, window, second key: investigate" : "the platform did not interfere"));
  hbars($("path-funnel"), [
    { label: "Signal", value: F.signal },
    { label: "Approved by risk", value: F.risk },
    { label: "Executed", value: F.execution },
    { label: "Verified", value: F.verify },
  ], "No signals");
  hbars($("path-codes"), day.by_code.map((c) => ({
    label: codeText(c.code), small: STAGE_RU[c.stage], value: c.count, cls: c.origin, title: c.code,
  })), "No rejections");

  const pass = OUTCOME_FILTERS.find(([k]) => k === state.pathFilter)[2];
  $("path-filter").replaceChildren(...OUTCOME_FILTERS.map(([key, label, f]) => {
    const b = el("button", { role: "tab", "aria-selected": String(state.pathFilter === key) }, label,
      el("span", { class: "count" }, day.rows.filter(f).length));
    b.addEventListener("click", () => { state.pathFilter = key; renderPaths(); });
    return b;
  }));
  table($("path-table"), [["Symbol"], ["Signal"], ["Risk"], ["Execution"], ["Verification"], ["Outcome"]],
    day.rows.filter(pass).map((r) => el("tr", {},
      el("td", { class: "sym" }, r.symbol),
      ...["signal", "risk", "execution", "verify"].map((st) => el("td", {}, stageChip(r.stages[st]))),
      el("td", {}, el("span", { class: `outcome ${r.outcome.kind}` }, outcomeText(r.outcome))))),
    "No symbols match this filter.");
  $("path-closes").replaceChildren(...(day.closes.length ? [el("strong", {}, "Closes:"),
    ...day.closes.map((c) => el("span", {}, `${c.symbol} ${codeText(c.reason)} ${c.pnl !== null && c.pnl !== undefined ? money(+c.pnl, true) : ""}`))] : []));
}

function renderPathTrend() {
  const box = $("path-trend");
  if (!state.data || !state.data.paths) return;
  stackedBars(box, state.data.paths.trend, [
    ["entered", (d) => d.entered + d.flagged, "seg-a"],
    ["rule rejections", (d) => d.rule, "seg-b"],
    ["platform rejections", (d) => d.platform, "seg-warn"],
  ], (d) => `night ${shortDate(d.moment)} · signals ${d.signals}`);
}

// ── charts ─────────────────────────────────────────────────
function niceTicks(lo, hi, count = 4) {
  const span = hi - lo || 1;
  const raw = span / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => span / s <= count) || raw;
  const out = [];
  for (let v = Math.floor(lo / step) * step; ; v += step) {
    out.push(+v.toFixed(10));
    if (v >= hi - step * 1e-9) break;  // the scale covers all of the data
  }
  return out;
}

function renderEquity() {
  const box = $("equity-chart");
  if (!box.offsetParent) return;
  const raw = state.data.equity, initial = state.data.summary.initial;
  const points = raw.filter((p, i) => i === raw.length - 1 || raw[i + 1].t !== p.t);
  if (points.length < 2) {
    box.replaceChildren(empty("The curve appears after the first closed trade", "A trade is held for up to 7 days or until its stop."));
    return;
  }
  const W = box.clientWidth, H = box.clientHeight, m = { l: 52, r: 64, t: 12, b: 26 };
  const xs = points.map((p) => new Date(p.t).getTime()), vs = points.map((p) => p.v);
  const x0 = xs[0], x1 = Math.max(xs[xs.length - 1], Date.now());
  const ticks = niceTicks(Math.min(...vs, initial), Math.max(...vs, initial));
  const y0 = ticks[0], y1 = ticks[ticks.length - 1];
  const X = (t) => m.l + ((t - x0) / (x1 - x0 || 1)) * (W - m.l - m.r);
  const Y = (v) => H - m.b - ((v - y0) / (y1 - y0 || 1)) * (H - m.t - m.b);

  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Equity on closed trades" });
  const grid = svg("g", { class: "grid" }), axis = svg("g", { class: "axis" });
  for (const v of ticks) {
    grid.append(svg("line", { x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v) }));
    const t = svg("text", { x: m.l - 8, y: Y(v) + 4, "text-anchor": "end" });
    t.textContent = `$${v.toFixed(v % 1 ? 1 : 0)}`;
    axis.append(t);
  }
  for (const t of [x0, (x0 + x1) / 2, x1]) {
    const lbl = svg("text", { x: X(t), y: H - 6, "text-anchor": t === x0 ? "start" : t === x1 ? "end" : "middle" });
    lbl.textContent = shortDate(new Date(t).toISOString());
    axis.append(lbl);
  }
  root.append(grid, axis, svg("line", { class: "ref", x1: m.l, x2: W - m.r, y1: Y(initial), y2: Y(initial) }));
  let d = `M${X(xs[0])},${Y(vs[0])}`;
  for (let i = 1; i < xs.length; i++) d += ` H${X(xs[i])} V${Y(vs[i])}`;
  d += ` H${X(x1)}`;
  root.append(svg("path", { class: "area", d: `${d} V${Y(y0)} H${X(xs[0])} Z` }), svg("path", { class: "line", d }));
  const last = vs[vs.length - 1];
  root.append(svg("circle", { class: "end-dot", cx: X(x1), cy: Y(last), r: 4.5 }));
  const endLbl = svg("text", { class: "end-label", x: X(x1) + 10, y: Y(last) + 4 });
  endLbl.textContent = money(last);
  root.append(endLbl);

  const cross = svg("line", { class: "cross", y1: m.t, y2: H - m.b, visibility: "hidden" });
  const hit = svg("rect", { class: "hit", x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b, tabindex: 0 });
  const color = getComputedStyle(box).getPropertyValue("--series");
  const show = (i, cx, cy) => {
    cross.setAttribute("x1", X(xs[i])); cross.setAttribute("x2", X(xs[i])); cross.setAttribute("visibility", "visible");
    showTip(cx, cy, money(vs[i]), i === 0 ? `start · ${msk(points[i].t)}` : `after close · ${msk(points[i].t)}`, color);
  };
  hit.addEventListener("pointermove", (e) => {
    const r = root.getBoundingClientRect(), px = ((e.clientX - r.left) / r.width) * W;
    let best = 0;
    for (let i = 0; i < xs.length; i++) if (X(xs[i]) <= px) best = i;
    show(best, e.clientX, e.clientY);
  });
  const off = () => { cross.setAttribute("visibility", "hidden"); hideTip(); };
  hit.addEventListener("pointerleave", off);
  hit.addEventListener("focus", () => {
    const r = root.getBoundingClientRect();
    show(xs.length - 1, r.left + (X(xs[xs.length - 1]) / W) * r.width, r.top + (Y(last) / H) * r.height);
  });
  hit.addEventListener("blur", off);
  root.append(cross, hit);
  box.replaceChildren(root);
}

function renderR() {
  const box = $("r-chart");
  if (!box.offsetParent) return;
  const rows = state.data.trades.filter((t) => t.r !== null).slice().reverse();
  if (!rows.length) {
    box.replaceChildren(empty("Each trade's result in R will appear here", "Blue: profit, red: loss."));
    return;
  }
  const W = box.clientWidth, H = box.clientHeight, m = { l: 44, r: 8, t: 10, b: 10 };
  const ticks = niceTicks(Math.min(-1, ...rows.map((t) => t.r)), Math.max(1, ...rows.map((t) => t.r)), 4);
  const lo = ticks[0], hi = ticks[ticks.length - 1];
  const Y = (v) => m.t + ((hi - v) / (hi - lo)) * (H - m.t - m.b);
  const band = (W - m.l - m.r) / rows.length, bw = Math.max(2, Math.min(24, band - 2));
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Trade results in R" });
  const grid = svg("g", { class: "grid" }), axis = svg("g", { class: "axis" });
  for (const v of ticks) {
    grid.append(svg("line", { x1: m.l, x2: W - m.r, y1: Y(v), y2: Y(v) }));
    const t = svg("text", { x: m.l - 8, y: Y(v) + 4, "text-anchor": "end" });
    t.textContent = `${v > 0 ? "+" : ""}${v}R`;
    axis.append(t);
  }
  root.append(grid, axis, svg("line", { class: "baseline", x1: m.l, x2: W - m.r, y1: Y(0), y2: Y(0) }));
  const styles = getComputedStyle(box);
  rows.forEach((t, i) => {
    const cx = m.l + band * i + band / 2, up = t.r >= 0;
    const top = Math.min(Y(t.r), Y(0)), h = Math.max(1, Math.abs(Y(t.r) - Y(0)));
    const r = Math.min(4, h, bw / 2), x = cx - bw / 2;
    // 4px rounding only at the data end; a square corner at zero.
    const d = up
      ? `M${x},${top + h} V${top + r} Q${x},${top} ${x + r},${top} H${x + bw - r} Q${x + bw},${top} ${x + bw},${top + r} V${top + h} Z`
      : `M${x},${top} V${top + h - r} Q${x},${top + h} ${x + r},${top + h} H${x + bw - r} Q${x + bw},${top + h} ${x + bw},${top + h - r} V${top} Z`;
    const bar = svg("path", { class: `bar ${up ? "bar-gain" : "bar-loss"}`, d });
    const hit = svg("rect", { class: "bar-hit", x: m.l + band * i, y: m.t, width: band, height: H - m.t - m.b, tabindex: 0 });
    const label = `${t.symbol} · ${t.side === "long" ? "long" : "short"} · ${CODES[t.reason] || t.reason} · ${shortDate(t.closed_at)} · ${money(t.pnl, true)}`;
    const color = styles.getPropertyValue(up ? "--gain" : "--loss");
    const on = (e) => { bar.classList.add("lift"); showTip(e.clientX, e.clientY, rfmt(t.r), label, color); };
    const off = () => { bar.classList.remove("lift"); hideTip(); };
    hit.addEventListener("pointermove", on);
    hit.addEventListener("focus", () => { const b = hit.getBoundingClientRect(); on({ clientX: b.left + b.width / 2, clientY: b.top + 20 }); });
    hit.addEventListener("pointerleave", off);
    hit.addEventListener("blur", off);
    root.append(bar, hit);
  });
  box.replaceChildren(root);
}

function renderCharts() {
  if (!state.data) return;
  renderEquity();
  renderR();
  renderBotCharts();
  renderPathTrend();
}

// ── loading ────────────────────────────────────────────────
async function refresh() {
  try {
    const books = await api("/api/books");
    if (!books.includes(state.book)) { state.book = books[0]; localSet("book", state.book); }
    const pathQuery = state.pathMoment ? `/api/paths?moment=${encodeURIComponent(state.pathMoment)}` : "/api/paths";
    const [summary, equity, positions, trades, journal, runlog, strategy, health, bots, paths] = await Promise.all([
      api("/api/summary"), api("/api/equity"), api("/api/positions"), api("/api/trades"),
      api("/api/journal?limit=2000"), api("/api/runlog?blocks=60"), api("/api/strategy"), api("/api/health"),
      api("/api/bots"), api(pathQuery),
    ]);
    state.data = { books, summary, equity, positions, trades, journal, runlog, strategy, health, bots, paths };
    state.loadedAt = Date.now();
    renderChrome(books, summary);
    renderOverview();
    renderPositions();
    renderTrades();
    renderRisk();
    renderFilters();
    renderJournal();
    renderRuns();
    renderStrategy();
    renderBots();
    renderPaths();
    renderNav();
    renderCharts();
    tickUpdated();
  } catch (err) {
    const banner = $("banner");
    banner.className = "banner";
    banner.textContent = `⚠ No data: ${err.message}. Retrying in 5 seconds.`;
    banner.hidden = false;
    setTimeout(refresh, 5000);
  }
}

function tickUpdated() {
  if (!state.loadedAt) return;
  const sec = Math.round((Date.now() - state.loadedAt) / 1000);
  $("updated").textContent = sec < 5 ? "Updated just now" : `Updated ${sec < 60 ? `${sec} s` : `${Math.round(sec / 60)} min`} ago`;
}

$("journal-search").addEventListener("input", (e) => { state.query = e.target.value; state.shown = 150; renderJournal(); });
window.addEventListener("hashchange", route);
let resizeTimer;
window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(renderCharts, 150); });
route();
refresh();
setInterval(refresh, REFRESH_MS);
setInterval(tickUpdated, 5000);
