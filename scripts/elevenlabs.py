"""ElevenLabs Agents setup (stdlib only). Run from the repo root:

    $py = ".\\screener-api\\.venv\\Scripts\\python.exe"
    & $py scripts\\elevenlabs.py check      # API key works? which voices/agent exist
    & $py scripts\\elevenlabs.py sync       # create/update the interviewer agent from vapi/*.md|json
    & $py scripts\\elevenlabs.py webhook    # post-call webhook -> PUBLIC_BASE_URL/elevenlabs/post-call
    & $py scripts\\elevenlabs.py all        # sync + webhook

The agent uses the SAME prompt and first message as the Vapi browser interviewer
(vapi/assistant-prompt.md, vapi/assistant.json), so both channels run an identical,
fair interview. Endpoints/field names were taken from the official SDK (elevenlabs 2.71.0).
Note: ElevenLabs can't change a webhook's URL, so when the tunnel URL changes this script
deletes the old webhook, creates a new one and stores the NEW secret in .env.
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import vapi  # noqa: E402  (shared .env helpers)

API = "https://api.elevenlabs.io"
DEFAULT_LLM = "gemini-3.5-flash"  # listed in the SDK's Llm enum
# Placeholder values ElevenLabs shows in its test console; real values come per call.
PLACEHOLDERS = {
    "candidate_name": "Sara", "role": "Python Developer Intern", "company_name": "Our Company",
    "technical_question": "What is the difference between a list and a tuple in Python?",
    "problem_question": "How would you find duplicate emails in a list of 10,000 customers?",
    "internship_duration": "three months", "internship_schedule": "full-time, Monday to Friday",
    "internship_start": "next month", "internship_faq": "Each intern gets a mentor.",
    "candidate_id": "TEST", "interview_token": "TEST",
}


def call(method: str, path: str, body: dict | None = None) -> dict:
    env = vapi.read_env()
    key = env.get("ELEVENLABS_API_KEY")
    if not key:
        sys.exit("Set ELEVENLABS_API_KEY in .env (elevenlabs.io -> Developers -> API keys).")
    req = urllib.request.Request(f"{API}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"xi-api-key": key, "Content-Type": "application/json",
                                          "User-Agent": "ai-internship-screener/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        sys.exit(f"ElevenLabs {method} {path} -> HTTP {e.code}: {e.read().decode(errors='replace')[:800]}")


def pick_voice(env: dict[str, str]) -> str:
    if env.get("ELEVENLABS_VOICE_ID"):
        return env["ELEVENLABS_VOICE_ID"]
    voices = call("GET", "/v1/voices").get("voices", [])
    # Prefer a calm female premade voice ("Sarah"), else the first available voice
    for v in voices:
        if v.get("name", "").lower().startswith("sarah"):
            return v["voice_id"]
    if not voices:
        sys.exit("No voices available on this ElevenLabs account.")
    return voices[0]["voice_id"]


def agent_config(env: dict[str, str]) -> dict:
    assistant = json.loads((ROOT / "vapi" / "assistant.json").read_text(encoding="utf-8"))
    prompt = (ROOT / "vapi" / "assistant-prompt.md").read_text(encoding="utf-8")
    return {
        "name": "Internship Screener - Ayla",
        "tags": ["ai-internship-screener"],
        "conversation_config": {
            "agent": {
                "first_message": assistant["firstMessage"],  # AI disclosure + consent, word for word
                "language": "en",
                "dynamic_variables": {"dynamic_variable_placeholders": PLACEHOLDERS},
                "prompt": {
                    "prompt": prompt,
                    "llm": env.get("ELEVENLABS_LLM", DEFAULT_LLM),
                    "temperature": 0.3,
                    "max_tokens": 300,
                    "built_in_tools": {"end_call": {"type": "system", "name": "end_call", "description": "",
                                                    "params": {"system_tool_type": "end_call"}}},
                },
            },
            "tts": {"voice_id": pick_voice(env)},
            "turn": {"silence_end_call_timeout": 40},
            "conversation": {"max_duration_seconds": 420},
        },
    }


def cmd_check(_: argparse.Namespace) -> None:
    env = vapi.read_env()
    user = call("GET", "/v1/user/subscription")
    print(f"API key OK. Plan: {user.get('tier')}, characters used {user.get('character_count')}/{user.get('character_limit')}")
    agents = call("GET", "/v1/convai/agents?page_size=20").get("agents", [])
    print("Agents:", [(a.get("name"), a.get("agent_id")) for a in agents] or "none")
    wa = call("GET", "/v1/convai/whatsapp-accounts")
    print("Imported WhatsApp accounts:", json.dumps(wa)[:400])
    print("Configured agent id:", env.get("ELEVENLABS_AGENT_ID") or "(none yet - run sync)")


def cmd_sync(_: argparse.Namespace) -> None:
    env = vapi.read_env()
    config = agent_config(env)
    agent_id = env.get("ELEVENLABS_AGENT_ID")
    if agent_id:
        call("PATCH", f"/v1/convai/agents/{agent_id}", config)
        print(f"Updated agent {agent_id}")
    else:
        agent_id = call("POST", "/v1/convai/agents/create", config)["agent_id"]
        vapi.set_env_value("ELEVENLABS_AGENT_ID", agent_id)
        print(f"Created agent {agent_id} and saved ELEVENLABS_AGENT_ID to .env")


def cmd_webhook(_: argparse.Namespace) -> None:
    env = vapi.read_env()
    base = env.get("PUBLIC_BASE_URL", "")
    if not base.startswith("https://"):
        sys.exit("PUBLIC_BASE_URL must be a public https URL (start the tunnel: scripts/dev_tunnel.py).")
    url = base.rstrip("/") + "/elevenlabs/post-call"
    existing = call("GET", "/v1/workspace/webhooks").get("webhooks", [])
    mine = [w for w in existing if w.get("name") == "ai-internship-screener post-call"]
    if mine and mine[0].get("webhook_url") == url and env.get("ELEVENLABS_WEBHOOK_SECRET"):
        webhook_id = mine[0]["webhook_id"]
        print(f"Webhook already points to {url}")
    else:
        for w in mine:  # URL can't be edited -> replace
            call("DELETE", f"/v1/workspace/webhooks/{w['webhook_id']}")
        created = call("POST", "/v1/workspace/webhooks", {"settings": {
            "auth_type": "hmac", "name": "ai-internship-screener post-call", "webhook_url": url}})
        webhook_id = created["webhook_id"]
        vapi.set_env_value("ELEVENLABS_WEBHOOK_SECRET", created.get("webhook_secret") or "")
        print(f"Created webhook {webhook_id} -> {url} (secret saved to .env)")
    call("PATCH", "/v1/convai/settings", {"webhooks": {
        "post_call_webhook_id": webhook_id, "events": ["transcript", "call_initiation_failure"]}})
    print("Post-call webhook enabled for: transcript, call_initiation_failure")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(required=True)
    sub.add_parser("check").set_defaults(func=cmd_check)
    sub.add_parser("sync").set_defaults(func=cmd_sync)
    sub.add_parser("webhook").set_defaults(func=cmd_webhook)
    sub.add_parser("all").set_defaults(func=lambda a: (cmd_sync(a), cmd_webhook(a)))
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
