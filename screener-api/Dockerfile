# Screener API image: FastAPI + (Phase 2) LangGraph agent
FROM python:3.12-slim

# PYTHONDONTWRITEBYTECODE: no .pyc files; PYTHONUNBUFFERED: logs appear immediately
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /code

# Install dependencies first: this layer is cached until requirements.txt changes,
# so code-only edits rebuild in seconds.
COPY requirements.txt .
RUN pip install -r requirements.txt

# Run as an unprivileged user, not root
RUN useradd --create-home --uid 10001 app
COPY --chown=app:app app ./app
USER app

EXPOSE 8000

# --proxy-headers: trust X-Forwarded-* from Caddy so request URLs show https://
# --no-access-log: our own structured request log (app/main.py) replaces uvicorn's
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*", "--no-access-log"]
