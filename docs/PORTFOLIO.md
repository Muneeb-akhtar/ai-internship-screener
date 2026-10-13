# Resume & LinkedIn material

## Resume bullets (pick 2–4)

**AI Internship Screener** — Python, LangGraph, FastAPI, n8n, Docker, AWS · [github.com/Muneeb-akhtar/ai-internship-screener](https://github.com/Muneeb-akhtar/ai-internship-screener)

- Built an end-to-end agentic hiring pipeline: n8n automates invites from Google Sheets, a Vapi
  browser voice agent runs 5-minute screening interviews, and a **LangGraph** agent scores
  transcripts with Pydantic structured output and books Google Meet interviews via the Calendar API.
- Designed a human-in-the-loop decision graph with a **self-reflection re-scoring loop**,
  `interrupt()`-based HR review and a **Postgres checkpointer**, so borderline candidates pause
  for one-click, HMAC-signed approval links and resume after restarts.
- Made scoring fairer for accented speech by treating ASR errors as uncertainty (routing to human
  review instead of lowering scores), found by analysing real call recordings and transcripts.
- Deployed with **Docker Compose on AWS EC2** behind Caddy auto-HTTPS, nightly S3 backups via a
  least-privilege IAM role, and **GitHub Actions** CI with pull-based deployment that only ships
  commits that passed CI and **rolls back automatically**; 94 automated tests with a mocked LLM.

## One-line summary
Agentic AI system that screens internship candidates via voice interviews, scores them with a
LangGraph agent and schedules interviews — with HR in the loop for every hiring decision.

## LinkedIn post (draft)

> I built an **AI Internship Screener** to learn how production agentic systems fit together. 🧠🎙️
>
> HR adds a candidate to a Google Sheet → n8n sends an invite → the candidate does a 5-minute
> voice interview in the browser (Vapi) → a LangGraph agent scores it against a fixed rubric →
> strong candidates get a Google Meet interview booked automatically, borderline ones go to HR
> with one-click Approve/Reject.
>
> Things I learned the hard way:
> • Speech-to-text mishears accents, so the agent now treats possible ASR errors as *uncertainty*
>   and asks a human, instead of penalising the candidate.
> • Human-in-the-loop needs durable state — LangGraph's interrupt + Postgres checkpointer let HR
>   answer days later, even after restarts.
> • Webhooks get retried: idempotency keys stopped duplicate evaluations and double bookings.
>
> Stack: Python, LangGraph, FastAPI, Gemini, n8n, Vapi, Postgres, Docker, Caddy, AWS EC2,
> GitHub Actions. The AI only shortlists — people make the hiring decisions.
>
> 🎥 Demo: <video link> · 💻 Code: https://github.com/Muneeb-akhtar/ai-internship-screener
>
> #AgenticAI #LangGraph #n8n #AIEngineering #Python
