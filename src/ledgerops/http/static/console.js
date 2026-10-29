"use strict";
let token = "";
let sessionGeneration = 0;
let nextAccount = null;
let identity = null;
let operatorProposal = null;
let operatorBusy = false;
let executionId = null;
const el = (id) => document.getElementById(id);
const has = (scope) => identity?.scopes.includes(scope) === true;
function message(text, error = false) { el("notice").textContent = text; el("notice").className = error ? "error" : ""; }
function enabled() {
  el("refresh").disabled = !has("accounts:read");
  for (const id of ["inspect", "metrics"]) el(id).disabled = !has("evidence:read");
  el("logout").hidden = !identity;
  const available = identity?.operator_controls_available === true && !operatorBusy;
  el("propose-action").disabled = !available || !has("approval:propose") || !el("action-id").value;
  el("new-action").disabled = !available || !has("approval:propose");
  el("load-action").disabled = !available || !has("evidence:read");
  el("approve-action").disabled = !available || !has("approval:approve") || !operatorProposal || operatorProposal.review.maker_actor_id === identity.actor_id || !!operatorProposal.execution;
  el("execute-action").disabled = !available || !has("approval:execute") || !operatorProposal?.approval || !!operatorProposal.execution;
}
function disconnect() {
  sessionGeneration += 1; token = ""; identity = null; nextAccount = null;
  operatorProposal = null; executionId = null; operatorBusy = false;
  el("credential").value = ""; el("identity").textContent = "尚未连接";
  el("accounts").replaceChildren(); el("case-summary").replaceChildren();
  el("case-json").textContent = ""; el("case-details").hidden = true;
  el("metrics-result").textContent = "尚未查询"; el("more").hidden = true;
  for (const id of ["operator-form", "action-lookup"]) el(id).reset();
  el("action-id").value = ""; el("proposal-json").textContent = "尚未读取提案";
  el("proposal-review").textContent = "尚未读取提案"; el("action-history").textContent = "尚未查询";
  enabled();
}
async function request(path, payload = null) {
  const generation = sessionGeneration;
  const headers = {Authorization: "Bearer " + token};
  const options = {headers, cache: "no-store", credentials: "omit"};
  if (payload !== null) { options.method = "POST"; headers["Content-Type"] = "application/json"; options.body = JSON.stringify(payload); }
  const response = await fetch(path, options);
  const value = await response.json();
  if (generation !== sessionGeneration) throw new Error("会话已改变，请重新连接。");
  if (!response.ok) { if (response.status === 401) disconnect(); throw new Error((value.error?.code || value.code || "REQUEST_FAILED") + " · 请求编号 " + (response.headers.get("X-Request-ID") || "未知")); }
  return value;
}
async function accounts(after = null) {
  const data = await request("/v1/accounts" + (after ? "?after=" + encodeURIComponent(after) : ""));
  const body = el("accounts"); body.replaceChildren();
  for (const account of data.accounts) { const row = document.createElement("tr"); for (const key of ["id", "class", "booked", "reserved", "available", "version"]) { const cell = document.createElement("td"); cell.textContent = String(account[key]); row.append(cell); } body.append(row); }
  if (!data.accounts.length) { const row = document.createElement("tr"); const cell = document.createElement("td"); cell.colSpan = 6; cell.textContent = "当前身份没有可显示的账户。"; row.append(cell); body.append(row); }
  nextAccount = data.next_after; el("more").hidden = !nextAccount;
}
function uuid(value) { if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value)) throw new Error("请输入完整的小写 UUID。"); return value; }
async function showAction(data) {
  const generation = sessionGeneration;
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(data.proposal_json));
  if (generation !== sessionGeneration) throw new Error("会话已改变，请重新连接。");
  const hash = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
  if (hash !== data.proposal_sha256) throw new Error("提案原文摘要不符，已阻止审批。");
  if (operatorProposal?.action_id !== data.action_id) executionId = crypto.randomUUID();
  operatorProposal = data;
  el("action-reference").value = data.action_id;
  el("proposal-review").textContent = JSON.stringify({action_id: data.action_id, proposal_sha256: hash, ...data.review}, null, 2);
  // Do not parse and reserialize authoritative JSON: large integers must remain exact.
  el("proposal-json").textContent = data.proposal_json;
  el("action-history").textContent = JSON.stringify({approval: data.approval || null, execution: data.execution || null, attempts: data.attempts || [], attempts_have_more: data.attempts_have_more || false}, null, 2);
}
async function operatorCall(work) {
  if (operatorBusy) return;
  operatorBusy = true; enabled();
  try { await work(); } catch (error) { message(error.message, true); }
  finally { operatorBusy = false; enabled(); }
}
el("login").addEventListener("submit", async (event) => {
  event.preventDefault(); const entered = el("credential").value; disconnect(); token = entered;
  try { identity = await request("/v1/me"); el("identity").textContent = "工作区 " + identity.workspace_id + " · 身份 " + identity.actor_id + " · 权限 " + identity.scopes.join(", "); enabled(); if (has("accounts:read")) await accounts(); message("已连接；只开放当前身份有权使用的功能。"); }
  catch (error) { disconnect(); message(error.message, true); }
});
el("logout").addEventListener("click", () => { disconnect(); message("本页凭据和提案已清除。"); });
el("refresh").addEventListener("click", async () => { try { await accounts(); message("余额已刷新。"); } catch (error) { message(error.message, true); } });
el("more").addEventListener("click", async () => { try { await accounts(nextAccount); message("已显示下一页。"); } catch (error) { message(error.message, true); } });
el("lookup").addEventListener("submit", async (event) => {
  event.preventDefault();
  try { const data = await request("/v1/investigation/operations/" + uuid(el("operation").value.trim())); const dl = document.createElement("dl"); const fields = [["业务类型", data.operation.kind], ["受理结果", data.operation.admission], ["HTTP 结果", data.operation.http_status], ["原始请求摘要", data.operation.payload_sha256], ["分录行数", data.entries.length], ["提现知识状态", data.payout?.knowledge || "不适用"]]; for (const [name, value] of fields) { const dt = document.createElement("dt"); const dd = document.createElement("dd"); dt.textContent = name; dd.textContent = String(value); dl.append(dt, dd); } el("case-summary").replaceChildren(dl); el("case-json").textContent = JSON.stringify(data, null, 2); el("case-details").hidden = false; message("已读取业务快照；证据版本引用见完整结果，提案入口会重新核验实际文件。"); }
  catch (error) { message(error.message, true); }
});
el("metrics").addEventListener("click", async () => { try { el("metrics-result").textContent = JSON.stringify(await request("/v1/metrics"), null, 2); message("工作区状态已读取。"); } catch (error) { message(error.message, true); } });
el("new-action").addEventListener("click", () => { el("action-id").value = crypto.randomUUID(); message("已生成新提案编号。失败重试保留此编号；改变意图时再生成新的编号。"); enabled(); });
el("operator-form").addEventListener("submit", (event) => {
  event.preventDefault();
  operatorCall(async () => {
    const data = await request("/v1/operator/actions", {action_id: uuid(el("action-id").value), action_type: "REPROCESS_TERMINAL_LOCAL_BLOCKER", payout_id: uuid(el("action-payout").value.trim()), observation_id: uuid(el("action-observation").value.trim()), evidence_run_id: uuid(el("action-evidence").value.trim()), reason: el("action-reason").value.trim(), ttl_seconds: 900});
    await showAction(data); message("提案已保留。复制提案编号，供另一位审核人读取原文后审批。");
  });
});
el("action-lookup").addEventListener("submit", (event) => { event.preventDefault(); operatorCall(async () => { await showAction(await request("/v1/operator/actions/" + uuid(el("action-reference").value.trim()))); message("提案原文摘要已核对；当前证据与权限会在审批和执行时重新检查。"); }); });
el("approve-action").addEventListener("click", () => operatorCall(async () => {
  const data = operatorProposal;
  await request("/v1/operator/actions/" + data.action_id + "/approve", {proposal_sha256: data.proposal_sha256});
  await showAction(await request("/v1/operator/actions/" + data.action_id)); message("审批已记录，资金尚未改变。");
}));
el("execute-action").addEventListener("click", () => operatorCall(async () => {
  const data = operatorProposal;
  await request("/v1/operator/actions/" + data.action_id + "/execute", {proposal_sha256: data.proposal_sha256, execution_id: executionId});
  await showAction(await request("/v1/operator/actions/" + data.action_id)); message("已读取保留的执行结果。断线或暂不可用时，保留同一提案与执行编号重试。");
}));
fetch("/health/ready", {cache: "no-store", credentials: "omit"}).then((response) => { el("health").textContent = response.ok ? "服务已就绪" : "金融写入尚未开放"; }).catch(() => { el("health").textContent = "服务暂不可达"; });
window.addEventListener("pagehide", disconnect);
