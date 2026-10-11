"""Builds the n8n workflows as code and pushes them through the n8n public API.

    $py = ".\\screener-api\\.venv\\Scripts\\python.exe"
    & $py scripts\\n8n_workflows.py build      # write n8n/workflows/*.json only
    & $py scripts\\n8n_workflows.py push       # build + create/update in n8n (needs N8N_API_KEY)
    & $py scripts\\n8n_workflows.py activate   # push + activate (needs credentials connected)

Why code instead of clicking? The workflows are reviewable in git, identical on every
machine, and n8n validates every node when we push. Secrets never appear in the JSON:
nodes read them from environment variables ($env.X) or from n8n's encrypted credentials.

Google Sheet layout (tab "Candidates", row 1 = headers):
  candidate_id, name, phone, email, role, status, interview_link, call_time, score,
  ai_summary, decision, interview_datetime, meet_link, updated_at, thread_id, flags
Status flow: New -> Invited -> (Reminded) -> Called -> Scheduled | Shortlisted | Rejected |
             Human Review | Incomplete -> Re-invited | No response | Invite failed | Error
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "n8n" / "workflows"
N8N = "http://localhost:5678"
SHEET_TAB = "Candidates"
COLUMNS = [
    "candidate_id", "name", "phone", "email", "role", "status", "interview_link", "call_time",
    "score", "ai_summary", "decision", "interview_datetime", "meet_link", "updated_at",
    "thread_id", "flags",
]
CRED_SHEETS = "Google Sheets (screener)"
CRED_GMAIL = "Gmail (screener)"

# ----------------------------------------------------------------------------- JS helpers
# Shared JavaScript pasted into Code nodes. Kept small and dependency-free.
JS_HELPERS = r"""
const API = $env.SCREENER_API_URL;
// WhatsApp wants digits only, with country code: 0300... -> 92300...
function waNumber(p) {
  let d = String(p || '').replace(/\D/g, '');
  if (d.startsWith('00')) d = d.slice(2);
  if (d.startsWith('0') && d.length === 11) d = '92' + d.slice(1);
  return d;
}
// Template parameters can't contain newlines/tabs or 4+ spaces, and have length limits
function clean(t, max = 900) {
  return String(t || '').replace(/[\r\n\t]+/g, ' ').replace(/ {2,}/g, ' ').trim().slice(0, max);
}
// Our drafted messages already greet/sign; the templates add their own greeting/signature
function body(t) {
  return clean(String(t || '')
    .replace(/^\s*(hi|hello|dear)\s+[^,!\n]*[,!]\s*/i, '')
    .replace(/[-–—]?\s*(warm(ly)?|best( wishes)?|regards|wishing you[^,\n]*)?[,\s]*the hiring team\.?\s*$/i, ''));
}
function template(to, name, params) {
  return { messaging_product: 'whatsapp', to, type: 'template',
    template: { name, language: { code: $env.WA_TEMPLATE_LANG || 'en' },
      components: [{ type: 'body', parameters: params.map(p => ({ type: 'text', text: clean(p) || '-' })) }] } };
}
function text(to, msg) {
  return { messaging_product: 'whatsapp', to, type: 'text', text: { body: String(msg).slice(0, 4000) } };
}
function firstName(n) { return String(n || 'there').trim().split(/\s+/)[0]; }
function pkt(iso) {
  return new Date(iso).toLocaleString('en-GB', { timeZone: 'Asia/Karachi', weekday: 'short', day: 'numeric',
    month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit' }) + ' PKT';
}
const NOW = new Date().toISOString();
"""


# ----------------------------------------------------------------------------- node builders
class WF:
    """Tiny helper to build an n8n workflow: nodes + connections by node name."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.nodes: list[dict] = []
        self.connections: dict[str, dict] = {}
        self._x = 0

    def add(self, name: str, type_: str, version: float, params: dict, *, pos: tuple[int, int] | None = None,
            creds: dict | None = None, **extra) -> str:
        if pos is None:
            self._x += 240
            pos = (self._x, 300)
        node = {"id": str(uuid.uuid4()), "name": name, "type": type_, "typeVersion": version,
                "position": list(pos), "parameters": params, **extra}
        if creds:
            node["credentials"] = creds
        self.nodes.append(node)
        return name

    def link(self, src: str, dst: str, output: int = 0) -> None:
        outs = self.connections.setdefault(src, {"main": []})["main"]
        while len(outs) <= output:
            outs.append([])
        outs[output].append({"node": dst, "type": "main", "index": 0})

    def chain(self, *names: str) -> None:
        for a, b in zip(names, names[1:], strict=False):
            self.link(a, b)

    def json(self) -> dict:
        return {"name": self.name, "nodes": self.nodes, "connections": self.connections,
                "settings": {"executionOrder": "v1", "timezone": "Asia/Karachi",
                             "saveDataSuccessExecution": "all", "saveManualExecutions": True}}


def code(js: str, each: bool = False) -> dict:
    p = {"jsCode": JS_HELPERS + "\n" + js.strip()}
    if each:
        p["mode"] = "runOnceForEachItem"
    return p


def rl(value: str, mode: str = "id") -> dict:
    return {"__rl": True, "value": value, "mode": mode}


def sheet_params(operation: str, **extra) -> dict:
    return {"operation": operation, "documentId": rl("={{ $env.GOOGLE_SHEET_ID }}"),
            "sheetName": rl(SHEET_TAB, "name"), **extra}


def sheet_read(filter_col: str, value: str) -> dict:
    return sheet_params("read", filtersUI={"values": [{"lookupColumn": filter_col, "lookupValue": value}]},
                        options={})


def sheet_update() -> dict:
    """Update the row whose candidate_id matches; only fields present on the item are written."""
    schema = [{"id": c, "displayName": c, "required": False, "defaultMatch": c == "candidate_id",
               "display": True, "type": "string", "canBeUsedToMatch": True} for c in COLUMNS]
    return sheet_params("update", columns={"mappingMode": "autoMapInputData", "value": {},
                                           "matchingColumns": ["candidate_id"], "schema": schema},
                        options={})


def http(method: str, url: str, *, json_body: str | None = None, api_key: bool = True,
         bearer_env: str | None = None, full: bool = True, timeout: int = 30000) -> dict:
    headers = []
    if api_key:
        headers.append({"name": "X-API-Key", "value": "={{ $env.SCREENER_API_KEY }}"})
    if bearer_env:
        headers.append({"name": "Authorization", "value": f"=Bearer {{{{ $env.{bearer_env} }}}}"})
    p: dict = {"method": method, "url": url, "options": {"timeout": timeout}}
    if headers:
        p |= {"sendHeaders": True, "headerParameters": {"parameters": headers}}
    if json_body:
        p |= {"sendBody": True, "specifyBody": "json", "jsonBody": json_body}
    if full:  # never throw on 4xx/5xx: we inspect statusCode ourselves
        p["options"]["response"] = {"response": {"fullResponse": True, "neverError": True}}
    return p


RETRY = {"retryOnFail": True, "maxTries": 3, "waitBetweenTries": 3000}
WA_URL = "=https://graph.facebook.com/{{ $env.META_GRAPH_VERSION || 'v24.0' }}/{{ $env.META_WA_PHONE_NUMBER_ID }}/messages"


def wa_send(wf: WF, name: str, pos: tuple[int, int]) -> str:
    return wf.add(name, "n8n-nodes-base.httpRequest", 4.2,
                  http("POST", WA_URL, json_body="={{ JSON.stringify($json.wa) }}", api_key=False,
                       bearer_env="META_WA_ACCESS_TOKEN"), pos=pos, **RETRY)


def switch_on(field: str, keys: list[str]) -> dict:
    rules = [{"conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
                             "conditions": [{"id": str(uuid.uuid4()), "leftValue": f"={{{{ $json.{field} }}}}",
                                             "rightValue": k, "operator": {"type": "string", "operation": "equals"}}],
                             "combinator": "and"},
              "renameOutput": True, "outputKey": k} for k in keys]
    return {"rules": {"values": rules}, "options": {}}


def webhook(wf: WF, name: str, path: str, pos: tuple[int, int]) -> str:
    return wf.add(name, "n8n-nodes-base.webhook", 2,
                  {"httpMethod": "POST", "path": path, "responseMode": "responseNode", "options": {}},
                  pos=pos, webhookId=str(uuid.uuid5(uuid.NAMESPACE_URL, path)))


def respond(wf: WF, name: str, status: int, pos: tuple[int, int]) -> str:
    return wf.add(name, "n8n-nodes-base.respondToWebhook", 1.1,
                  {"respondWith": "json", "responseBody": f'={{{{ {{ "status": {status} }} }}}}',
                   "options": {"responseCode": status}}, pos=pos)


# ----------------------------------------------------------------------------- workflows
def wf_process_result(creds: dict) -> WF:
    """Sub-workflow: input items {result, row, prev_status} -> Sheet + WhatsApp + email."""
    wf = WF("5 - Process evaluation result")
    trig = wf.add("When called by another workflow", "n8n-nodes-base.executeWorkflowTrigger", 1.1,
                  {"inputSource": "passthrough"}, pos=(0, 300))
    plan = wf.add("Plan notifications", "n8n-nodes-base.code", 2, code(r"""
const out = [];
for (const item of $input.all()) {
  const { result: r, row, prev_status, error } = item.json;
  const cid = row.candidate_id, name = row.name, first = firstName(name);
  const to = waNumber(row.phone), hrTo = waNumber($env.HR_WHATSAPP_NUMBER);
  const hrEmail = $env.HR_EMAIL, email = row.email;
  const push = (channel, data) => out.push({ json: { channel, ...data } });
  const hrAlert = (msg) => hrTo && push('whatsapp', { wa: template(hrTo, 'hr_alert', [msg]) });

  if (error || !r) {  // evaluation failed even after retries
    push('sheet', { fields: { candidate_id: cid, status: 'Error', updated_at: NOW } });
    hrAlert(`Evaluation FAILED for ${name} (${cid}): ${error || 'no result'}. Check n8n executions.`);
    continue;
  }
  const s = r.scores;
  const fields = { candidate_id: cid, status: r.sheet_status, decision: r.final_decision || r.decision,
    score: s ? `${s.total}/40` : '', ai_summary: s ? s.summary : (r.incomplete_reason || ''),
    thread_id: r.thread_id, flags: (r.flags || []).join(', '), updated_at: NOW };
  if (r.interview) { fields.interview_datetime = pkt(r.interview.start); fields.meet_link = r.interview.meet_link || ''; }
  const m = r.messages || {};

  switch (r.sheet_status) {
    case 'Scheduled':
    case 'Shortlisted':
      push('sheet', { fields });
      if (to) push('whatsapp', { wa: template(to, 'screening_update', [first, body(m.candidate)]) });
      if (email) push('email', { to: email, subject: `Interview invitation - ${r.candidate_id ? row.role : ''} internship`, message: m.candidate });
      hrAlert(m.hr);
      if (hrEmail) push('email', { to: hrEmail, subject: `[Shortlisted] ${name} - ${row.role}`, message: `${m.hr}\n\nThread: ${r.thread_id}` });
      break;
    case 'Rejected':
      push('sheet', { fields });
      if (to) push('whatsapp', { wa: template(to, 'screening_update', [first, body(m.candidate)]) });
      if (email) push('email', { to: email, subject: `Your ${row.role} internship application`, message: m.candidate });
      if (hrEmail) push('email', { to: hrEmail, subject: `[Rejected] ${name} - ${row.role}`, message: `${m.hr}\n\nThread: ${r.thread_id}` });
      break;
    case 'Human Review':
      push('sheet', { fields });
      hrAlert(m.hr);  // contains "Reply APPROVE <thread> or REJECT <thread>"
      if (hrEmail) push('email', { to: hrEmail, subject: `[Decision needed] ${name} - ${row.role}`, message: m.hr });
      break;
    case 'Incomplete':
      if (prev_status !== 'Re-invited') {  // edge case: one new link after a dropped/short call
        push('reinvite', { candidate_id: cid, name, role: row.role, to });
      } else {
        push('sheet', { fields });
        hrAlert(`${name} (${cid}) did not complete the screening twice (${r.incomplete_reason}). Please follow up personally.`);
      }
      break;
  }
  if ((r.flags || []).includes('asked_for_human')) hrAlert(`${name} (${cid}) asked to speak with a person during the screening.`);
}
return out;
"""), pos=(240, 300))
    route = wf.add("Route", "n8n-nodes-base.switch", 3.2, switch_on("channel", ["sheet", "whatsapp", "email", "reinvite"]),
                   pos=(480, 300))
    unwrap = wf.add("Sheet fields", "n8n-nodes-base.code", 2,
                    {"jsCode": "return $input.all().map(i => ({ json: i.json.fields }));"}, pos=(720, 100))
    upd = wf.add("Update sheet", "n8n-nodes-base.googleSheets", 4.7, sheet_update(), pos=(960, 100),
                 creds=creds["sheets"], **RETRY)
    send = wa_send(wf, "Send WhatsApp", (720, 280))
    mail = wf.add("Send email", "n8n-nodes-base.gmail", 2.1,
                  {"resource": "message", "operation": "send", "sendTo": "={{ $json.to }}",
                   "subject": "={{ $json.subject }}", "emailType": "text", "message": "={{ $json.message }}",
                   "options": {"appendAttribution": False}},
                  pos=(720, 460), creds=creds["gmail"], onError="continueRegularOutput", **RETRY)
    link = wf.add("New interview link", "n8n-nodes-base.httpRequest", 4.2,
                  http("POST", "={{ $env.SCREENER_API_URL }}/interview-links",
                       json_body="={{ JSON.stringify({ candidate_id: $json.candidate_id, candidate_name: $json.name, role: $json.role }) }}",
                       full=False), pos=(720, 640), **RETRY)
    reinv = wf.add("Re-invite message", "n8n-nodes-base.code", 2, code(r"""
const src = $('Route').item.json;
return { json: {
  wa: template(src.to, 'screening_update', [firstName(src.name),
    `your screening call ended early, no problem. Here is a new link to try again: ${$json.url}`]),
  fields: { candidate_id: src.candidate_id, status: 'Re-invited', interview_link: $json.url, updated_at: NOW },
} };
""", each=True), pos=(960, 640))
    send2 = wa_send(wf, "Send re-invite", (1200, 640))
    fields2 = wf.add("Re-invite sheet fields", "n8n-nodes-base.code", 2,
                     {"jsCode": "return $('Re-invite message').all().map(i => ({ json: i.json.fields }));"},
                     pos=(1440, 640))
    upd2 = wf.add("Update sheet (re-invited)", "n8n-nodes-base.googleSheets", 4.7, sheet_update(),
                  pos=(1680, 640), creds=creds["sheets"], **RETRY)
    wf.chain(trig, plan, route)
    wf.link(route, unwrap, 0)
    wf.link(route, send, 1)
    wf.link(route, mail, 2)
    wf.link(route, link, 3)
    wf.chain(unwrap, upd)
    wf.chain(link, reinv, send2, fields2, upd2)
    return wf


def wf_invite(creds: dict) -> WF:
    wf = WF("1 - Invite new candidates")
    sched = wf.add("Every 5 minutes", "n8n-nodes-base.scheduleTrigger", 1.2,
                   {"rule": {"interval": [{"field": "minutes", "minutesInterval": 5}]}}, pos=(0, 300))
    rows = wf.add("Get new candidates", "n8n-nodes-base.googleSheets", 4.7, sheet_read("status", "New"),
                  pos=(240, 300), creds=creds["sheets"], **RETRY)
    link = wf.add("Create interview link", "n8n-nodes-base.httpRequest", 4.2,
                  http("POST", "={{ $env.SCREENER_API_URL }}/interview-links",
                       json_body="={{ JSON.stringify({ candidate_id: String($json.candidate_id), candidate_name: $json.name, role: $json.role }) }}",
                       full=False), pos=(480, 300), **RETRY)
    build = wf.add("Build invite", "n8n-nodes-base.code", 2, code(r"""
const row = $('Get new candidates').item.json;
return { json: {
  candidate_id: String(row.candidate_id), url: $json.url,
  wa: template(waNumber(row.phone), 'screening_invite', [firstName(row.name), row.role, $json.url]),
} };
""", each=True), pos=(720, 300))
    send = wa_send(wf, "Send WhatsApp invite", (960, 300))
    fields = wf.add("Sheet fields", "n8n-nodes-base.code", 2, code(r"""
const b = $('Build invite').item.json;
const ok = $json.statusCode === 200;
return { json: { candidate_id: b.candidate_id, interview_link: b.url,
  status: ok ? 'Invited' : 'Invite failed', updated_at: NOW,
  flags: ok ? '' : `whatsapp_error: ${clean(JSON.stringify($json.body?.error?.message || $json.body), 200)}` } };
""", each=True), pos=(1200, 300))
    upd = wf.add("Update sheet", "n8n-nodes-base.googleSheets", 4.7, sheet_update(), pos=(1440, 300),
                 creds=creds["sheets"], **RETRY)
    wf.chain(sched, rows, link, build, send, fields, upd)
    return wf


def wf_vapi(creds: dict, process_id: str) -> WF:
    wf = WF("2 - Vapi end-of-call -> evaluate")
    hook = webhook(wf, "Vapi end-of-call webhook", "vapi-end-of-call", (0, 300))
    parse = wf.add("Verify & parse", "n8n-nodes-base.code", 2, code(r"""
const h = $json.headers || {};
const msg = ($json.body || {}).message || {};
const call = msg.call || {};
const meta = (call.assistantOverrides || {}).metadata || {};
const started = msg.startedAt || call.startedAt, ended = msg.endedAt || call.endedAt;
return { json: {
  authorized: !!$env.VAPI_WEBHOOK_SECRET && h['x-vapi-secret'] === $env.VAPI_WEBHOOK_SECRET,
  relevant: msg.type === 'end-of-call-report' && !!meta.candidate_id,
  candidate_id: meta.candidate_id, interview_token: meta.interview_token, call_id: call.id,
  duration: started && ended ? (Date.parse(ended) - Date.parse(started)) / 1000 : null,
  transcript: (msg.artifact || {}).transcript || msg.transcript || '',
  call_time: started ? pkt(started) : '', ended_reason: msg.endedReason,
} };
""", each=True), pos=(240, 300))
    auth = wf.add("Authorized?", "n8n-nodes-base.if", 2.2, {
        "conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
                       "conditions": [{"id": str(uuid.uuid4()), "leftValue": "={{ $json.authorized }}", "rightValue": "",
                                       "operator": {"type": "boolean", "operation": "true", "singleValue": True}}],
                       "combinator": "and"}, "options": {}}, pos=(480, 300))
    ok = respond(wf, "200 OK", 200, (720, 200))
    deny = respond(wf, "401 Unauthorized", 401, (720, 420))
    rel = wf.add("End-of-call report?", "n8n-nodes-base.filter", 2.2, {
        "conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
                       "conditions": [{"id": str(uuid.uuid4()), "leftValue": "={{ $('Verify & parse').item.json.relevant }}",
                                       "rightValue": "", "operator": {"type": "boolean", "operation": "true", "singleValue": True}}],
                       "combinator": "and"}, "options": {}}, pos=(960, 200))
    find = wf.add("Find candidate", "n8n-nodes-base.googleSheets", 4.7,
                  sheet_read("candidate_id", "={{ $('Verify & parse').item.json.candidate_id }}"),
                  pos=(1200, 200), creds=creds["sheets"], **RETRY)
    build = wf.add("Build evaluate request", "n8n-nodes-base.code", 2, code(r"""
const p = $('Verify & parse').item.json, row = $json;
return { json: { row, prev_status: row.status,
  sheet: { candidate_id: String(row.candidate_id), status: 'Called', call_time: p.call_time, updated_at: NOW },
  request: { candidate_id: String(row.candidate_id), candidate_name: row.name, candidate_email: row.email || null,
    role: row.role, transcript: p.transcript || '(empty)', call_id: p.call_id,
    call_duration_seconds: p.duration, interview_token: p.interview_token },
} };
""", each=True), pos=(1440, 200))
    mark = wf.add("Mark called", "n8n-nodes-base.code", 2,
                  {"jsCode": "return $input.all().map(i => ({ json: i.json.sheet }));"}, pos=(1680, 60))
    upd = wf.add("Update sheet", "n8n-nodes-base.googleSheets", 4.7, sheet_update(), pos=(1920, 60),
                 creds=creds["sheets"], **RETRY)
    ev = wf.add("Evaluate", "n8n-nodes-base.httpRequest", 4.2,
                http("POST", "={{ $env.SCREENER_API_URL }}/evaluate",
                     json_body="={{ JSON.stringify($json.request) }}", timeout=180000),
                pos=(1680, 300), retryOnFail=True, maxTries=2, waitBetweenTries=10000)
    pack = wf.add("Package result", "n8n-nodes-base.code", 2, code(r"""
const b = $('Build evaluate request').item.json;
const ok = $json.statusCode === 200;
return { json: { row: b.row, prev_status: b.prev_status, result: ok ? $json.body : null,
  error: ok ? null : `HTTP ${$json.statusCode}: ${clean(JSON.stringify($json.body), 300)}` } };
""", each=True), pos=(1920, 300))
    sub = wf.add("Process result", "n8n-nodes-base.executeWorkflow", 1.2,
                 {"source": "database", "workflowId": rl(process_id), "options": {}}, pos=(2160, 300))
    wf.chain(hook, parse, auth)
    wf.link(auth, ok, 0)
    wf.link(auth, deny, 1)
    wf.chain(ok, rel, find, build)
    wf.link(build, mark)
    wf.link(build, ev)
    wf.chain(mark, upd)
    wf.chain(ev, pack, sub)
    return wf


def wf_review(creds: dict, process_id: str) -> WF:
    wf = WF("3 - HR review reply -> resume agent")
    hook = webhook(wf, "HR review (from API)", "hr-review", (0, 300))
    auth = wf.add("Internal call?", "n8n-nodes-base.if", 2.2, {
        "conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
                       "conditions": [{"id": str(uuid.uuid4()), "leftValue": "={{ $json.headers['x-api-key'] }}",
                                       "rightValue": "={{ $env.SCREENER_API_KEY }}",
                                       "operator": {"type": "string", "operation": "equals"}}],
                       "combinator": "and"}, "options": {}}, pos=(240, 300))
    ok = respond(wf, "200 OK", 200, (480, 200))
    deny = respond(wf, "401 Unauthorized", 401, (480, 420))
    rev = wf.add("Resume agent (/review)", "n8n-nodes-base.httpRequest", 4.2,
                 http("POST", "={{ $env.SCREENER_API_URL }}/review/{{ $('HR review (from API)').item.json.body.thread_id }}",
                      json_body="={{ JSON.stringify({ decision: $('HR review (from API)').item.json.body.decision, reviewer: $('HR review (from API)').item.json.body.reviewer, note: $('HR review (from API)').item.json.body.note }) }}",
                      timeout=120000), pos=(720, 200), **RETRY)
    check = wf.add("Check result", "n8n-nodes-base.code", 2, code(r"""
const req = $('HR review (from API)').item.json.body;
if ($json.statusCode === 200) return { json: { ok: true, result: $json.body } };
const why = { 404: 'unknown reference', 409: 'this candidate was already decided' }[$json.statusCode] || `error ${$json.statusCode}`;
return { json: { ok: false, wa: text(waNumber(req.from), `Could not apply "${req.decision} ${req.thread_id}": ${why}.`) } };
""", each=True), pos=(960, 200))
    applied = wf.add("Applied?", "n8n-nodes-base.if", 2.2, {
        "conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
                       "conditions": [{"id": str(uuid.uuid4()), "leftValue": "={{ $json.ok }}", "rightValue": "",
                                       "operator": {"type": "boolean", "operation": "true", "singleValue": True}}],
                       "combinator": "and"}, "options": {}}, pos=(1200, 200))
    find = wf.add("Find candidate", "n8n-nodes-base.googleSheets", 4.7,
                  sheet_read("candidate_id", "={{ $json.result.candidate_id }}"), pos=(1440, 100),
                  creds=creds["sheets"], **RETRY)
    pack = wf.add("Package result", "n8n-nodes-base.code", 2, code(r"""
return { json: { row: $json, prev_status: $json.status, result: $('Check result').item.json.result } };
""", each=True), pos=(1680, 100))
    sub = wf.add("Process result", "n8n-nodes-base.executeWorkflow", 1.2,
                 {"source": "database", "workflowId": rl(process_id), "options": {}}, pos=(1920, 100))
    tell = wa_send(wf, "Tell HR it failed", (1440, 320))
    wf.chain(hook, auth)
    wf.link(auth, ok, 0)
    wf.link(auth, deny, 1)
    wf.chain(ok, rev, check, applied)
    wf.link(applied, find, 0)
    wf.link(applied, tell, 1)
    wf.chain(find, pack, sub)
    return wf


def wf_inbound(creds: dict) -> WF:
    wf = WF("4 - Inbound WhatsApp messages")
    hook = webhook(wf, "Inbound message (from API)", "whatsapp-inbound", (0, 300))
    plan = wf.add("Plan reply", "n8n-nodes-base.code", 2, code(r"""
const ok = $json.headers['x-api-key'] === $env.SCREENER_API_KEY;
const m = $json.body || {};
const hrTo = waNumber($env.HR_WHATSAPP_NUMBER);
let wa = null;
if (ok && m.kind === 'hr_message') {
  // HR replied within the 24h window -> a normal text message is allowed
  wa = text(hrTo, 'To decide on a borderline candidate, reply exactly:\nAPPROVE <reference>\nor\nREJECT <reference> optional note\n(the reference is in the alert, e.g. C002-c83ec8f0)');
} else if (ok && m.kind === 'candidate_message' && hrTo) {
  wa = template(hrTo, 'hr_alert', [`Message from candidate ${m.name || ''} (+${m.from}): "${clean(m.text, 600)}"`]);
}
return { json: { ok, wa } };
""", each=True), pos=(240, 300))
    resp = wf.add("Respond", "n8n-nodes-base.respondToWebhook", 1.1,
                  {"respondWith": "json", "responseBody": "={{ { ok: $json.ok } }}",
                   "options": {"responseCode": "={{ $json.ok ? 200 : 401 }}"}}, pos=(480, 300))
    has = wf.add("Anything to send?", "n8n-nodes-base.filter", 2.2, {
        "conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
                       "conditions": [{"id": str(uuid.uuid4()), "leftValue": "={{ !!$('Plan reply').item.json.wa }}",
                                       "rightValue": "", "operator": {"type": "boolean", "operation": "true", "singleValue": True}}],
                       "combinator": "and"}, "options": {}}, pos=(720, 300))
    prep = wf.add("Message", "n8n-nodes-base.code", 2,
                  {"jsCode": "return $('Plan reply').all().filter(i => i.json.wa).map(i => ({ json: { wa: i.json.wa } }));"},
                  pos=(960, 300))
    send = wa_send(wf, "Send WhatsApp", (1200, 300))
    wf.chain(hook, plan, resp, has, prep, send)
    return wf


def wf_reminders(creds: dict) -> WF:
    wf = WF("6 - Reminders and expiry")
    sched = wf.add("Every hour", "n8n-nodes-base.scheduleTrigger", 1.2,
                   {"rule": {"interval": [{"field": "hours", "hoursInterval": 1}]}}, pos=(0, 300))
    rows = wf.add("Get invited candidates", "n8n-nodes-base.googleSheets", 4.7, sheet_read("status", "Invited"),
                  pos=(240, 300), creds=creds["sheets"], **RETRY)
    st = wf.add("Link status", "n8n-nodes-base.httpRequest", 4.2,
                http("GET", "={{ $env.SCREENER_API_URL }}/candidates/{{ encodeURIComponent($json.candidate_id) }}/interview-status"),
                pos=(480, 300), **RETRY)
    plan = wf.add("Decide", "n8n-nodes-base.code", 2, code(r"""
const out = [];
const rows = $('Get invited candidates').all();
$input.all().forEach((item, i) => {
  const row = rows[i].json, s = item.json.body || {};
  if (item.json.statusCode !== 200) return;
  const hours = (Date.now() - Date.parse(s.created_at)) / 36e5;
  if (s.status === 'sent' && hours >= 24) {   // never opened the link -> one reminder
    out.push({ json: {
      wa: template(waNumber(row.phone), 'screening_update', [firstName(row.name),
        `a reminder to complete your 5-minute screening interview for the ${row.role} internship: ${s.url} (link valid until ${pkt(s.expires_at)})`]),
      fields: { candidate_id: String(row.candidate_id), status: 'Reminded', updated_at: NOW } } });
  } else if (s.status === 'expired') {
    out.push({ json: { wa: null, fields: { candidate_id: String(row.candidate_id), status: 'No response', updated_at: NOW } } });
  }
});
return out;
"""), pos=(720, 300))
    has = wf.add("Send reminder?", "n8n-nodes-base.if", 2.2, {
        "conditions": {"options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2},
                       "conditions": [{"id": str(uuid.uuid4()), "leftValue": "={{ !!$json.wa }}", "rightValue": "",
                                       "operator": {"type": "boolean", "operation": "true", "singleValue": True}}],
                       "combinator": "and"}, "options": {}}, pos=(960, 300))
    send = wa_send(wf, "Send reminder", (1200, 200))
    f1 = wf.add("Reminded fields", "n8n-nodes-base.code", 2,
                {"jsCode": "return $('Send reminder?').all().filter(i => i.json.wa).map(i => ({ json: i.json.fields }));"},
                pos=(1440, 200))
    f2 = wf.add("Expired fields", "n8n-nodes-base.code", 2,
                {"jsCode": "return $input.all().map(i => ({ json: i.json.fields }));"}, pos=(1200, 420))
    upd = wf.add("Update sheet", "n8n-nodes-base.googleSheets", 4.7, sheet_update(), pos=(1680, 300),
                 creds=creds["sheets"], **RETRY)
    wf.chain(sched, rows, st, plan, has)
    wf.link(has, send, 0)
    wf.link(has, f2, 1)
    wf.chain(send, f1, upd)
    wf.link(f2, upd)
    return wf


# ----------------------------------------------------------------------------- n8n API
def read_env() -> dict[str, str]:
    env = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def api(method: str, path: str, body: dict | None = None) -> dict:
    key = read_env().get("N8N_API_KEY")
    if not key:
        sys.exit("Set N8N_API_KEY in .env (n8n -> Settings -> n8n API -> Create an API key).")
    req = urllib.request.Request(f"{N8N}/api/v1{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-N8N-API-KEY": key, "Content-Type": "application/json",
                                          "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        sys.exit(f"n8n API {method} {path} -> HTTP {e.code}: {e.read().decode(errors='replace')[:800]}")


def ensure_credentials() -> dict:
    """Create the Google OAuth credentials (client id/secret from secrets/) if missing.
    You still click 'Sign in with Google' on each one in the n8n UI to connect it."""
    client = json.loads((ROOT / "secrets" / "google-oauth-client.json").read_text(encoding="utf-8"))
    client = client.get("installed") or client.get("web")
    found: dict[str, dict] = {}
    existing = {c["name"]: c for c in api("GET", "/credentials").get("data", [])}
    for key, (name, ctype) in {"sheets": (CRED_SHEETS, "googleSheetsOAuth2Api"),
                               "gmail": (CRED_GMAIL, "gmailOAuth2")}.items():
        cred = existing.get(name) or api("POST", "/credentials", {
            "name": name, "type": ctype,
            "data": {"clientId": client["client_id"], "clientSecret": client["client_secret"],
                     "sendAdditionalBodyProperties": False, "additionalBodyProperties": ""}})
        found[key] = {ctype: {"id": cred["id"], "name": name}}
    return found


def placeholder_credentials() -> dict:
    return {"sheets": {"googleSheetsOAuth2Api": {"id": "SHEETS_CREDENTIAL_ID", "name": CRED_SHEETS}},
            "gmail": {"gmailOAuth2": {"id": "GMAIL_CREDENTIAL_ID", "name": CRED_GMAIL}}}


def upsert(wf: WF) -> str:
    existing = {w["name"]: w for w in api("GET", "/workflows?limit=250").get("data", [])}
    body = wf.json()
    if wf.name in existing:
        wid = existing[wf.name]["id"]
        api("PUT", f"/workflows/{wid}", body)
        print(f"  updated  {wf.name} ({wid})")
    else:
        wid = api("POST", "/workflows", body)["id"]
        print(f"  created  {wf.name} ({wid})")
    return wid


def save(wf: WF, filename: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / filename).write_text(json.dumps(wf.json(), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["build", "push", "activate"])
    action = parser.parse_args().action

    creds = placeholder_credentials() if action == "build" else ensure_credentials()
    process = wf_process_result(creds)
    pid = upsert(process) if action != "build" else "PROCESS_RESULT_WORKFLOW_ID"
    others = [(wf_invite(creds), "1-invite.json"), (wf_vapi(creds, pid), "2-vapi-end-of-call.json"),
              (wf_review(creds, pid), "3-hr-review.json"), (wf_inbound(creds), "4-inbound-whatsapp.json"),
              (wf_reminders(creds), "6-reminders.json")]
    ids = {process.name: pid}
    for wf, _ in others:
        if action != "build":
            ids[wf.name] = upsert(wf)

    # Exported copies for git use placeholder credential/workflow ids (no secrets anyway)
    exp_creds = placeholder_credentials()
    save(wf_process_result(exp_creds), "5-process-result.json")
    for builder, fname in [(lambda: wf_invite(exp_creds), "1-invite.json"),
                           (lambda: wf_vapi(exp_creds, "PROCESS_RESULT_WORKFLOW_ID"), "2-vapi-end-of-call.json"),
                           (lambda: wf_review(exp_creds, "PROCESS_RESULT_WORKFLOW_ID"), "3-hr-review.json"),
                           (lambda: wf_inbound(exp_creds), "4-inbound-whatsapp.json"),
                           (lambda: wf_reminders(exp_creds), "6-reminders.json")]:
        save(builder(), fname)
    print(f"Exported 6 workflows to {OUT.relative_to(ROOT)}")

    if action == "activate":
        for name, wid in ids.items():
            if name == process.name:
                continue  # sub-workflows don't need activation
            api("POST", f"/workflows/{wid}/activate")
            print(f"  activated {name}")


if __name__ == "__main__":
    main()
