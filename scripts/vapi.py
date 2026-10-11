"""Vapi helper (stdlib only). Run from the repo root with the venv's Python:

    $py = ".\\screener-api\\.venv\\Scripts\\python.exe"
    & $py scripts\\vapi.py sync                     # create/update the assistant from vapi/*.{json,md}
    & $py scripts\\vapi.py link --id C100 --name "Sara Khan" --role "Python Developer Intern"
    & $py scripts\\vapi.py calls                    # list your latest Vapi calls
    & $py scripts\\vapi.py evaluate --call <call_id>  # fetch a call's transcript -> POST /evaluate

`sync` and `calls`/`evaluate` need VAPI_PRIVATE_KEY in .env (server-side key: never put it
in the browser or commit it). `link` and `evaluate` need the Docker stack running.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = ROOT / ".env"
VAPI_API = "https://api.vapi.ai"
LOCAL_API = "http://localhost:8000"


# --------------------------------------------------------------------------- helpers
def read_env() -> dict[str, str]:
    env: dict[str, str] = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            env[key.strip()] = value.strip()
    return env


def set_env_value(key: str, value: str) -> None:
    """Replace KEY=... in .env (or append it), keeping everything else untouched."""
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    for i, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def http(method: str, url: str, headers: dict[str, str], body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    # A real User-Agent: Vapi's Cloudflare blocks the default "Python-urllib" one (error 1010)
    base = {"Content-Type": "application/json", "User-Agent": "ai-internship-screener/0.1"}
    req = urllib.request.Request(url, data=data, method=method, headers={**base, **headers})
    try:
        with urllib.request.urlopen(req, timeout=300) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        sys.exit(f"HTTP {e.code} from {method} {url}:\n{e.read().decode(errors='replace')}")


def vapi_headers(env: dict[str, str]) -> dict[str, str]:
    key = env.get("VAPI_PRIVATE_KEY")
    if not key:
        sys.exit("Set VAPI_PRIVATE_KEY in .env (Vapi dashboard -> API Keys -> Private key).")
    return {"Authorization": f"Bearer {key}"}


def api_headers(env: dict[str, str]) -> dict[str, str]:
    return {"X-API-Key": env["SCREENER_API_KEY"]}


# --------------------------------------------------------------------------- commands
def cmd_sync(_: argparse.Namespace) -> None:
    env = read_env()
    assistant = json.loads((ROOT / "vapi" / "assistant.json").read_text(encoding="utf-8"))
    prompt = (ROOT / "vapi" / "assistant-prompt.md").read_text(encoding="utf-8")
    assistant["model"]["messages"] = [{"role": "system", "content": prompt}]

    # End-of-call webhook -> n8n. Vapi must reach it over the internet (https tunnel/domain).
    webhook_base = env.get("N8N_WEBHOOK_URL", "")
    if webhook_base.startswith("https://"):
        assistant["server"] = {
            "url": webhook_base.rstrip("/") + "/webhook/vapi-end-of-call",
            "headers": {"X-Vapi-Secret": env.get("VAPI_WEBHOOK_SECRET", "")},
        }
        print(f"Webhook: {assistant['server']['url']}")
    else:
        print("Webhook: skipped (N8N_WEBHOOK_URL is not a public https URL yet - set up in Phase 5)")

    assistant_id = env.get("VAPI_ASSISTANT_ID")
    if assistant_id:
        result = http("PATCH", f"{VAPI_API}/assistant/{assistant_id}", vapi_headers(env), assistant)
        print(f"Updated assistant {result['id']} ({result.get('name')})")
    else:
        result = http("POST", f"{VAPI_API}/assistant", vapi_headers(env), assistant)
        set_env_value("VAPI_ASSISTANT_ID", result["id"])
        print(f"Created assistant {result['id']} and saved VAPI_ASSISTANT_ID to .env")
        print("Restart the API to load it:  docker compose up -d screener-api")


def cmd_link(args: argparse.Namespace) -> None:
    env = read_env()
    body = {"candidate_id": args.id, "candidate_name": args.name, "role": args.role}
    link = http("POST", f"{LOCAL_API}/interview-links", api_headers(env), body)
    print(f"Interview link for {args.name} ({args.id}), expires {link['expires_at']}:\n{link['url']}")


def cmd_calls(_: argparse.Namespace) -> None:
    env = read_env()
    calls = http("GET", f"{VAPI_API}/call?limit=5", vapi_headers(env))
    for c in calls if isinstance(calls, list) else calls.get("results", []):
        meta = (c.get("assistantOverrides") or {}).get("metadata") or {}
        print(f"{c['id']}  {c.get('createdAt', '')[:19]}  {c.get('status')}  "
              f"ended={c.get('endedReason')}  candidate={meta.get('candidate_id')}")


def cmd_evaluate(args: argparse.Namespace) -> None:
    """Does by hand what the n8n workflow will do in Phase 5."""
    env = read_env()
    call = http("GET", f"{VAPI_API}/call/{args.call}", vapi_headers(env))
    meta = (call.get("assistantOverrides") or {}).get("metadata") or {}
    transcript = (call.get("artifact") or {}).get("transcript") or call.get("transcript") or ""
    if not meta.get("candidate_id"):
        sys.exit("This call has no candidate metadata - was it started from an /interview/ page?")
    duration = None
    if call.get("startedAt") and call.get("endedAt"):
        start = datetime.fromisoformat(call["startedAt"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(call["endedAt"].replace("Z", "+00:00"))
        duration = (end - start).total_seconds()

    status = http("GET", f"{LOCAL_API}/candidates/{meta['candidate_id']}/interview-status", api_headers(env))
    body = {
        "candidate_id": meta["candidate_id"],
        "candidate_name": status["candidate_name"],
        "role": status["role"],
        "transcript": transcript or "(empty)",
        "call_id": call["id"],
        "call_duration_seconds": duration,
        "interview_token": meta.get("interview_token"),
        # Raw recording URLs are private storage links, so HR gets the Vapi dashboard page for the
        # call instead (login required -> candidate audio is never a public link in WhatsApp).
        "recording_url": f"https://dashboard.vapi.ai/calls/{call['id']}",
    }
    if args.rerun:  # new thread id -> evaluate again (e.g. after a prompt change)
        body["call_id"] = f"{call['id']}-rerun-{datetime.now():%H%M%S}"
    print(f"Call {call['id']}: {call.get('endedReason')}, {duration or 0:.0f}s, "
          f"{len(transcript.split())} words. Evaluating (10-60 s)...")
    result = http("POST", f"{LOCAL_API}/evaluate", api_headers(env), body)
    print(json.dumps({k: result[k] for k in ("thread_id", "status", "decision", "sheet_status", "scores", "flags", "messages")}, indent=2, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(required=True)
    sub.add_parser("sync", help="create/update the Vapi assistant").set_defaults(func=cmd_sync)
    p = sub.add_parser("link", help="create an interview link")
    p.add_argument("--id", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--role", default="Python Developer Intern")
    p.set_defaults(func=cmd_link)
    sub.add_parser("calls", help="list recent Vapi calls").set_defaults(func=cmd_calls)
    p = sub.add_parser("evaluate", help="evaluate a finished Vapi call")
    p.add_argument("--call", required=True)
    p.add_argument("--rerun", action="store_true", help="evaluate again as a new thread")
    p.set_defaults(func=cmd_evaluate)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
