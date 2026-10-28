"use strict";

(() => {
  const $ = (id) => document.getElementById(id);
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined && text !== null) element.textContent = String(text);
    if (className) element.className = className;
    return element;
  };
  const append = (parent, ...children) => children.forEach((child) => parent.append(child));
  const json = (value) => JSON.stringify(value, null, 2);
  const scrollBehavior = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
  const money = (value) => value === undefined || value === null ? "—" : String(value).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  const names = { customer: "客户", M1: "商户 M1", M2: "商户 M2", clearing: "清算账户" };
  const kinds = { OPENING: "合成开户", PURCHASE: "购买", REVERSAL: "全额冲正" };
  const actionLessons = { opening: 1, purchase: 2, replay_purchase: 3, conflict_purchase: 3, reversal: 4, replay_reversal: 5, duplicate_reversal: 5, drift: 6, verify: 7, freeze: 8, preview: 9, rebuild: 10, resume: 11 };
  const chapters = [ ["01", "看懂一笔账", [1, 2]], ["02", "拒绝与身份", [3]], ["03", "冲正与历史", [4, 5]], ["04", "找到错误", [6, 7]], ["05", "安全修复", [8, 9, 10, 11, 12]] ];
  let state = null;
  let content = null;
  let readingNumber = 1;
  let selectedEventId = null;
  let busy = false;
  let auto = false;
  let ended = false;
  let readingTab = "why";
  let evidenceTab = "journals";
  let sourceVersion = 0;
  const sourceCache = new Map();

  function notice(message, error = false) {
    $("notice").textContent = message;
    $("notice").classList.toggle("error", error);
    $("notice").hidden = !message;
  }
  function activeEvent() {
    return state?.events?.find((event) => String(event.id) === String(selectedEventId)) || null;
  }
  function lesson(number = readingNumber) {
    return content?.lessons?.find((item) => item.number === number);
  }
  function accountLabel(id) {
    const account = state?.accounts?.find((item) => item.id === id);
    return names[account?.label] || account?.label || id || "—";
  }
  function clear(element) { element.replaceChildren(); }
  function empty(parent, title, body) {
    const element = node("div", undefined, "empty-state");
    append(element, node("strong", title), node("span", body));
    parent.append(element);
  }
  function disclosure(title, body, className = "disclosure") {
    const details = node("details", undefined, className);
    details.append(node("summary", title));
    if (typeof body === "string") details.append(node("p", body));
    else if (body) details.append(body);
    return details;
  }
  function table(headers, rows, numeric = []) {
    const wrap = node("div", undefined, "table-wrap");
    const element = node("table");
    const head = node("thead");
    const hrow = node("tr");
    headers.forEach((label, index) => {
      const cell = node("th", label, numeric.includes(index) ? "numeric" : "");
      cell.scope = "col";
      hrow.append(cell);
    });
    head.append(hrow);
    const body = node("tbody");
    rows.forEach((values) => {
      const row = node("tr");
      if (values.change) row.classList.add(`row-${values.change}`);
      values.forEach((value, index) => {
        const cell = node("td", undefined, numeric.includes(index) ? "numeric" : "");
        if (value instanceof Node) cell.append(value);
        else cell.textContent = String(value ?? "—");
        row.append(cell);
      });
      body.append(row);
    });
    append(element, head, body);
    wrap.append(element);
    return wrap;
  }
  function keyValues(values) {
    const list = node("dl", undefined, "key-value");
    values.forEach(([label, value, mono = false]) => append(list, node("dt", label), node("dd", value ?? "—", mono ? "mono" : "")));
    return list;
  }
  function payloadText(operation) {
    if (!operation.payload_hex) return operation.payload ? json(operation.payload) : "未返回原始请求";
    try {
      const bytes = operation.payload_hex.match(/.{1,2}/g).map((pair) => parseInt(pair, 16));
      return new TextDecoder("utf-8", { fatal: true }).decode(new Uint8Array(bytes));
    } catch { return operation.payload_hex; }
  }

  function renderNavigation() {
    const nav = $("chapter-nav");
    clear(nav);
    chapters.forEach(([index, title, numbers]) => {
      const section = node("div", undefined, "chapter");
      const heading = node("p", undefined, "chapter-title");
      append(heading, node("span", index), node("strong", title));
      const list = node("div", undefined, "chapter-steps");
      numbers.forEach((number) => {
        const complete = state.guide.index >= number;
        const current = Math.min(state.guide.index + 1, 12) === number;
        const button = node("button", undefined, "chapter-step");
        button.type = "button";
        if (complete) button.classList.add("complete");
        if (readingNumber === number) button.classList.add("reading");
        if (current) button.setAttribute("aria-current", "step");
        button.setAttribute("aria-label", `第 ${number} 步，${state.guide.steps[number - 1]?.title || ""}，${complete ? "已完成" : "待执行"}，点击阅读`);
        append(button, node("span", complete ? "✓" : String(number).padStart(2, "0"), "step-dot"), node("span", state.guide.steps[number - 1]?.title || lesson(number)?.title));
        button.addEventListener("click", () => {
          readingNumber = number;
          selectedEventId = number <= state.guide.index ? state.events.find((event) => event.guide_step === number)?.id ?? null : null;
          renderNavigation();
          renderReading();
          renderResult();
          renderJournals();
          renderOperations();
        });
        list.append(button);
      });
      append(section, heading, list);
      nav.append(section);
    });
  }

  function renderBalances() {
    const panels = $("balance-panels");
    clear(panels);
    ["customer", "M1", "clearing"].forEach((label) => {
      const balance = state.balances.find((item) => item.label === label);
      const panel = node("div", undefined, "balance-panel");
      const heading = node("div", undefined, "balance-heading");
      append(heading, node("strong", names[label]), node("span", label === "clearing" ? "平台资产 · 借方正常" : "平台负债 · 贷方正常", "balance-class"));
      const main = node("div", undefined, "balance-main");
      append(main, node("span", money(balance?.booked), "amount"), node("span", balance ? "grosz" : "尚未开户", "currency"));
      const bottom = node("div", undefined, "balance-bottom");
      [["可用", balance?.available], ["预留", balance?.reserved], ["版本", balance?.version]].forEach(([name, value]) => {
        const item = node("span", name);
        item.append(node("strong", money(value)));
        bottom.append(item);
      });
      append(panel, heading, main, bottom);
      panels.append(panel);
    });
  }

  function renderGuide() {
    const count = state.guide.index;
    const next = lesson(Math.min(count + 1, 12));
    $("step-counter").textContent = count >= 12 ? "十二步实验 / 已完成" : `当前待执行 / ${String(count + 1).padStart(2, "0")}—12`;
    $("lesson-title").textContent = state.guide.customized ? "你已进入自由实验" : count >= 12 ? "一条完整的账本故障链，走通了" : next.title;
    $("lesson-summary").textContent = state.guide.customized ? "自由操作改变了标准故事的前提。你可以继续观察真实数据、重放和维护；重置后可重新运行十二步导览。" : count >= 12 ? "历史没有被抹掉，漂移有修复记录，新的离线报告给出了可检查的健康结果。现在可以展开自由实验，继续比较新业务和原身份重放。" : next.summary;
    $("step-preview").textContent = state.guide.customized ? "左侧章节仍可阅读；每个操作的实际 SQL 和结果都会保留在下方记录中。导览暂停，避免把预设故事当作真实执行结果。" : count >= 12 ? "继续学习：展开“自由实验”，试试原冲正身份重放，再对比新业务的重复冲正。看实际结果、业务 ID、请求键和分录数量如何不同。" : next.preview;
    $("lesson-state").textContent = state.guide.customized ? "自由操作中" : count >= 12 ? "导览完成" : count ? "真实数据库 · 继续" : "空工作区 · 开始";
    $("progress-label").textContent = `${count} / 12 已完成`;
    $("progress-fill").style.width = `${count / 12 * 100}%`;
    $("guide-progress").setAttribute("aria-valuenow", count);
    $("next-label").textContent = busy ? "正在执行…" : count >= 12 ? "十二步已完成" : "执行这一步";
  }

  function renderResult() {
    const event = activeEvent();
    $("last-result").hidden = !event;
    if (!event) return;
    $("result-title").textContent = `${event.id === state.last_event?.id ? "刚刚发生了什么" : "回看操作记录"} · ${event.title}`;
    const status = $("result-status");
    status.className = "subtle-tag result-status";
    const errorCode = event.result?.error?.code;
    const rejected = event.http_status >= 400;
    status.classList.add(rejected ? "rejected" : "success");
    status.textContent = event.http_status ? `${event.http_status}${event.replayed ? " · 原结果重放" : ""}` : "已执行";
    const copy = event.replayed ? "本次使用保存的原身份与原内容，返回已保存结果。比较余额和分录，确认没有新增资金效果。" : errorCode ? `实际返回 ${errorCode}。查看返回结果与 SQL；业务拒绝是否保存、异常是否回滚，要以实际记录为准。` : event.action === "verify" ? "导出事务结束后，核对器离线读取导出文件；报告位于“核对与修复”页。" : event.action === "rebuild" ? "余额投影、修复明细和 projection epoch 已在维护事务中处理；工作区仍需显式恢复。" : "下面展示本次操作前后的真实余额，以及服务端实际返回内容。完整 SQL 顺序在右侧“执行过程”中。";
    $("result-copy").textContent = copy;
    const deltas = $("balance-deltas");
    clear(deltas);
    const before = new Map((event.before || []).map((row) => [row.account_id, row]));
    (event.after || []).forEach((row) => {
      const previous = before.get(row.account_id);
      const changed = previous?.booked !== row.booked;
      if (!changed && row.label !== "customer" && row.label !== "M1") return;
      const item = node("div", undefined, "delta-item");
      append(item, node("span", names[row.label] || row.label), node("strong", `${money(previous?.booked)} → ${money(row.booked)}`, changed ? "" : "unchanged"));
      deltas.append(item);
    });
    const meta = $("result-meta");
    clear(meta);
    append(meta, node("span", `实际连接角色：${event.role || "—"}`), node("span", `操作 #${event.id} · ${time(event.at)}`));
    if (event.request) {
      meta.append(keyValues([["业务 ID", event.request.operation_id, true], ["请求键", event.request.key, true]]));
    }
    $("result-json").textContent = json(event.result);
    renderDiffs(event);
  }

  const diffTables = {
    balances: ["余额投影", ["account_id"]],
    journals: ["分录表头", ["journal_id"]],
    entries: ["分录明细", ["journal_id", "line_no"]],
    operations: ["业务结果", ["operation_id"]],
    requests: ["请求结果", ["client_id", "route_family", "key"]],
    reversal_attempts: ["冲正尝试", ["operation_id"]],
    reversal_effects: ["成功冲正关系", ["original_operation_id"]],
    maintenance_freezes: ["冻结与恢复", ["freeze_id"]],
    repairs: ["修复记录", ["repair_id"]],
    repair_rows: ["修复明细", ["repair_id", "account_id"]]
  };
  function diffRows(event, name) {
    const keys = diffTables[name]?.[1];
    if (!keys || !event?.before_state || !event?.after_state) return [];
    const identity = (row) => keys.map((key) => String(row[key])).join("|");
    const before = new Map((event.before_state[name] || []).map((row) => [identity(row), row]));
    const after = new Map((event.after_state[name] || []).map((row) => [identity(row), row]));
    const rows = [];
    after.forEach((row, key) => {
      if (!before.has(key)) rows.push({ type: "added", key, before: null, after: row });
      else if (json(before.get(key)) !== json(row)) rows.push({ type: "changed", key, before: before.get(key), after: row });
    });
    before.forEach((row, key) => { if (!after.has(key)) rows.push({ type: "removed", key, before: row, after: null }); });
    return rows;
  }
  function rowChange(name, row) {
    const keys = diffTables[name]?.[1];
    if (!keys) return null;
    const key = keys.map((field) => String(row[field])).join("|");
    return diffRows(activeEvent(), name).find((item) => item.key === key)?.type || null;
  }
  function renderDiffs(event) {
    const container = $("record-diffs");
    clear(container);
    if (!event.before_state || !event.after_state) return;
    const body = node("div");
    let count = 0;
    const beforeWorkspace = event.before_state.workspace;
    const afterWorkspace = event.after_state.workspace;
    if (json(beforeWorkspace) !== json(afterWorkspace)) {
      const exact = node("div");
      append(exact, node("h4", "操作前"), node("pre", json(beforeWorkspace), "json-block"), node("h4", "操作后"), node("pre", json(afterWorkspace), "json-block"));
      body.append(disclosure("工作区 · 模式 / 投影代次变化", exact));
      count += 1;
    }
    Object.entries(diffTables).forEach(([name, [title]]) => {
      const changes = diffRows(event, name);
      if (!changes.length) return;
      count += changes.length;
      const group = node("div", undefined, "diff-group");
      append(group, node("h4", `${title} · ${changes.length} 行变化`), node("p", name, "source-path"));
      changes.forEach((change) => {
        const exact = node("div");
        if (change.before) append(exact, node("h4", "操作前"), node("pre", json(change.before), "json-block"));
        if (change.after) append(exact, node("h4", "操作后"), node("pre", json(change.after), "json-block"));
        const label = change.type === "added" ? "新增" : change.type === "changed" ? "修改" : "移除";
        const detail = disclosure(`${label} · ${change.after?.label || change.key}`, exact);
        detail.classList.add(`diff-${change.type}`);
        group.append(detail);
      });
      body.append(group);
    });
    if (!count) body.append(node("p", "本次操作没有改变这些账本业务表；核对导出和只读重放也可能不改变资金数据。", "table-note"));
    container.append(disclosure(`查看本次表级变化 · ${count} 行（含原始前后值）`, body));
  }

  function time(value) {
    if (!value) return "—";
    const parsed = new Date(value);
    return Number.isNaN(parsed.valueOf()) ? String(value) : parsed.toLocaleTimeString("zh-CN", { hour12: false });
  }

  function renderJournals() {
    const panel = $("panel-journals");
    clear(panel);
    if (!state.journals.length) return empty(panel, "还没有记账分录", "执行开户后，这里会出现实际 journal 和 journal_entry。拒绝的购买不会新增分录。");
    panel.append(node("p", "D = 借方，C = 贷方。对客户 / 商户负债账户，借方减少余额、贷方增加余额；清算资产账户方向相反。", "table-note"));
    const event = activeEvent();
    const oldIds = new Set((event?.before_state?.journals || []).map((item) => item.journal_id));
    const newIds = new Set((event?.after_state?.journals || []).map((item) => item.journal_id).filter((id) => !oldIds.has(id)));
    [...state.journals].reverse().forEach((journal) => {
      const item = node("div", undefined, "journal");
      const heading = node("div", undefined, "journal-head");
      append(heading, node("strong", `${kinds[journal.kind] || journal.kind}${newIds.has(journal.journal_id) ? " · 本次新增" : ""}`), node("span", `共 ${journal.line_count} 行 · 声明总额 ${money(journal.total_minor)}`, "mono"));
      const entries = state.entries.filter((entry) => entry.journal_id === journal.journal_id).sort((a, b) => a.line_no - b.line_no);
      const rows = entries.map((entry) => {
        const row = [node("span", accountLabel(entry.account_id), "entry-account"), node("span", entry.side === "D" ? "D · 借方" : "C · 贷方", entry.side === "D" ? "debit" : "credit"), entry.side === "D" ? money(entry.amount_minor) : "—", entry.side === "C" ? money(entry.amount_minor) : "—"];
        row.change = rowChange("entries", entry);
        return row;
      });
      append(item, heading, table(["账户", "方向", "借方金额", "贷方金额"], rows, [2, 3]));
      const detail = node("div");
      detail.append(keyValues([["Journal ID", journal.journal_id, true], ["业务 ID", journal.operation_id, true], ["入账时间", journal.recorded_at]]));
      const disclosureElement = disclosure("查看分录身份与原始行", detail);
      disclosureElement.style.padding = "0 12px 10px";
      detail.append(node("pre", json({ journal, entries }), "json-block"));
      item.append(disclosureElement);
      panel.append(item);
    });
  }

  function renderOperations() {
    const panel = $("panel-operations");
    clear(panel);
    if (!state.operations.length) return empty(panel, "还没有业务记录", "同一业务 ID 与同一请求键的重试，会重放原来保存的成功或拒绝。");
    panel.append(node("p", "业务 ID 回答“哪笔业务”；请求键回答“哪个调用方的哪次逻辑请求”。下面是原始数据库记录，不是页面生成的示例身份。", "table-note"));
    [...state.operations].reverse().forEach((operation) => {
      const item = node("div", undefined, "operation");
      const operationChange = rowChange("operations", operation);
      if (operationChange) item.classList.add(`record-${operationChange}`);
      const heading = node("div", undefined, "operation-header");
      append(heading, node("span", `${kinds[operation.kind] || operation.kind}${operationChange === "added" ? " · 本次新增" : ""}`), node("span", `${operation.http_status} · ${operation.admission}`, "subtle-tag"));
      append(item, heading, node("div", operation.operation_id, "operation-id"));
      const body = operation.response_body;
      if (body?.error?.code) item.append(node("p", body.error.code, "table-note"));
      const requests = state.requests.filter((request) => request.operation_id === operation.operation_id);
      requests.forEach((request) => {
        const requestNode = node("div", undefined, "request-row");
        const requestChange = rowChange("requests", request);
        if (requestChange) requestNode.classList.add(`record-${requestChange}`);
        append(requestNode, node("div", `请求键 · ${request.route_family}`), node("div", request.key, "mono"));
        item.append(requestNode);
      });
      const detail = node("div");
      append(detail, node("h4", "记账前保存的原始请求"), node("pre", payloadText(operation), "json-block"), node("h4", "保存的业务结果"), node("pre", json(body), "json-block"));
      const attempts = state.reversal_attempts.filter((row) => row.operation_id === operation.operation_id);
      const effects = state.reversal_effects.filter((row) => row.reversal_operation_id === operation.operation_id || row.original_operation_id === operation.operation_id);
      if (attempts.length || effects.length) append(detail, node("h4", "冲正尝试与成功关系"), node("pre", json({ attempts, successful_effects: effects }), "json-block"));
      item.append(disclosure("原始请求 / 保存结果 / 冲正关系", detail));
      panel.append(item);
    });
  }

  function renderReport(parent, evidence, title) {
    if (!evidence?.report) return;
    const report = evidence.report;
    const heading = node("div", undefined, "report-top");
    heading.append(node("strong", title));
    if (evidence.stale) heading.append(node("span", "旧快照 · 不代表当前状态", "stale-tag"));
    parent.append(heading);
    if (evidence.export_id) parent.append(node("div", `导出 ID · ${evidence.export_id}`, "operation-id"));
    const checks = node("div", undefined, "report-checks");
    [["结构正确", "structure_ok"], ["A · 分录与余额一致", "projection_ok"], ["B · 分录符合保存的请求", "request_intent_ok"], ["没有非零预留 · 支持重建", "reservation_supported"]].forEach(([name, key]) => {
      const row = node("div", undefined, "check-line");
      append(row, node("span", name), node("strong", report[key] ? "PASS" : "FAIL", report[key] ? "" : "fail"));
      checks.append(row);
    });
    parent.append(checks);
    (report.issues || []).forEach((issue) => {
      const item = node("div", undefined, "issue");
      append(item, node("h4", issue.code), node("div", `${accountLabel(issue.object_id)} · ${issue.field}`), node("div", `期望 ${typeof issue.expected === "object" ? json(issue.expected) : issue.expected} / 实际 ${typeof issue.actual === "object" ? json(issue.actual) : issue.actual}`));
      parent.append(item);
    });
    if (report.unverified_intent_kinds?.length) parent.append(node("p", `意图未核实类型：${report.unverified_intent_kinds.join("、")}。合成开户只核对结构，不能证明真实入金。`, "table-note"));
    parent.append(disclosure("展开完整核对报告", node("pre", json(report), "json-block")));
  }

  function renderVerification() {
    const panel = $("panel-verification");
    clear(panel);
    if (!state.verification && !state.preview && !state.repairs.length && !state.maintenance_freezes.length) return empty(panel, "先导出，再让独立程序核对", "核对 A 检查投影；核对 B 检查保存的业务意图。报告没有生成前，这里不会预填“通过”。");
    renderReport(panel, state.verification, "离线核对报告");
    if (state.preview) {
      const preview = state.preview;
      panel.append(node("h3", "重建预览", "source-heading"));
      if (preview.stale) panel.append(node("span", "旧预览 · 不是当前写入来源", "stale-tag"));
      if (preview.changes?.length) panel.append(table(["账户", "改前余额", "分录重算", "原版本"], preview.changes.map((change) => [accountLabel(change.account_id), money(change.before?.booked), money(change.after?.booked), change.before?.version ?? "—"]), [1, 2, 3]));
      else panel.append(node("p", "本次预览没有投影差异。", "table-note"));
      panel.append(disclosure("预览原始内容与准入检查", node("pre", json(preview), "json-block")));
    }
    if (state.repairs.length) {
      panel.append(node("h3", "实际修复记录", "source-heading"));
      state.repairs.forEach((repair) => {
        const item = node("div", undefined, "repair-row");
        append(item, node("strong", `投影代次 ${repair.old_epoch} → ${repair.new_epoch} · ${repair.account_count} 个账户`), node("div", repair.repair_id, "operation-id"));
        const rows = state.repair_rows.filter((row) => row.repair_id === repair.repair_id);
        item.append(table(["账户", "修复前", "修复后", "行版本"], rows.map((row) => [accountLabel(row.account_id), money(row.before_booked), money(row.after_booked), `${row.before_version ?? "无"} → ${row.after_version}`]), [1, 2, 3]));
        item.append(disclosure("修复元数据及改前 / 改后完整值", node("pre", json({ repair, rows }), "json-block")));
        panel.append(item);
      });
    }
    if (state.maintenance_freezes.length) {
      panel.append(node("h3", "冻结与恢复记录", "source-heading"));
      state.maintenance_freezes.forEach((freeze) => {
        const item = node("div", undefined, "repair-row");
        append(item, node("strong", freeze.resumed_at ? "已显式恢复 NORMAL" : "仍在 FROZEN"), node("p", freeze.reason, "table-note"));
        item.append(disclosure("冻结信息 / 恢复现场核对报告", node("pre", json(freeze), "json-block")));
        panel.append(item);
      });
    }
  }

  function renderAccounts() {
    const panel = $("panel-accounts");
    clear(panel);
    if (!state.accounts.length) return empty(panel, "空工作区", "账户尚未建立。执行第一步后，四个合成账户和实际余额行会出现在这里。");
    panel.append(table(["账户", "正常方向", "借方累计", "贷方累计", "账面余额", "预留", "可用", "版本"], state.accounts.map((account) => {
      const balance = state.balances.find((row) => row.account_id === account.id);
      return [accountLabel(account.id), account.normal_side, money(balance?.debits), money(balance?.credits), money(balance?.booked), money(balance?.reserved), money(balance?.available), balance?.version ?? "—"];
    }), [2, 3, 4, 5, 6, 7]));
    panel.append(node("p", "资产：booked = debits − credits；负债：booked = credits − debits；available = booked − reserved。商户 M2 用于自由实验。", "table-note"));
    state.accounts.forEach((account) => {
      const body = keyValues([["账户 ID", account.id, true], ["Owner ID", account.owner_id, true], ["类型", account.class, true], ["币种", account.currency], ["启用", account.enabled ? "是" : "否"]]);
      panel.append(disclosure(`${accountLabel(account.id)} · 身份与类型`, body));
    });
  }

  function renderHistory() {
    const list = $("event-list");
    clear(list);
    if (!state.events.length) return empty(list, "从第一步开始", "这里记录实际调用，不预填演示结果。");
    [...state.events].reverse().forEach((event) => {
      const row = node("li");
      const button = node("button", `${event.title}${event.http_status ? ` · ${event.http_status}` : ""}${event.replayed ? " · 重放" : ""}`, "history-button event-copy");
      button.type = "button";
      button.addEventListener("click", () => {
        selectedEventId = event.id;
        readingNumber = event.guide_step || actionLessons[event.action] || readingNumber;
        renderNavigation(); renderResult(); renderReading(); renderJournals();
        setReadingTab("process");
        $("last-result").scrollIntoView({ behavior: scrollBehavior(), block: "nearest" });
      });
      append(row, node("span", String(event.id).padStart(2, "0"), "event-index"), button, node("span", time(event.at), "event-time"));
      list.append(row);
    });
  }

  function renderReading() {
    const item = lesson();
    if (!item) return;
    const status = readingNumber <= state.guide.index ? "已完成 · 可回看" : readingNumber === state.guide.index + 1 ? "当前待执行" : "预习";
    $("reading-label").textContent = `第 ${readingNumber} 步 · ${status}`;
    const why = $("panel-why");
    clear(why);
    append(why, node("h3", item.title), node("p", item.lead, "lead"));
    item.sections.forEach((section) => {
      append(why, node("h4", section.heading), node("p", section.body));
      const details = node("div");
      append(details, node("p", section.enforcement), node("p", section.source, "source-path"));
      why.append(disclosure("谁保证这条规则 · 对应代码", details));
    });
    item.details.forEach((detail) => why.append(disclosure(detail.summary, detail.body)));
    renderTrace();
    const code = $("panel-code");
    clear(code);
    append(code, node("h3", "从这一段代码开始"), node("p", item.code.focus, "lead"));
    const links = node("div", undefined, "source-links");
    item.code.keys.forEach((key) => {
      const button = node("button", sourceLabel(key));
      button.type = "button";
      button.dataset.source = key;
      button.addEventListener("click", () => showSource(key));
      links.append(button);
    });
    code.append(links);
    const source = node("div");
    source.id = "source-container";
    code.append(source);
    item.code.interview.forEach((detail) => code.append(disclosure(`面试深挖 · ${detail.title}`, detail.body)));
    code.append(node("p", "这里提供回答依据与边界，不设置测验，也不把浏览器这一轮运行当作全部测试证据。", "protocol-note"));
    sourceVersion += 1;
    if (readingTab === "code") showSource(item.code.key);
  }

  function sourceLabel(key) {
    return { purchase: "事务入口", transfer: "写分录 / 投影", balance_lock: "余额锁", replay: "重放", reversal: "冲正", freeze: "冻结屏障", repair: "现场重建", resume: "恢复", verify: "独立核对", export: "一致快照", schema: "数据库约束" }[key] || key;
  }
  async function showSource(key) {
    const version = ++sourceVersion;
    const container = $("source-container");
    if (!container) return;
    container.textContent = "正在读取项目中的实际源码…";
    document.querySelectorAll("[data-source]").forEach((button) => button.classList.toggle("active", button.dataset.source === key));
    try {
      let source = sourceCache.get(key);
      if (!source) {
        const response = await fetch(`/api/source?key=${encodeURIComponent(key)}`, { cache: "no-store" });
        if (!response.ok) throw new Error("源码读取失败");
        source = await response.json();
        sourceCache.set(key, source);
      }
      if (version !== sourceVersion || !container.isConnected) return;
      clear(container);
      append(container, node("h4", source.title || sourceLabel(key), "source-heading"), node("p", `${source.path}:${source.start_line} · 实际文件，只读展示`, "source-path"));
      const pre = node("pre", undefined, "source-box");
      const text = String(source.text || "").split("\n").map((line, index) => `${String(Number(source.start_line) + index).padStart(4, " ")}  ${line}`).join("\n");
      pre.append(node("code", text));
      container.append(pre);
    } catch (error) {
      if (version === sourceVersion) container.textContent = error.message;
    }
  }

  function traceContent(event, wide = false) {
    const container = node("div");
    if (!event) {
      empty(container, "这一步还没有执行记录", "执行操作后，这里才显示后端实际采集的 SQL、提交或回滚及代码位置。没有执行就不生成追踪。");
      return container;
    }
    append(container, node("h3", `操作 #${event.id} · ${event.title}`), node("p", `实际连接角色：${event.role || "—"}。以下记录 Python 客户端发出的 SQL 命令及事务边界，不包含触发器内部 SQL 或锁等待因果。页面刷新读取不混入本次追踪。`, "trace-intro"));
    const trace = event.trace || [];
    if (!trace.length) {
      empty(container, "本次没有采集到 SQL", "这可能是进入数据库前的输入校验或离线阶段。这里不会用手写流程补成“真实执行”。");
      return container;
    }
    const errors = trace.filter((row) => row.sqlstate || row.status === "error").length;
    const attempts = new Set(trace.map((row) => row.attempt).filter((attempt) => attempt !== undefined && attempt !== null));
    container.append(node("p", `${trace.length} 条实际数据库记录${errors ? ` · ${errors} 条错误记录` : ""}${attempts.size ? ` · ${attempts.size} 个实际 attempt 标记` : ""}。SQL 耗时来自本地追踪，不代表生产性能。`, "protocol-note"));
    trace.forEach((entry, index) => {
      const details = node("details", undefined, "trace-entry");
      const sql = String(entry.sql || "");
      const boundary = /^(BEGIN|COMMIT|ROLLBACK|SAVEPOINT|RELEASE)/i.test(sql.trim());
      if (boundary) details.classList.add("boundary");
      if (entry.sqlstate || entry.status === "error") details.classList.add("trace-error");
      details.open = wide || boundary || index === 0;
      const summary = node("summary");
      append(summary, node("span", String(entry.index ?? index + 1).padStart(2, "0"), "trace-index"), node("span", sql.replace(/\s+/g, " "), "trace-sql-title"));
      const duration = typeof entry.duration_ms === "number" ? `${entry.duration_ms.toFixed(2)} ms` : entry.duration_ms !== undefined ? `${entry.duration_ms} ms` : "—";
      summary.append(node("span", duration, "trace-duration"));
      details.append(summary);
      details.append(node("pre", sql, "trace-sql"));
      const meta = [entry.status, entry.connection ? `连接 ${entry.connection}` : null, entry.role, entry.attempt !== undefined && entry.attempt !== null ? `attempt ${entry.attempt}` : null, entry.sqlstate ? `SQLSTATE ${entry.sqlstate}` : null].filter(Boolean).join(" · ");
      details.append(node("p", meta, "source-path"));
      if (entry.params !== undefined && entry.params !== null) details.append(disclosure("绑定参数", node("pre", typeof entry.params === "string" ? entry.params : json(entry.params), "json-block")));
      if (entry.code) details.append(node("p", `${entry.code.path || ""}:${entry.code.line ?? ""} · ${entry.code.function || ""}`, "source-path"));
      container.append(details);
    });
    return container;
  }
  function renderTrace() {
    const panel = $("panel-process");
    clear(panel);
    const event = activeEvent();
    panel.append(traceContent(event));
    if (event?.trace?.length) {
      const button = node("button", "宽屏查看完整实际 SQL ↗", "button secondary trace-expand");
      button.type = "button";
      button.addEventListener("click", () => {
        const dialog = node("dialog", undefined, "trace-dialog");
        dialog.setAttribute("aria-label", "实际 SQL 执行追踪");
        const heading = node("div", undefined, "dialog-heading");
        heading.append(node("h2", "真实 SQL 执行顺序"));
        const close = node("button", "×", "icon-button");
        close.type = "button";
        close.setAttribute("aria-label", "关闭 SQL 追踪");
        close.addEventListener("click", () => dialog.close());
        heading.append(close);
        append(dialog, heading, traceContent(event, true));
        dialog.addEventListener("close", () => dialog.remove());
        document.body.append(dialog);
        dialog.showModal();
      });
      panel.append(button);
    }
  }

  function renderGlossary() {
    const container = $("glossary-content");
    clear(container);
    content.glossary.forEach((entry) => {
      const item = node("div", undefined, "glossary-entry");
      const heading = node("h3", entry.term);
      heading.append(node("span", entry.en));
      append(item, heading, node("p", entry.body), node("div", entry.source, "code-map"));
      container.append(item);
    });
  }

  function setBusy(value) {
    busy = value;
    const unavailable = !state || busy || ended;
    document.querySelectorAll("[data-action], #purchase-form button, #reset, #shutdown").forEach((button) => { button.disabled = unavailable; });
    $("execute-next").disabled = unavailable || state?.guide?.index >= 12 || state?.guide?.customized || auto;
    $("auto-demo").disabled = ended || !state || (!auto && (busy || state.guide.index >= 12 || state.guide.customized));
    $("auto-demo").textContent = auto ? "Ⅱ 暂停自动演示" : "▷ 自动演示";
    if (state) $("next-label").textContent = busy ? "正在执行…" : state.guide.index >= 12 ? "十二步已完成" : "执行这一步";
    $("purchase-form").setAttribute("aria-busy", String(busy));
    $("execute-next").setAttribute("aria-busy", String(busy));
  }

  function render() {
    $("database-state").replaceChildren(node("span", undefined, "live-dot"), document.createTextNode("真实 PostgreSQL · 临时库"));
    $("workspace-mode").textContent = `模式 ${state.workspace?.mode || "—"}`;
    $("projection-epoch").textContent = `投影代次 ${state.workspace?.projection_epoch ?? "—"}`;
    $("evidence-count").textContent = `${state.journals.length} 张分录 · ${state.operations.length} 笔业务`;
    renderNavigation(); renderBalances(); renderGuide(); renderResult(); renderJournals(); renderOperations(); renderVerification(); renderAccounts(); renderHistory(); renderReading();
    setBusy(busy);
  }

  async function action(actionName, extra = {}) {
    if (busy || ended || !state) return false;
    if (!auto) notice("");
    setBusy(true);
    try {
      const response = await fetch("/api/action", { method: "POST", cache: "no-store", headers: { "Content-Type": "application/json", "X-Ledger-Lab": state.token }, body: json({ action: actionName, ...extra }) });
      const data = await response.json();
      if (actionName === "shutdown" && response.ok) {
        ended = true; auto = false;
        $("database-state").textContent = "实验已结束";
        notice("实验已结束，服务正在清理临时 PostgreSQL 与导出资源。项目源代码不会被删除；重新运行 make lab 可开启新实验。");
        $("next-label").textContent = "实验已结束";
        return true;
      }
      const previousCount = state.guide.index;
      if (data.state) state = data.state;
      else if (data.workspace && data.guide) state = data;
      if (actionName === "reset" && response.ok) {
        readingNumber = 1; selectedEventId = null; auto = false;
        $("free-lab").open = false;
      } else {
        selectedEventId = state.last_event?.id ?? null;
        readingNumber = actionName === "next" ? Math.min(previousCount + 1, 12) : actionLessons[actionName] || readingNumber;
      }
      render();
      if (!response.ok) {
        auto = false;
        notice(`${data.error?.code || response.status} · ${data.error?.message || "操作未完成，请查看实际结果与执行过程。"}`, true);
        return false;
      }
      if (actionName === "reset") notice("已建立新的空工作区。第一步执行前，没有预填余额或分录。");
      return true;
    } catch (error) {
      auto = false;
      notice(`无法确认本次请求结果：${error.message}。若已经提交，页面不能凭网络错误判定未入账；请保留原业务身份，重新打开页面查看或使用原身份重放。`, true);
      return false;
    } finally { setBusy(false); }
  }

  function setReadingTab(name) {
    readingTab = name;
    document.querySelectorAll("[data-reading]").forEach((button) => {
      const selected = button.dataset.reading === name;
      button.setAttribute("aria-selected", String(selected)); button.tabIndex = selected ? 0 : -1;
      $(`panel-${button.dataset.reading}`).hidden = !selected;
    });
    if (name === "code") showSource(lesson().code.key);
  }
  function setEvidenceTab(name) {
    evidenceTab = name;
    document.querySelectorAll("[data-evidence]").forEach((button) => {
      const selected = button.dataset.evidence === name;
      button.setAttribute("aria-selected", String(selected)); button.tabIndex = selected ? 0 : -1;
      $(`panel-${button.dataset.evidence}`).hidden = !selected;
    });
  }
  function keyboardTabs(selector, activate, field) {
    document.querySelectorAll(selector).forEach((button, index, list) => {
      button.addEventListener("click", () => activate(button.dataset[field]));
      button.addEventListener("keydown", (event) => {
        let next;
        if (event.key === "ArrowRight") next = (index + 1) % list.length;
        if (event.key === "ArrowLeft") next = (index + list.length - 1) % list.length;
        if (event.key === "Home") next = 0;
        if (event.key === "End") next = list.length - 1;
        if (next === undefined) return;
        event.preventDefault(); activate(list[next].dataset[field]); list[next].focus();
      });
    });
  }
  function amount() {
    const value = $("amount").value.trim();
    if (!/^[1-9][0-9]*$/.test(value)) throw new Error("金额必须是正整数最小货币单位，例如 20；不要输入小数、负数或科学计数法。");
    return value;
  }

  $("execute-next").addEventListener("click", () => action("next"));
  $("reset").addEventListener("click", () => { auto = false; action("reset"); });
  $("shutdown").addEventListener("click", () => { auto = false; action("shutdown"); });
  $("auto-demo").addEventListener("click", async () => {
    if (auto) { auto = false; setBusy(busy); return; }
    auto = true; setBusy(false);
    while (auto && !ended && state.guide.index < 12 && !state.guide.customized) {
      const ok = await action("next");
      if (!ok) break;
      await new Promise((resolve) => setTimeout(resolve, 1600));
    }
    auto = false; setBusy(busy);
  });
  $("purchase-form").addEventListener("submit", (event) => {
    event.preventDefault();
    try { action("purchase", { amount_minor: amount(), merchant: $("merchant").value }); }
    catch (error) { notice(error.message, true); $("amount").focus(); }
  });
  document.querySelectorAll("[data-action]").forEach((button) => button.addEventListener("click", () => {
    try { action(button.dataset.action, button.dataset.action === "conflict_purchase" ? { amount_minor: amount() } : {}); }
    catch (error) { notice(error.message, true); }
  }));
  $("show-accounts").addEventListener("click", () => { setEvidenceTab("accounts"); $("evidence-title").scrollIntoView({ behavior: scrollBehavior(), block: "start" }); });
  $("open-glossary").addEventListener("click", () => $("glossary-dialog").showModal());
  $("close-glossary").addEventListener("click", () => $("glossary-dialog").close());
  keyboardTabs("[data-reading]", setReadingTab, "reading");
  keyboardTabs("[data-evidence]", setEvidenceTab, "evidence");

  async function start() {
    setBusy(true);
    const results = await Promise.allSettled([fetch("/api/state", { cache: "no-store" }).then(async (response) => { if (!response.ok) throw new Error("读取实验状态失败"); return response.json(); }), fetch("/lessons.json", { cache: "no-store" }).then(async (response) => { if (!response.ok) throw new Error("读取课程内容失败"); return response.json(); })]);
    const failed = results.find((result) => result.status === "rejected");
    if (failed) {
      notice(`无法初始化学习工作台：${failed.reason.message}。请确认 make lab 仍在运行，然后刷新。`, true);
      $("database-state").textContent = "未连接";
      $("lesson-title").textContent = "实验室尚未连接";
      setBusy(false); return;
    }
    state = results[0].value; content = results[1].value;
    selectedEventId = state.last_event?.id ?? null;
    readingNumber = Math.min(Math.max(state.guide.index, 1), 12);
    renderGlossary(); render(); setBusy(false);
  }
  start();
})();
