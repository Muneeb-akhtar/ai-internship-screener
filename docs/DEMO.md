# Demo video: script and recording guide (2–3 minutes)

Goal: show the **whole pipeline working for real** — from an HR row in a Sheet to an AI
decision and a booked Meet interview — in under 3 minutes.

## Before recording (10 min)
1. Start the server if it's stopped (AWS → EC2 → Instances → `ai-screener` → **Start**).
2. Open these tabs, in this order:
   1. The **Google Sheet** (Candidates tab)
   2. **Gmail** of the candidate address you'll use
   3. **n8n** through the SSH tunnel: `ssh -i $env:USERPROFILE\.ssh\screener-key.pem -L 5678:localhost:5678 ubuntu@<server-ip>` → http://localhost:5678 → *Executions*
   4. **Google Calendar** (HR account)
   5. This repo on GitHub (README + Actions tab)
3. Use **earphones with a mic**, a quiet room, browser zoom 110–125 % so text is readable.
4. Prepare answers that score well (strong candidate), so the video ends with a booked Meet.

## How to record (free)
- **Windows Snipping Tool** → *Record* (Win + Shift + R) → select the browser window. Saves MP4.
- Or **Clipchamp** (built into Windows) → *Record* → *Screen and camera* → then trim/cut and add
  captions in the same app.
- Upload to **YouTube as "Unlisted"** (or LinkedIn), then put the link in the README line
  `🎥 Demo video:`.

## Script

| Time | Show | Say |
|---|---|---|
| 0:00–0:15 | README top (diagram) | "This is an AI internship screener I built. HR just adds a candidate to a Google Sheet, and an agentic pipeline handles invitations, a voice interview, scoring and scheduling — while HR keeps the final decision." |
| 0:15–0:30 | Sheet: type a new row, status `New` | "As HR, I add a candidate. n8n polls this sheet; within a few minutes the candidate gets an invite." |
| 0:30–0:40 | Gmail: invite email arrives; Sheet shows `Invited` | "Each candidate gets a unique, expiring link." |
| 0:40–1:30 | Interview page → mic test → Start → answer 2–3 questions (cut the middle) | "The interview runs in the browser with Vapi. The AI says it's an AI, asks for consent, and every candidate gets the same role-specific questions — that's what makes it fair." |
| 1:30–1:50 | n8n Executions: the run going green | "When the call ends, Vapi's report hits n8n, which calls my FastAPI service running a LangGraph agent." |
| 1:50–2:15 | README agent diagram (or `/docs` locally) | "The agent extracts answers, scores them with structured output, re-scores itself if it's not confident, and routes: shortlist, reject, or pause for a human with LangGraph's interrupt — state is checkpointed in Postgres." |
| 2:15–2:35 | Sheet: score, summary, decision filled. Calendar: event with Meet link. Gmail: result email | "Strong candidates get a real slot in HR's calendar with a Google Meet link. Borderline ones go to HR with one-click Approve/Reject buttons and the call recording." |
| 2:35–2:50 | GitHub Actions green + README tech stack | "It's deployed on AWS with Docker and Caddy HTTPS, CI on GitHub Actions, and deploys only after tests pass — with automatic rollback." |
| 2:50–3:00 | Back to the diagram | "Thanks for watching — the code is on GitHub." |

## Tips
- Cut the waiting time (n8n polls every 5 minutes) — say "a few minutes later" and jump.
- Don't show `.env`, tokens, API keys, or the AWS/Meta consoles.
- Add captions (Clipchamp → *Auto captions*) — many people watch muted.
