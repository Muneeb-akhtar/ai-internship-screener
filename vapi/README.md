# Vapi voice interview: setup

| File | What it is |
|---|---|
| [`assistant-prompt.md`](assistant-prompt.md) | System prompt for the voice assistant "Ayla": fixed questions, fairness rules, edge cases |
| [`assistant.json`](assistant.json) | Assistant config: first message (AI disclosure + consent), model, voice, transcriber, limits |
| [`../scripts/vapi.py`](../scripts/vapi.py) | Creates/updates the assistant from these files, creates links, evaluates calls |

**Infrastructure as code:** don't edit the assistant by hand in the Vapi dashboard. Change these
files and run `sync` again. That way the voice agent is versioned in git like the rest of the project.

## How it fits together
```
n8n ──POST /interview-links──▶ API ── returns https://<domain>/interview/<token>
candidate opens link ─▶ /interview/<token> page ─▶ Vapi Web SDK browser call
      variableValues: name, role, company, technical_question, problem_question, internship facts
      metadata:       interview_token, candidate_id
call ends ─▶ Vapi end-of-call-report ─▶ n8n webhook (Phase 5) ─▶ POST /evaluate (+ interview_token)
```
- **Same questions for everyone:** the technical and problem questions come from
  `screener-api/app/interview/questions.py` (one set per role keyword), not from the LLM.
- **Consent is word-for-word:** it's in `firstMessage`, so every candidate hears identical wording.
- **The browser isn't trusted:** `/evaluate` checks `interview_token` against the database.
- **Cost caps:**
  - At most 2 calls per link, reserved *before* Vapi starts.
  - `maxDurationSeconds: 420` (7 minutes).
  - The page also stops the call after 6 minutes.

## One-time setup
1. **Create a Vapi account** at https://dashboard.vapi.ai. New accounts usually get trial
   credit (about $10). Each 5-minute interview costs roughly $0.50–$1.50. Don't add a card
   until you need to.
2. **Copy the keys** (Dashboard → *API Keys*) into `.env`:
   ```
   VAPI_PUBLIC_KEY=...    # "Public" key: used in the browser
   VAPI_PRIVATE_KEY=...   # "Private" key: only for scripts/vapi.py. Never share it.
   ```
3. **Restrict the public key** (Dashboard → API Keys → edit the public key):
   - *Allowed origins*: `http://localhost:8000` now; add your domain in Phase 6.
   - *Allowed assistants*: this assistant only, once it exists.

   Anyone can see a public key in the page source, so these restrictions stop it being used elsewhere.
4. **Create the assistant**:
   ```powershell
   $py = ".\screener-api\.venv\Scripts\python.exe"
   & $py scripts\vapi.py sync          # creates it and writes VAPI_ASSISTANT_ID into .env
   docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d screener-api   # reload env
   ```
5. Optionally set your company and internship details in `.env` (`COMPANY_NAME`, `INTERNSHIP_*`).
   The assistant only answers candidate questions with these facts.

## Test a real browser call (Phase 3)
```powershell
& $py scripts\vapi.py link --id C100 --name "Your Name" --role "Python Developer Intern"
# open the printed http://localhost:8000/interview/... link in Chrome/Edge, allow the mic, do the interview
& $py scripts\vapi.py calls                       # find your call id
& $py scripts\vapi.py evaluate --call <call_id>   # transcript -> /evaluate (what n8n will automate)
```
`localhost` counts as a secure origin, so the microphone works without HTTPS. To test on a phone,
you'll need the HTTPS tunnel from Phase 5.

## End-of-call webhook (Phase 5)
Vapi has to reach n8n over the internet. Once the tunnel or domain exists, set
`N8N_WEBHOOK_URL=https://.../` in `.env` and run `sync` again. That adds
`server.url = <N8N_WEBHOOK_URL>/webhook/vapi-end-of-call` and an `X-Vapi-Secret` header, which
the n8n webhook checks. The fields n8n uses from the report:
`message.call.id`, `message.call.assistantOverrides.metadata.{interview_token,candidate_id}`,
`message.artifact.transcript`, `message.startedAt` / `message.endedAt`, `message.endedReason`.
n8n sends `recording_url = https://dashboard.vapi.ai/calls/<call.id>` to `/evaluate`, so HR can listen
(after logging in) before deciding. Speech-to-text makes mistakes, so the transcript alone isn't the truth.

## Changing the voice or model
Edit `assistant.json`. For example, `voice.voiceId` takes any built-in Vapi voice name (try them in
the dashboard). `model.model` must be one Vapi supports for the provider; `sync` reports an error if not.
