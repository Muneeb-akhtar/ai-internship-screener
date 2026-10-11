"""One-time Google OAuth consent for HR's calendar. Run on your PC (not in Docker):

    .\\screener-api\\.venv\\Scripts\\python.exe scripts\\google_auth.py

Needs secrets/google-oauth-client.json (OAuth client of type "Desktop app", downloaded from
Google Cloud Console). Opens your browser; after you approve, it writes
secrets/google-token.json (contains a refresh token - treat it like a password; git-ignored).
The API container reads it from /run/secrets/google-token.json.

`--check` only tests an existing token: reads your free/busy for the next 5 days.
"""

import argparse
import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SECRETS = ROOT / "secrets"
CLIENT_FILE = SECRETS / "google-oauth-client.json"
TOKEN_FILE = SECRETS / "google-token.json"
sys.path.insert(0, str(ROOT / "screener-api"))
from app.tools.calendar import GOOGLE_SCOPES  # noqa: E402  (single source of truth for scopes)


def authorise() -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow

    if not CLIENT_FILE.is_file():
        sys.exit(f"Missing {CLIENT_FILE}. Download the Desktop OAuth client JSON and save it there.")
    flow = InstalledAppFlow.from_client_secrets_file(str(CLIENT_FILE), GOOGLE_SCOPES)
    # Opens the browser; a tiny local web server on a random port receives the result.
    # access_type=offline + prompt=consent guarantee we get a refresh token.
    creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    print(f"Saved {TOKEN_FILE.relative_to(ROOT)} (scopes: {', '.join(s.rsplit('/', 1)[1] for s in creds.scopes or [])})")


def check() -> None:
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build

    if not TOKEN_FILE.is_file():
        sys.exit(f"No token yet - run without --check first ({TOKEN_FILE}).")
    creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GOOGLE_SCOPES)
    service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    now = datetime.now(UTC)
    body = {
        "timeMin": now.isoformat(),
        "timeMax": (now + timedelta(days=5)).isoformat(),
        "items": [{"id": "primary"}],
    }
    result = service.freebusy().query(body=body).execute()
    cal = result["calendars"]["primary"]
    if cal.get("errors"):
        sys.exit(f"Calendar error: {json.dumps(cal['errors'])}")
    print(f"Token works. Busy blocks in the next 5 days: {len(cal.get('busy', []))}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--check", action="store_true", help="only test the saved token")
    if parser.parse_args().check:
        check()
    else:
        authorise()
        check()
