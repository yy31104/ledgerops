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
  el("credential").value = ""; el("identity").textContent = "Not connected";
  el("accounts").replaceChildren(); el("case-summary").replaceChildren();
  el("case-json").textContent = ""; el("case-details").hidden = true;
  el("metrics-result").textContent = "Not queried"; el("more").hidden = true;
  for (const id of ["operator-form", "action-lookup"]) el(id).reset();
  el("action-id").value = ""; el("proposal-json").textContent = "No proposal loaded";
  el("proposal-review").textContent = "No proposal loaded"; el("action-history").textContent = "Not queried";
  enabled();
}
async function request(path, payload = null) {
  const generation = sessionGeneration;
  const headers = {Authorization: "Bearer " + token};
  const options = {headers, cache: "no-store", credentials: "omit"};
  if (payload !== null) { options.method = "POST"; headers["Content-Type"] = "application/json"; options.body = JSON.stringify(payload); }
  const response = await fetch(path, options);
  const value = await response.json();
  if (generation !== sessionGeneration) throw new Error("The session changed. Connect again.");
  if (!response.ok) { if (response.status === 401) disconnect(); throw new Error((value.error?.code || value.code || "REQUEST_FAILED") + " \u00b7 request ID " + (response.headers.get("X-Request-ID") || "unknown")); }
  return value;
}
async function accounts(after = null) {
  const data = await request("/v1/accounts" + (after ? "?after=" + encodeURIComponent(after) : ""));
  const body = el("accounts"); body.replaceChildren();
  for (const account of data.accounts) { const row = document.createElement("tr"); for (const key of ["id", "class", "booked", "reserved", "available", "version"]) { const cell = document.createElement("td"); cell.textContent = String(account[key]); row.append(cell); } body.append(row); }
  if (!data.accounts.length) { const row = document.createElement("tr"); const cell = document.createElement("td"); cell.colSpan = 6; cell.textContent = "No accounts are available to this identity."; row.append(cell); body.append(row); }
  nextAccount = data.next_after; el("more").hidden = !nextAccount;
}
function uuid(value) { if (!/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/.test(value)) throw new Error("Enter a complete lowercase UUID."); return value; }
async function showAction(data) {
  const generation = sessionGeneration;
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(data.proposal_json));
  if (generation !== sessionGeneration) throw new Error("The session changed. Connect again.");
  const hash = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
  if (hash !== data.proposal_sha256) throw new Error("The proposal digest does not match. Approval is blocked.");
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
  try { identity = await request("/v1/me"); el("identity").textContent = "Workspace " + identity.workspace_id + " \u00b7 actor " + identity.actor_id + " \u00b7 permissions " + identity.scopes.join(", "); enabled(); if (has("accounts:read")) await accounts(); message("Connected. Controls follow this identity's current permissions."); }
  catch (error) { disconnect(); message(error.message, true); }
});
el("logout").addEventListener("click", () => { disconnect(); message("Credentials and proposals have been cleared from this page."); });
el("refresh").addEventListener("click", async () => { try { await accounts(); message("Balances refreshed."); } catch (error) { message(error.message, true); } });
el("more").addEventListener("click", async () => { try { await accounts(nextAccount); message("Next page loaded."); } catch (error) { message(error.message, true); } });
el("lookup").addEventListener("submit", async (event) => {
  event.preventDefault();
  try { const data = await request("/v1/investigation/operations/" + uuid(el("operation").value.trim())); const dl = document.createElement("dl"); const fields = [["Operation type", data.operation.kind], ["Admission result", data.operation.admission], ["HTTP status", data.operation.http_status], ["Original request digest", data.operation.payload_sha256], ["Journal entry count", data.entries.length], ["Payout knowledge state", data.payout?.knowledge || "Not applicable"]]; for (const [name, value] of fields) { const dt = document.createElement("dt"); const dd = document.createElement("dd"); dt.textContent = name; dd.textContent = String(value); dl.append(dt, dd); } el("case-summary").replaceChildren(dl); el("case-json").textContent = JSON.stringify(data, null, 2); el("case-details").hidden = false; message("Operation snapshot loaded. Evidence references are in the full result; proposal creation rechecks the actual files."); }
  catch (error) { message(error.message, true); }
});
el("metrics").addEventListener("click", async () => { try { el("metrics-result").textContent = JSON.stringify(await request("/v1/metrics"), null, 2); message("Workspace status loaded."); } catch (error) { message(error.message, true); } });
el("new-action").addEventListener("click", () => { el("action-id").value = crypto.randomUUID(); message("New proposal ID generated. Keep it for retries; use a new ID when the intended action changes."); enabled(); });
el("operator-form").addEventListener("submit", (event) => {
  event.preventDefault();
  operatorCall(async () => {
    const data = await request("/v1/operator/actions", {action_id: uuid(el("action-id").value), action_type: "REPROCESS_TERMINAL_LOCAL_BLOCKER", payout_id: uuid(el("action-payout").value.trim()), observation_id: uuid(el("action-observation").value.trim()), evidence_run_id: uuid(el("action-evidence").value.trim()), reason: el("action-reason").value.trim(), ttl_seconds: 900});
    await showAction(data); message("Proposal retained. Give its ID to another reviewer so they can read and approve the exact proposal.");
  });
});
el("action-lookup").addEventListener("submit", (event) => { event.preventDefault(); operatorCall(async () => { await showAction(await request("/v1/operator/actions/" + uuid(el("action-reference").value.trim()))); message("Proposal digest verified. Approval and execution recheck current evidence and permissions."); }); });
el("approve-action").addEventListener("click", () => operatorCall(async () => {
  const data = operatorProposal;
  await request("/v1/operator/actions/" + data.action_id + "/approve", {proposal_sha256: data.proposal_sha256});
  await showAction(await request("/v1/operator/actions/" + data.action_id)); message("Approval recorded. No funds have moved.");
}));
el("execute-action").addEventListener("click", () => operatorCall(async () => {
  const data = operatorProposal;
  await request("/v1/operator/actions/" + data.action_id + "/execute", {proposal_sha256: data.proposal_sha256, execution_id: executionId});
  await showAction(await request("/v1/operator/actions/" + data.action_id)); message("Retained execution result loaded. After disconnection or temporary unavailability, retry with the same proposal and execution IDs.");
}));
fetch("/health/ready", {cache: "no-store", credentials: "omit"}).then((response) => { el("health").textContent = response.ok ? "Service ready" : "Financial writes are disabled"; }).catch(() => { el("health").textContent = "Service unavailable"; });
window.addEventListener("pagehide", disconnect);
