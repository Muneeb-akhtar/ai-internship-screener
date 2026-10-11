# Project progress log

Running record of the build so any new session (human or AI) can pick up where we left off.
**Rule:** build in phases and stop after each one so the owner can test before moving on.

## Status
| Phase | Status |
|---|---|
| 1. Scaffold + local Docker | ✅ **Done.** All 3 containers healthy, endpoints + DBs verified (2026-10-09) |
| 2. LangGraph agent | ✅ **Built + verified** with real Gemini and Postgres (2026-10-10). ⏳ Owner to test |
| 3. Vapi interview page + assistant prompt | ✅ **Done.** Real browser interview → transcript → agent decision verified (2026-10-11) |
| 4. Google Calendar tool | ✅ **Done.** Real free/busy + events with Meet links in hr-account's calendar (2026-10-11) |
| 5. n8n workflows + Meta WhatsApp Cloud API | Not started |
| 6. AWS EC2 deployment | Not started (set an AWS Budget alert first) |
| 7. CI/CD + README polish | Not started |

## Log
### 2026-10-09: environment setup + Phase 1
- Docker Desktop failed with "Virtual Machine Platform not enabled". Enabled the
  `Microsoft-Windows-Subsystem-Linux` + `VirtualMachinePlatform` features and installed
  WSL 3.0.1 (`wsl --install --no-distribution --web-download`; the Store download kept resetting).
  **A Windows restart is required** before Docker works.
- Installed Python 3.12.10 (user scope, `%LOCALAPPDATA%\Programs\Python\Python312`).
- Built Phase 1: compose stack (postgres 16, n8n 2.42.6, screener-api, caddy behind the `prod`
  profile), dev override, Postgres init script (separate `n8n` + `screener` DBs/users),
  FastAPI `/health` + `/auth/ping`, settings, API-key security, structlog, 5 pytest tests,
  `.env.example`, `scripts/init-env.ps1` (already run, so `.env` exists), `git init` (staged, not committed).

- After the first restart, WSL2 still said "virtualization is not enabled". The features were
  Enabled, but the BCD `hypervisorlaunchtype` setting was unset and the `vmcompute` service was
  missing. Ran `bcdedit /set hypervisorlaunchtype auto` and re-ran the VMP enable → **restart #2 needed**.
  If it still fails: disable VirtualMachinePlatform, restart, re-enable it, restart again.

- **Real root cause:** the restarts were plain "Restart", which skips pending servicing. A
  cumulative update (KB5129195, build 26100.9457) was pending and the WSL/VMP feature installs
  were queued behind it, so their files (`vmcompute.exe`, `lxss.sys`) were never laid down.
  "Update and restart" fixed it. **Lesson: if a feature says Enabled but its files are missing,
  check for pending updates (`dism /online /get-packages` → "Install Pending").**
- Phase 1 verified: postgres, n8n and screener-api all healthy; `/health` → ok/db ok;
  `/auth/ping` 200 with key, 401 without; `n8n` + `screener` DBs exist with their own owners;
  n8n migrated 148 tables. Turned off uvicorn's access log (it duplicated our structured log).
- Note: the Windows time zone is set to Pacific Standard Time (the clock itself is correct).
  The containers use Asia/Karachi, so the project isn't affected.

### 2026-10-10: Gemini API key
- Created the key in AI Studio (project `ai-internship-screener`, free tier) and saved it in `.env`.
- Tested it: `gemini-2.5-*` returns 404 ("no longer available to new users").
  `gemini-3.8-flash`, `gemini-3.5-flash`, `gemini-3.5-flash-lite` and `gemini-3.1-flash-lite` work.
  → **Default model for Phase 2: `gemini-3.8-flash`.**

### 2026-10-10: Phase 2, the LangGraph Screening Decision Agent
- Phase 1 committed as `6aadc14` (local git only, no remote yet).
- Built: `app/agent/{state,prompts,nodes,graph,service}.py`, `app/llm.py` (Gemini/Claude/OpenAI
  switch, retry + fallback model), `app/audit.py` (`evaluations` table), `app/schemas.py`,
  `app/tools/calendar.py` (pure `find_free_slot` + `LocalScheduler`; Google comes in Phase 4),
  endpoints `/evaluate`, `/review/{thread_id}`, `/evaluations/{thread_id}`, `scripts/try-evaluate.ps1`.
- Graph: check_transcript → extract_answers → score ⇄ self_check → reject | request_review →
  human_review (interrupt) | shortlist. Plus an `incomplete` branch for short calls or no consent.
- Additions beyond the brief, chosen deliberately:
  - A `check_transcript` guard runs before any LLM call (call < 60 s or < 40 candidate words → incomplete).
  - No consent → incomplete. Asking for a human → `asked_for_human` flag.
  - Still low confidence after the one retry → human_review (flag `low_confidence`), so the AI never auto-decides when unsure.
  - A separate `request_review` node drafts the HR message *before* the interrupt (a node's updates are only checkpointed when it finishes).
  - Messages with facts (dates, links, scores) are templates; only the rejection text is LLM-drafted (with a template fallback).
  - `/evaluate` is idempotent per Vapi `call_id`, and resumes from the checkpoint after a crash.
  - Prompts treat the transcript as untrusted (prompt-injection guard).
- Verified:
  - 40 unit tests with a mocked LLM pass; ruff is clean.
  - Live Gemini on the 3 transcripts: strong 36/40 → shortlist, borderline 22–23/40 → human_review, weak 5–7/40 → reject.
  - Docker + Postgres: the borderline evaluation paused, **the API container was restarted**, then APPROVE resumed it from the checkpoint → Scheduled. Audit rows and checkpoints confirmed.
- Issues found and fixed:
  - `gemini-3.8-flash` returned "503 high demand" → added a fallback model (`gemini-3.5-flash`).
  - Multi-statement SQL fails with prepared statements → run one statement at a time.
  - ISO datetimes lose the "PKT" name → re-attach the zone when formatting.
  - PowerShell 5.1 `Get-Content` adds hidden properties to the JSON → read with `[IO.File]`.

### 2026-10-10: Phase 3, the Vapi voice interview
- Phase 2 committed as `8717037`. Checked the Vapi Web SDK 2.7.1 types (start(assistantId, overrides),
  variableValues, metadata, end-of-call report → `artifact.transcript`, `server.headers` for auth).
- Built:
  - `vapi/assistant-prompt.md` (voice system prompt), `vapi/assistant.json` (consent in `firstMessage`,
    google/gemini-3.5-flash, Vapi voice "Clara", deepgram nova-3, 420 s max, recording on, endCall tool), `vapi/README.md`.
  - `app/interview/`: `questions.py` (fixed questions per role keyword), `links.py` (Postgres `interview_links`
    table: random 32-character token, 72 h expiry, opened/started/evaluated timestamps, max 2 call starts),
    `routes.py` (`POST /interview-links`, `GET /candidates/{id}/interview-status`, `GET /interview/{token}`,
    `POST /interview/{token}/events`).
  - Templates `interview.html` (Vapi SDK via the jsDelivr ESM build, mic check, captions, timer) and `message.html`.
  - `/evaluate` now accepts `interview_token`: 403 if it belongs to another candidate, and marks the link
    completed (except for incomplete calls, so the link's second attempt stays available).
  - `scripts/vapi.py`: `sync` (assistant from repo files → Vapi API, writes VAPI_ASSISTANT_ID), `link`, `calls`,
    `evaluate --call` (does by hand what n8n will do).
- Security choice: the browser and Vapi metadata are untrusted. The token is verified against the DB, the public
  key is restricted in the Vapi dashboard, and the private key never reaches the container or the browser.
- Verified: 49 tests pass (9 new). On Docker: created a link, the page returns 503 "not configured" without
  Vapi keys, and the status endpoint and `interview_links` row are correct.

### 2026-10-11: Phase 3 real-call testing
- Created the Vapi account (5 credits) and the keys in `.env`. `vapi.py sync` created the assistant
  `c746333d-…` ("Internship Screener - Ayla").
- Vapi's API is behind Cloudflare, which blocks the `Python-urllib` User-Agent (error 1010) → `vapi.py` now sends its own.
- Calls 1 and 2 ended with `silence-timed-out` after 3 min and 95 s. Analysing the stereo recording
  (`artifact.presignedStereoUrl`; channel 0 = candidate) showed the candidate's speech at −45 to −75 dBFS
  with gaps of digital silence: a quiet built-in laptop mic plus speaker echo, with Chrome's default
  noise suppression and echo cancellation cutting the voice.
- Fixes:
  - Deepgram `en-IN` + keyterms + endpointing 300, `startSpeakingPlan.waitSeconds` 1.2, `silenceTimeoutSeconds` 40.
  - Vapi's smart denoising is off (it suppressed quiet speech).
  - The page opens the mic itself (echo cancellation on, noise suppression off, auto gain on) and passes the track via
    `new Vapi(key, undefined, undefined, { audioSource: track })`. Added a pre-call mic test meter and picker.
  - Vapi emits `error` on a normal server-side hangup → the page now waits for `call-end`.
- **Call 3: 288 s, `customer-ended-call`, $0.38, full transcript.** Ayla followed the script (consent, fixed
  questions, no hints, answered the FAQ only from the given facts, deferred an unknown question to HR).
  `vapi.py evaluate` → 19/40 (C6 T6 P1 M6, available, confidence 0.90) → **reject**. The link was marked completed.
- Note: 19/40 is one point under the human-review band. The candidate skipped one question and was
  auto-rejected. That's correct per the brief's rule, but worth an owner decision (see next steps).

- Vapi moved to the owner's **hr@example.com** account: new keys, `vapi.py sync` recreated the assistant
  (`2b225229-…`) from the repo files, and a new link page was verified. The old owner-account assistant is unused.

- **Fairness finding (call 4 on the new account, 220 s):** Deepgram misheard words ("computer science" →
  "the purpose science", "work culture" → "Walmart", mutable/immutable garbled). The scorer took them as the
  candidate's errors (technical 2/10 → auto-reject 11/40). Fix: the prompts now say it's ASR output; possible
  transcription errors lower **confidence**, not the score → human review. More keyterms. HR messages and
  the audit log carry a recording link (the Vapi dashboard call page; raw recording URLs are private). Re-run of the
  same call: confidence 0.60 twice → human_review + `low_confidence` + recording link. Commit `1487711`.

- Recording links: Vapi's dashboard has no per-call URL and raw recordings are private, so `/recordings/<call_id>`
  serves signed HMAC links that expire after 14 days and redirect to a fresh presigned audio URL (Vapi private key
  now in the container, server-side only). Verified live: audio/wav; a tampered link → 403. Commit `7f4c4ce`.

### 2026-10-11: Phase 4, Google Calendar
- OAuth (Desktop client) on **hr@example.com**, in the project `ai-internship-screener` (`<gcp-project-id>`),
  with no billing. Calendar API enabled, consent screen External/Testing, owner added as a test user.
  Scopes: `calendar.events` + `calendar.freebusy` only. Files: `secrets/google-oauth-client.json`, `secrets/google-token.json`
  (git-ignored, mounted read-only at /run/secrets).
- `GoogleCalendarScheduler`: free/busy → `find_free_slot` → `events.insert` with a hangoutsMeet `createRequest`
  (`conferenceDataVersion=1`). The event id is derived from the thread id (idempotent; 409 → fetch the existing event).
  Retries on 403/429/5xx, waits for a pending Meet link, and an asyncio lock prevents double booking. The API falls
  back to `LocalScheduler` without a token.
- Gotcha: `docker compose up -d` doesn't recreate an unchanged container, so a new secret file needs
  `docker compose restart screener-api` (the scheduler is chosen at startup).
- Verified live: the strong sample → shortlist → event Mon 12 Oct 10:00 PKT with a real Meet link; the same call_id again →
  same event (no duplicate); a 2nd candidate → 10:30 (real busy time respected). 63 tests.
- ⚠️ The consent screen is in **Testing** mode → Google expires the refresh token after 7 days. Publish the app
  (Audience → Publish app) to avoid re-running `scripts/google_auth.py` every week.
- Test events created: "Interview: Sara Khan (TEST)" 10:00 and "Interview: Ali Raza (TEST)" 10:30 on Mon 12 Oct.

### Next steps
1. Owner decision: should near-threshold rejects (e.g. 17–19) or any skipped question go to human review?
2. Owner: rotate the Vapi private key (it was shared in chat).
3. Owner: publish the OAuth app (avoid the 7-day token expiry); delete the 2 TEST events if wanted.
4. Phase 5: n8n workflows + Meta WhatsApp Cloud API + public tunnel.

## Decisions made
- The repo root is `D:\AI Internship Screener` itself; the Compose project name is `ai-screener`.
- Dependencies are in `requirements.txt` / `requirements-dev.txt` (pinned); `pyproject.toml` holds tool config only.
- One Postgres server with two databases (least privilege per service).
- Dev ports bind to 127.0.0.1 only. The API's hot reload uses polling (`WATCHFILES_FORCE_POLLING`) because of Windows bind mounts.
- Secrets are hex strings from `init-env.ps1`. `N8N_ENCRYPTION_KEY` must never change.

## Spec summary (from the original brief)
**Flow:** HR adds candidates to a Google Sheet (columns: candidate_id, name, phone, email, role,
status, interview_link, call_time, score, ai_summary, decision, interview_datetime, meet_link,
updated_at) → n8n finds status "New" → WhatsApp invite via **Meta WhatsApp Cloud API** (not
Twilio) with a unique link → status "Invited" → candidate opens `/interview/{token}` (FastAPI page
using the **Vapi Web SDK** browser call, with candidate_id as metadata) → Vapi agent: discloses it
is an AI and that the call is recorded, asks for consent, then 4–5 entry-level questions
(communication, technical, problem solving, motivation, availability) → Vapi end-of-call report →
n8n webhook → `POST /evaluate` → status "Called".

**LangGraph agent:** `extract_answers` → `score` (Pydantic structured output: communication,
technical, problem_solving, motivation 0–10 each; available bool; confidence 0–1; reasoning) →
`self_check` (if confidence < 0.7, re-score once) → route on the total out of 40: not available or
< 20 → `reject`; 20–27 → `human_review` (`interrupt()`, HR replies APPROVE/REJECT, n8n calls
`POST /review/{thread_id}`); ≥ 28 → `shortlist` (find the next free 30-minute slot on working
days, 10:00–17:00 Asia/Karachi, within the next 5 working days; create a Calendar event with a
Meet link; draft messages). Postgres checkpointer. Returns decision, scores, summary, datetime,
meet link and messages.

**n8n:** update the Sheet; send WhatsApp + email to the candidate and HR (HR's copy includes the
AI summary and scores); send rejection messages.
**Edge cases:** if the link isn't opened, send one reminder after 24h; if the call is dropped or
too short, mark it "Incomplete" and send a new link once; if the candidate asks for a human,
flag it for HR.
**Principle:** the AI only shortlists. HR decides. Every candidate gets the same questions and
rubric, and everything is logged.

**Stack:** Vapi, Meta WhatsApp Cloud API, n8n (Docker), Python 3.12 / FastAPI / LangGraph /
LangChain / Pydantic, Gemini by default (switchable to Claude/OpenAI via env), Postgres 16,
Caddy, LangSmith (optional), Docker Compose on AWS EC2 Ubuntu 24.04 + DuckDNS, GitHub Actions →
ECR → SSH deploy.

**Rules:** no secrets in git; shared-secret header on every endpoint; verify Meta signatures;
type hints, Pydantic, structured logging, retries for external APIs; simple, commented code that
explains concepts; PowerShell commands; ask before anything that costs money or needs the
owner's accounts.
