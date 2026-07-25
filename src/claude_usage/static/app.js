"use strict";

const API = "/api/v1";
const COLS = 9;
const els = {
  filters: document.querySelector("#filters"),
  search: document.querySelector("#search"),
  since: document.querySelector("#since"),
  tool: document.querySelector("#tool"),
  sort: document.querySelector("#sort"),
  subagentSort: document.querySelector("#subagent-sort"),
  order: document.querySelector("#order"),
  refresh: document.querySelector("#refresh"),
  cacheStatus: document.querySelector("#cache-status"),
  error: document.querySelector("#error"),
  count: document.querySelector("#result-count"),
  table: document.querySelector("#sessions-table"),
  body: document.querySelector("#sessions-body"),
  foot: document.querySelector("#sessions-foot"),
};

let items = [];
let meta = {};
let listController = null;
let searchTimer = null;
let polling = false;
const expanded = new Set();
const details = new Map();
const PREFERENCES_KEY = "claude-usage.filters.v1";

function restorePreferences() {
  try {
    const saved = JSON.parse(localStorage.getItem(PREFERENCES_KEY) || "{}");
    for (const element of [els.since, els.tool, els.sort, els.subagentSort, els.order]) {
      if (typeof saved[element.name] === "string") element.value = saved[element.name];
    }
  } catch (_) { /* Ignore unavailable or malformed device-local preferences. */ }
}

function savePreferences() {
  try {
    localStorage.setItem(PREFERENCES_KEY, JSON.stringify({
      since: els.since.value,
      tool: els.tool.value,
      sort: els.sort.value,
      subagent_sort: els.subagentSort.value,
      order: els.order.value,
    }));
  } catch (_) { /* The dashboard still works when storage is unavailable. */ }
}

function node(tag, className, text) {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== undefined && text !== null) element.textContent = text;
  return element;
}

function value(object, ...names) {
  for (const name of names) {
    if (object && object[name] !== undefined && object[name] !== null) return object[name];
  }
  return null;
}

function formatCompact(input) {
  const n = Number(input);
  if (!Number.isFinite(n)) return "—";
  if (Math.abs(n) < 1000) return Math.round(n).toLocaleString();
  if (Math.abs(n) < 1e6) return `${(n / 1e3).toFixed(1)}K`;
  if (Math.abs(n) < 1e9) return `${(n / 1e6).toFixed(1)}M`;
  return `${(n / 1e9).toFixed(1)}B`;
}

function formatDuration(input) {
  const ms = Number(input);
  if (!Number.isFinite(ms)) return "—";
  if (ms < 1000) return `${Math.round(ms)}ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds < 10 ? seconds.toFixed(1) : Math.round(seconds)}s`;
  const minutes = seconds / 60;
  if (minutes < 60) return `${minutes < 10 ? minutes.toFixed(1) : Math.round(minutes)}m`;
  const hours = minutes / 60;
  if (hours < 24) return `${hours < 10 ? hours.toFixed(1) : Math.round(hours)}h`;
  return `${(hours / 24).toFixed(1)}d`;
}

function formatBytes(input) {
  const bytes = Number(input);
  if (!Number.isFinite(bytes) || bytes < 0) return null;
  if (bytes < 1024) return `${Math.round(bytes)} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unit = units[0];
  for (let index = 1; index < units.length && value >= 1024; index += 1) {
    value /= 1024;
    unit = units[index];
  }
  return `${value < 10 ? value.toFixed(1) : Math.round(value)} ${unit}`;
}

function formatCost(input) {
  const n = Number(input);
  if (!Number.isFinite(n)) return "—";
  if (n > 0 && n < 0.01) return "<$0.01";
  return n.toLocaleString(undefined, { style: "currency", currency: "USD", minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function dateText(item) {
  const raw = value(item, "date", "timestamp");
  return raw ? String(raw).slice(0, 10) : "—";
}

function itemKey(item) {
  return `${value(item, "tool") || "unknown"}:${value(item, "session_id", "id") || "unknown"}`;
}

function modelName(record) {
  return value(record, "display_model", "primary_model", "model") || "—";
}

// Codex's "Fast" mode is OpenAI's priority tier, billed at a multiple of the
// standard rate — so the bolt explains why a row's cost looks high for its size.
// Keyed on tokens actually billed fast, not on service_tier: that field is the
// tier the thread is set to *now*, which says nothing about a thread that ran
// fast earlier and was switched off.
function fastBadge(record) {
  const fast = Number(value(record, "priority_tokens")) || 0;
  if (fast <= 0) return null;
  const total = Number(value(record, "total_tokens")) || 0;
  const badge = node("span", "fast", "⚡");
  badge.title =
    total > fast
      ? `${formatCompact(fast)} of ${formatCompact(total)} tokens billed on the fast (priority) tier`
      : "Fast (priority) service tier — billed above the standard rate";
  return badge;
}

function modelCell(record) {
  const full = value(record, "primary_model", "model") || "";
  const cell = node("td", "model", modelName(record));
  if (full && full !== modelName(record)) cell.title = full;
  const badge = fastBadge(record);
  if (badge) cell.append(badge);
  return cell;
}

function numberCell(text, raw, extraClass = "") {
  const cell = node("td", `number ${extraClass}`.trim(), text);
  if (Number(raw) === 0) cell.classList.add("zero");
  return cell;
}

function costClass(cost) {
  const n = Number(cost);
  if (!Number.isFinite(n) || n === 0) return "";
  if (n < 5) return "cost-low";
  if (n < 25) return "cost-mid";
  return "cost-high";
}

function renderSummaryRow(item) {
  const key = itemKey(item);
  const row = node("tr", "summary");
  row.dataset.key = key;
  row.setAttribute("aria-expanded", String(expanded.has(key)));
  row.append(node("td", "", dateText(item)));

  const sessionCell = node("td", "session-cell");
  const wrap = node("div", "session-wrap");
  const button = node("button", "expander");
  button.type = "button";
  button.dataset.action = "expand";
  button.dataset.key = key;
  button.setAttribute("aria-expanded", String(expanded.has(key)));
  button.setAttribute("aria-label", `${expanded.has(key) ? "Collapse" : "Expand"} ${value(item, "name") || "session"}`);
  wrap.append(button, node("span", "", value(item, "name") || "(untitled)"));
  sessionCell.title = value(item, "name") || "";
  sessionCell.append(wrap);
  row.append(sessionCell);

  row.append(numberCell(formatCompact(item.context_used_tokens), item.context_used_tokens));

  const identityCell = node("td");
  const identity = node("div", "identity");
  const model = node("span", "model", modelName(item));
  const fullModel = value(item, "primary_model", "model") || "";
  if (fullModel && fullModel !== modelName(item)) model.title = fullModel;
  identity.append(model);
  const badge = fastBadge(item);
  if (badge) identity.append(badge);
  identityCell.append(identity);
  row.append(identityCell);

  row.append(numberCell(formatCompact(item.input_tokens), item.input_tokens));
  row.append(numberCell(formatCompact(item.output_tokens), item.output_tokens));
  row.append(numberCell(formatCompact(item.cache_read_tokens), item.cache_read_tokens));
  row.append(numberCell(formatCompact(item.cache_write_tokens), item.cache_write_tokens, "optional"));
  row.append(numberCell(formatCost(item.cost_usd), item.cost_usd, costClass(item.cost_usd)));
  return row;
}

function appendAgentRows(output, agents, depth = 0) {
  if (!Array.isArray(agents)) return;
  for (const agent of agents) {
    output.push({ record: agent, label: value(agent, "label", "description", "agent_type") || "Subagent", depth, kind: "agent" });
    if (Array.isArray(agent.per_model) && agent.per_model.length > 1) {
      for (const model of agent.per_model) output.push({ record: model, label: model.model || "Model", depth: depth + 1, kind: "model" });
    }
    for (const [index, segment] of (agent.segments || []).entries()) {
      output.push({ record: segment, label: segment.label || `Context ${index + 1}`, depth: depth + 1, kind: "segment" });
    }
    appendAgentRows(output, agent.children, depth + 1);
  }
}

function detailRecords(data) {
  const output = [];
  const base = data.base;
  if (base) {
    output.push({ record: base, label: "main", depth: 0, kind: "agent" });
    if (Array.isArray(base.per_model) && base.per_model.length > 1) {
      for (const model of base.per_model) output.push({ record: model, label: model.model || "Model", depth: 1, kind: "model" });
    }
    for (const [index, segment] of (base.segments || []).entries()) {
      output.push({ record: segment, label: segment.label || `Context ${index + 1}`, depth: 1, kind: "segment" });
    }
  }
  appendAgentRows(output, data.subagents);
  return output;
}

function renderWorkerRow(item, detail) {
  const { record, label, depth, kind } = detail;
  const row = node("tr", `worker-row ${kind}`);
  row.dataset.detailFor = itemKey(item);
  const labelCell = node("td", "worker-label");
  labelCell.style.setProperty("--depth", depth);
  labelCell.append(node("span", "tree-mark", depth ? "└" : ""), node("span", "", label));
  const notes = [];
  if (record.peak_context_tokens) notes.push(`Peak context: ${formatCompact(record.peak_context_tokens)}`);
  if (record.context_window_tokens) notes.push(`Window: ${formatCompact(record.context_window_tokens)}`);
  if (notes.length) row.title = notes.join(" · ");
  row.append(
    node("td", "detail-gutter", ""),
    labelCell,
    numberCell(formatCompact(record.context_used_tokens), record.context_used_tokens),
    modelCell(record),
    numberCell(formatCompact(record.input_tokens), record.input_tokens),
    numberCell(formatCompact(record.output_tokens), record.output_tokens),
    numberCell(formatCompact(record.cache_read_tokens), record.cache_read_tokens),
    numberCell(formatCompact(record.cache_write_tokens), record.cache_write_tokens, "optional"),
    numberCell(formatCost(record.cost_usd), record.cost_usd, costClass(record.cost_usd)),
  );
  return row;
}

function renderLoadingRow(item, message = "Loading details…") {
  const row = node("tr", "detail-row");
  row.dataset.detailFor = itemKey(item);
  const cell = node("td", "detail-loading", message);
  cell.colSpan = COLS;
  row.append(cell);
  return row;
}

function renderTotals() {
  const totals = meta.totals || meta.total;
  if (!totals) {
    els.foot.hidden = true;
    els.foot.replaceChildren();
    return;
  }
  const row = node("tr");
  const label = node("td", "", "Total");
  label.colSpan = 2;
  row.append(
    label,
    numberCell(formatCompact(totals.context_used_tokens), totals.context_used_tokens),
    node("td", "", ""),
    numberCell(formatCompact(totals.input_tokens), totals.input_tokens),
    numberCell(formatCompact(totals.output_tokens), totals.output_tokens),
    numberCell(formatCompact(totals.cache_read_tokens), totals.cache_read_tokens),
    numberCell(formatCompact(totals.cache_write_tokens), totals.cache_write_tokens, "optional"),
    numberCell(formatCost(totals.cost_usd), totals.cost_usd, costClass(totals.cost_usd)),
  );
  els.foot.replaceChildren(row);
  els.foot.hidden = false;
}

function render() {
  const fragment = document.createDocumentFragment();
  if (!items.length) {
    const row = node("tr");
    const cell = node("td", "empty", "No sessions match these filters.");
    cell.colSpan = COLS;
    row.append(cell);
    fragment.append(row);
  } else {
    for (const item of items) {
      fragment.append(renderSummaryRow(item));
      if (expanded.has(itemKey(item))) {
        const detail = details.get(itemKey(item));
        if (!detail) fragment.append(renderLoadingRow(item));
        else if (detail.error) fragment.append(renderLoadingRow(item, detail.error));
        else {
          const records = detailRecords(detail);
          if (!records.length) fragment.append(renderLoadingRow(item, "No agent detail is available."));
          else for (const record of records) fragment.append(renderWorkerRow(item, record));
        }
      }
    }
  }
  els.body.replaceChildren(fragment);
  els.count.textContent = `${items.length.toLocaleString()} ${items.length === 1 ? "session" : "sessions"}`;
  renderTotals();
}

function showError(message) {
  els.error.textContent = message;
  els.error.hidden = false;
}

function clearError() {
  els.error.hidden = true;
  els.error.textContent = "";
}

function renderStatus(state = meta, primaryOverride = null) {
  let primary = primaryOverride;
  if (!primary && state.refreshing) primary = "Scanning session logs…";
  else {
    const raw = value(state, "scanned_at", "generated_at", "updated_at");
    if (!primary && !raw) primary = "Cached data loaded";
    else {
      const date = new Date(raw);
      if (!primary && Number.isNaN(date.valueOf())) primary = `Updated ${raw}`;
      else {
        const seconds = Math.max(0, Math.round((Date.now() - date.valueOf()) / 1000));
        if (!primary && seconds < 60) primary = "Updated just now";
        else if (!primary && seconds < 3600) primary = `Updated ${Math.floor(seconds / 60)}m ago`;
        else if (!primary && seconds < 86400) primary = `Updated ${Math.floor(seconds / 3600)}h ago`;
        else if (!primary) primary = `Updated ${date.toLocaleString()}`;
      }
    }
  }
  const metrics = [];
  if (Number.isFinite(Number(state.scan_ms))) {
    metrics.push(`Refresh ${formatDuration(state.scan_ms)}`);
  }
  const memory = formatBytes(state.memory_bytes);
  if (memory) metrics.push(`${memory} memory`);

  const children = [node("span", "cache-status-primary", primary || "Status unavailable")];
  if (metrics.length) children.push(node("span", "cache-status-meta", metrics.join(" · ")));
  els.cacheStatus.replaceChildren(...children);
}

function queryString() {
  const params = new URLSearchParams();
  for (const element of [els.search, els.since, els.tool, els.sort, els.order]) {
    if (element.value) params.set(element.name, element.value);
  }
  return params.toString();
}

async function responseJson(response) {
  let body = null;
  try { body = await response.json(); } catch (_) { /* use HTTP status below */ }
  if (!response.ok) throw new Error(value(body, "detail", "error", "message") || `Request failed (${response.status})`);
  return body || {};
}

async function loadSessions({ quiet = false } = {}) {
  if (listController) listController.abort();
  const controller = new AbortController();
  listController = controller;
  els.table.setAttribute("aria-busy", "true");
  if (!quiet && !items.length) {
    const row = node("tr");
    const cell = node("td", "empty", "Loading sessions…");
    cell.colSpan = COLS;
    row.append(cell);
    els.body.replaceChildren(row);
  }
  try {
    const response = await fetch(`${API}/sessions?${queryString()}`, { signal: controller.signal, headers: { Accept: "application/json" } });
    const data = await responseJson(response);
    if (controller !== listController) return;
    const previousGeneration = meta.generation;
    items = Array.isArray(data.items) ? data.items : [];
    meta = data.meta || {};
    const generationChanged = previousGeneration !== undefined && previousGeneration !== meta.generation;
    if (generationChanged) details.clear();
    clearError();
    if (meta.last_error) showError(`Update failed; showing cached data. ${meta.last_error}`);
    else if (Array.isArray(meta.warnings) && meta.warnings.length) showError(`Scan note: ${meta.warnings.join(" ")}`);
    renderStatus();
    render();
    if (generationChanged) {
      for (const item of items) {
        if (expanded.has(itemKey(item))) void loadDetail(item);
      }
    }
    if (meta.refreshing && !polling) {
      els.refresh.disabled = true;
      void pollRefresh();
    }
  } catch (error) {
    if (error.name !== "AbortError") showError(`Could not load sessions: ${error.message}`);
  } finally {
    if (controller === listController) els.table.removeAttribute("aria-busy");
  }
}

async function loadDetail(item) {
  const key = itemKey(item);
  if (details.has(key)) return;
  const generation = meta.generation;
  const tool = encodeURIComponent(value(item, "tool") || "unknown");
  const id = encodeURIComponent(value(item, "session_id", "id") || "unknown");
  try {
    const params = new URLSearchParams({ subagent_sort: els.subagentSort.value });
    const response = await fetch(`${API}/sessions/${tool}/${id}?${params}`, { headers: { Accept: "application/json" } });
    const detail = await responseJson(response);
    if (generation !== meta.generation) return;
    details.set(key, detail);
  } catch (error) {
    if (generation !== meta.generation) return;
    details.set(key, { error: `Detail unavailable: ${error.message}` });
  }
  if (expanded.has(key)) render();
}

async function pollRefresh() {
  if (polling) return;
  polling = true;
  let failures = 0;
  try {
    while (true) {
      await new Promise((resolve) => setTimeout(resolve, 700));
      try {
        const response = await fetch(`${API}/status`, { headers: { Accept: "application/json" } });
        const state = await responseJson(response);
        failures = 0;
        renderStatus(state);
        if (!state.refreshing) {
          if (state.last_error) showError(`Update failed; showing cached data. ${state.last_error}`);
          await loadSessions({ quiet: true });
          break;
        }
      } catch (error) {
        failures += 1;
        if (failures >= 4) throw error;
      }
    }
  } catch (error) {
    showError(`Lost track of the update: ${error.message}. Cached data is still shown.`);
  } finally {
    polling = false;
    els.refresh.disabled = false;
  }
}

async function refreshData() {
  clearError();
  els.refresh.disabled = true;
  renderStatus(meta, "Starting scan…");
  try {
    const response = await fetch(`${API}/refresh`, { method: "POST", headers: { Accept: "application/json" } });
    const state = await responseJson(response);
    renderStatus({ ...state, refreshing: true });
    await pollRefresh();
  } catch (error) {
    showError(`Could not update data: ${error.message}`);
    renderStatus();
    els.refresh.disabled = false;
  }
}

els.filters.addEventListener("submit", (event) => event.preventDefault());
els.search.addEventListener("input", () => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => loadSessions(), 250);
});
[els.since, els.tool, els.sort, els.order].forEach((element) => element.addEventListener("change", () => {
  savePreferences();
  loadSessions();
}));
els.subagentSort.addEventListener("change", () => {
  savePreferences();
  details.clear();
  render();
  for (const item of items) {
    if (expanded.has(itemKey(item))) void loadDetail(item);
  }
});
els.refresh.addEventListener("click", refreshData);
els.body.addEventListener("click", (event) => {
  const button = event.target.closest("[data-action='expand']");
  if (!button) return;
  const key = button.dataset.key;
  const item = items.find((candidate) => itemKey(candidate) === key);
  if (!item) return;
  if (expanded.has(key)) expanded.delete(key);
  else {
    expanded.add(key);
    loadDetail(item);
  }
  render();
});

restorePreferences();
loadSessions();
