# AI Internship Screener

An agentic AI pipeline that screens internship candidates end to end: WhatsApp invite →
5-minute browser voice interview (Vapi) → LangGraph decision agent → calendar booking and
notifications through n8n. **The AI only shortlists; HR makes the final hiring decision.**

> 🚧 Work in progress. Phase 1 (scaffold + local Docker) is done.

## Stack
FastAPI · LangGraph · Gemini/Claude/OpenAI (switchable) · n8n · Postgres 16 · Vapi ·
Meta WhatsApp Cloud API · Google Calendar · Caddy · Docker Compose · AWS EC2 · GitHub Actions

## Quick start (Windows PowerShell)
```powershell
.\scripts\init-env.ps1                       # creates .env with random secrets
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
docker compose ps                            # wait until everything is "healthy"
Invoke-RestMethod http://localhost:8000/health
```
- n8n editor: http://localhost:5678
- API docs (Swagger): http://localhost:8000/docs

## Repository layout
| Path | What |
|---|---|
| `docker-compose.yml` | Base stack: postgres, n8n, screener-api, caddy (`prod` profile) |
| `docker-compose.dev.yml` | Local overrides: localhost ports, hot reload |
| `postgres/init/` | Creates the `n8n` and `screener` databases on first start |
| `screener-api/` | FastAPI + LangGraph agent |
| `n8n/workflows/` | Exported n8n workflows |
| `vapi/` | Voice assistant prompt + config |
| `deploy/` | EC2 setup + backup scripts |

_Architecture diagram, setup guide and demo come in Phase 7._
