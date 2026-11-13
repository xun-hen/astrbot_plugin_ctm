// 对话认知中枢 · 运行状态页
//
// 只通过插件页面 bridge 访问后端（不做相对路径 fetch，Dashboard 会注入资源令牌）。
// 所有渲染用 textContent，记忆正文不拼进 innerHTML。

const bridge = window.AstrBotPluginPage || window.AstrBotPluginView;
const POLL_MS = 5000;
const LIMIT = 20;

const $ = (id) => document.getElementById(id);
const el = {
  dot: $("dot"),
  live: $("live-state"),
  fatal: $("fatal"),
  auto: $("auto"),
  refresh: $("refresh"),
  turns: $("c-turns"),
  decisions: $("c-decisions"),
  tokens: $("c-tokens"),
  dropped: $("c-dropped"),
  preempted: $("c-preempted"),
  lt: $("c-lt"),
  active: $("c-active"),
  topics: $("c-topics"),
  sessions: $("c-sessions"),
  config: $("config"),
  activeRows: $("active-rows"),
  activeEmpty: $("active-empty"),
  activeCount: $("active-count"),
  turnRows: $("turn-rows"),
  turnEmpty: $("turn-empty"),
  topicRows: $("topic-rows"),
  topicEmpty: $("topic-empty"),
  memoryRows: $("memory-rows"),
  memoryEmpty: $("memory-empty"),
  health: $("health"),
};

let timer = null;

function setFatal(text) {
  el.fatal.hidden = false;
  el.fatal.textContent = text;
  el.dot.className = "dot bad";
}

function clearFatal() {
  el.fatal.hidden = true;
  el.dot.className = "dot ok";
}

function int(value) {
  const n = Number(value);
  return Number.isFinite(n) ? String(Math.round(n)) : "–";
}

function fmtTime(seconds) {
  const n = Number(seconds);
  if (!Number.isFinite(n) || n <= 0) return "–";
  const d = new Date(n * 1000);
  return d.toLocaleString("zh-CN", { hour12: false });
}

function shortSession(value) {
  const text = String(value || "");
  const parts = text.split(":");
  if (parts.length >= 3) return `${parts[0]}:${parts.slice(2).join(":")}`;
  return text || "–";
}

function cell(text, className) {
  const td = document.createElement("td");
  td.textContent = text;
  if (className) td.className = className;
  return td;
}

function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function fillDefinitionList(node, entries) {
  clear(node);
  for (const [key, value] of entries) {
    const dt = document.createElement("dt");
    dt.textContent = key;
    const dd = document.createElement("dd");
    dd.textContent = value;
    node.append(dt, dd);
  }
}

function toggleEmpty(emptyNode, hasRows) {
  emptyNode.hidden = hasRows;
}

function renderOverview(data) {
  const diag = data.diag || {};
  el.turns.textContent = int(diag.turns);
  el.decisions.textContent = int(diag.decisions);
  el.tokens.textContent = int(diag.injected_tokens);
  el.dropped.textContent = int(diag.dropped_items);
  el.preempted.textContent = int(diag.no_decision);
  el.lt.textContent = int((data.longterm || {}).items || 0);
  const rounds = ((data.aggregation || {}).active || []).length;
  el.active.textContent = int(rounds);
  const tables = data.tables || {};
  el.topics.textContent = int(tables.topics);
  el.sessions.textContent = `会话 ${int(tables.sessions)}`;

  const topic = data.topic || {};
  const agg = data.aggregation || {};
  const budget = data.budget || {};
  fillDefinitionList(el.config, [
    ["话题策略", `${topic.strategy}（阈值 ${topic.threshold}，最少 ${topic.min_turns} 轮，冷却 ${topic.cooldown_seconds}s）`],
    ["话题判定", topic.judge_enabled
      ? `开启；模型：${topic.judge_provider_id}；新话题门槛 ${topic.judge_min_confidence}；每小时上限 ${topic.judge_max_checks_per_hour}`
      : "关闭（仅启发式）"],
    ["请求钩子兜底", topic.detect_in_hook ? "开启" : "关闭"],
    ["消息聚合", agg.enabled
      ? `开启；防抖 ${agg.debounce_seconds}s，总等待上限 ${agg.max_wait_seconds}s`
      : "关闭"],
    ["注入预算", `${budget.injection_tokens} tokens（摘要 ${budget.summary_limit} / 断言 ${budget.assertion_limit} / 情节 ${budget.episode_limit}）`],
    ["Prompt 增幅上限", `${Math.round((budget.max_prompt_growth_ratio || 0) * 100)}%`],
  ]);

  renderActive(agg.active || []);
}

function renderActive(rows) {
  clear(el.activeRows);
  for (const row of rows) {
    const tr = document.createElement("tr");
    tr.append(
      cell(shortSession(row.session)),
      cell(row.sender || "–"),
      cell(int(row.items)),
      cell(`${Number(row.age_seconds || 0).toFixed(1)}s`)
    );
    el.activeRows.appendChild(tr);
  }
  el.activeCount.textContent = rows.length ? `${rows.length} 轮` : "";
  toggleEmpty(el.activeEmpty, rows.length > 0);
}

function renderTurns(items) {
  clear(el.turnRows);
  for (const row of items) {
    const tr = document.createElement("tr");
    tr.append(
      cell(fmtTime(row.at)),
      cell(shortSession(row.session)),
      cell(row.action || "–"),
      cell(row.persona || "–"),
      cell(int(row.tokens)),
      cell(int(row.dropped))
    );
    el.turnRows.appendChild(tr);
  }
  toggleEmpty(el.turnEmpty, items.length > 0);
}

function renderTopics(items) {
  clear(el.topicRows);
  for (const row of items) {
    const tr = document.createElement("tr");
    tr.append(
      cell(fmtTime(row.at)),
      cell(shortSession(row.session)),
      cell(row.title || "–"),
      cell(row.status || "–")
    );
    el.topicRows.appendChild(tr);
  }
  toggleEmpty(el.topicEmpty, items.length > 0);
}

function renderMemories(data) {
  clear(el.memoryRows);
  const items = data.items || [];
  for (const row of items) {
    const tr = document.createElement("tr");
    const content = row.slot_key ? `${row.slot_key}: ${row.content}` : row.content;
    tr.append(
      cell(fmtTime(row.at)),
      cell(row.scope || "–"),
      cell(row.kind || "–"),
      cell(content || "–", "wrap"),
      cell(String(row.importance ?? "–"))
    );
    el.memoryRows.appendChild(tr);
  }
  if (data.enabled === false) {
    el.memoryEmpty.textContent = "长期记忆未启用";
  }
  toggleEmpty(el.memoryEmpty, items.length > 0);
}

function renderHealth(data) {
  const db = data.db || {};
  const lt = data.longterm || {};
  fillDefinitionList(el.health, [
    ["数据库", db.ok ? `正常（${(db.size_bytes / 1024).toFixed(1)} KB，${db.tables} 张表）` : `异常：${db.error || "未知"}`],
    ["数据库路径", db.path || "–"],
    ["归档队列", `${int(data.archive_queue)} 条`],
    ["聚合进行中", `${int(data.aggregation_active)} 轮`],
    ["长期记忆", lt.enabled ? `启用，${int(lt.items)} 条` : "未启用"],
  ]);
}

async function apiGet(name, params) {
  if (!bridge || typeof bridge.apiGet !== "function") {
    throw new Error("页面 bridge 不可用（请从 AstrBot 插件页打开）");
  }
  return bridge.apiGet(name, params);
}

async function loadAll() {
  if (!bridge) {
    setFatal("未检测到插件页面 bridge：请从 AstrBot 的插件详情页打开本页面。");
    return;
  }
  try {
    const [overview, turns, topics, memories, health] = await Promise.all([
      apiGet("overview"),
      apiGet("turns", { limit: LIMIT }),
      apiGet("topics", { limit: LIMIT }),
      apiGet("memories", { limit: LIMIT }),
      apiGet("health"),
    ]);
    renderOverview(overview);
    renderTurns(turns.items || []);
    renderTopics(topics.items || []);
    renderMemories(memories);
    renderHealth(health);
    clearFatal();
    el.live.textContent = `已更新 ${new Date().toLocaleTimeString("zh-CN", { hour12: false })}`;
  } catch (error) {
    setFatal(`读取失败：${error && error.message ? error.message : error}`);
    el.live.textContent = "读取失败";
  }
}

function schedule() {
  if (timer) {
    clearInterval(timer);
    timer = null;
  }
  if (el.auto.checked) {
    timer = setInterval(loadAll, POLL_MS);
  }
}

el.refresh.addEventListener("click", loadAll);
el.auto.addEventListener("change", schedule);

loadAll();
schedule();
