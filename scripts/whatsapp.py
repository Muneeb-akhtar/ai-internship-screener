"""Meta WhatsApp Cloud API helper (stdlib only). Run from the repo root:

    $py = ".\\screener-api\\.venv\\Scripts\\python.exe"
    & $py scripts\\whatsapp.py templates          # create our 3 templates (idempotent) + show status
    & $py scripts\\whatsapp.py send-test           # send screening_invite to HR_WHATSAPP_NUMBER
    & $py scripts\\whatsapp.py webhook             # point the app's webhook at our tunnel + subscribe

Why templates? WhatsApp only lets a business START a conversation with a pre-approved
template. Free-form text is allowed only within 24 h after the person last messaged you.
Our three templates are generic on purpose (the API drafts the actual wording):
  screening_invite(name, role, link)   first contact with a candidate
  screening_update(name, text)         every later candidate message (result, reminder, re-invite)
  hr_alert(text)                       every message to HR
  interview_call_permission            asks permission before the ElevenLabs WhatsApp call (no params)
"""

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import vapi  # noqa: E402  (shared .env helpers)

TEMPLATES = [
    {
        "name": "screening_invite",
        "category": "UTILITY",
        "language": "en",
        "components": [{
            "type": "BODY",
            "text": ("Hi {{1}}, thank you for applying for the {{2}} internship. "
                     "Please complete your 5-minute AI screening interview here: {{3}} "
                     "The link is valid for 3 days. Reply to this message if you need help."),
            "example": {"body_text": [["Sara", "Python Developer Intern",
                                       "https://screener.example.com/interview/AbC123xYz"]]},
        }],
    },
    {
        "name": "screening_update",
        "category": "UTILITY",
        "language": "en",
        "components": [{
            "type": "BODY",
            "text": "Hi {{1}}, here is an update on your internship application: {{2}} Thank you, The Hiring Team.",
            "example": {"body_text": [["Sara", "You have been shortlisted. Your interview is on Monday 12 Oct at 10:00 AM PKT."]]},
        }],
    },
    {
        # Used by ElevenLabs before an outbound WhatsApp call: the candidate taps "Allow"
        # (Meta's call_permission_request component), then the AI interviewer calls them.
        "name": "interview_call_permission",
        "category": "UTILITY",
        "language": "en",
        "components": [
            {
                # No {{placeholders}}: ElevenLabs' outbound-call API sends this template without
                # parameters, so any variable here would make Meta reject the permission request.
                "type": "body",
                "text": ("Thank you for applying for our internship. Our AI interviewer would like to call you "
                         "on WhatsApp for a 5-minute screening interview. Please allow the call when you're ready."),
            },
            {"type": "call_permission_request"},
        ],
    },
    {
        "name": "hr_alert",
        "category": "UTILITY",
        "language": "en",
        "components": [{
            "type": "BODY",
            "text": "Screening update for HR: {{1}} Reply to this message to respond.",
            "example": {"body_text": [["Borderline candidate Sara Khan (C001), score 23/40. Reply APPROVE C001-1a2b3c4d or REJECT C001-1a2b3c4d."]]},
        }],
    },
]


def graph(method: str, path: str, body: dict | None = None, token: str | None = None) -> dict:
    env = vapi.read_env()
    url = f"https://graph.facebook.com/{env.get('META_GRAPH_VERSION', 'v25.0')}/{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token or env['META_WA_ACCESS_TOKEN']}", "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        return {"error": json.load(e).get("error", {})}


def cmd_templates(_: argparse.Namespace) -> None:
    env = vapi.read_env()
    waba = env["META_WABA_ID"]
    existing = {t["name"]: t for t in graph("GET", f"{waba}/message_templates?fields=name,status,category&limit=100").get("data", [])}
    for tpl in TEMPLATES:
        if tpl["name"] in existing:
            continue
        result = graph("POST", f"{waba}/message_templates", tpl)
        if "error" in result:
            print(f"  {tpl['name']}: ERROR {result['error'].get('message')} | {result['error'].get('error_user_msg', '')}")
        else:
            print(f"  {tpl['name']}: submitted ({result.get('status')}, category {result.get('category')})")
    print("Status:")
    for t in graph("GET", f"{waba}/message_templates?fields=name,status,category,rejected_reason&limit=100").get("data", []):
        if t["name"] in {x["name"] for x in TEMPLATES}:
            reason = f" - {t['rejected_reason']}" if t.get("rejected_reason") not in (None, "NONE") else ""
            print(f"  {t['name']:18} {t['status']:10} {t['category']}{reason}")


def cmd_send_test(_: argparse.Namespace) -> None:
    env = vapi.read_env()
    to = env["HR_WHATSAPP_NUMBER"]
    body = {"messaging_product": "whatsapp", "to": to, "type": "template",
            "template": {"name": "screening_invite", "language": {"code": env.get("WA_TEMPLATE_LANG", "en")},
                         "components": [{"type": "body", "parameters": [
                             {"type": "text", "text": "Muneeb"}, {"type": "text", "text": "Python Developer Intern"},
                             {"type": "text", "text": f"{env.get('PUBLIC_BASE_URL', 'https://example.com')}/interview/test"}]}]}}
    print(json.dumps(graph("POST", f"{env['META_WA_PHONE_NUMBER_ID']}/messages", body), indent=2))


def cmd_webhook(_: argparse.Namespace) -> None:
    """Configure the app-level webhook (app access token = APP_ID|APP_SECRET) and subscribe the WABA."""
    env = vapi.read_env()
    app_token = f"{env['META_APP_ID']}|{env['META_APP_SECRET']}"
    callback = env["PUBLIC_BASE_URL"].rstrip("/") + "/whatsapp/webhook"
    params = urllib.parse.urlencode({"object": "whatsapp_business_account", "callback_url": callback,
                                     "verify_token": env["META_WEBHOOK_VERIFY_TOKEN"], "fields": "messages",
                                     "include_values": "true"})
    print("app webhook:", graph("POST", f"{env['META_APP_ID']}/subscriptions?{params}", token=app_token))
    print("WABA -> app :", graph("POST", f"{env['META_WABA_ID']}/subscribed_apps"))
    print(f"Callback URL: {callback}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(required=True)
    sub.add_parser("templates").set_defaults(func=cmd_templates)
    sub.add_parser("send-test").set_defaults(func=cmd_send_test)
    sub.add_parser("webhook").set_defaults(func=cmd_webhook)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
