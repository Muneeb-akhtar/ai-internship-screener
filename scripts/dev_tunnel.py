"""Wire the Cloudflare quick-tunnel URL into the local stack. Run from the repo root:

    .\\screener-api\\.venv\\Scripts\\python.exe scripts\\dev_tunnel.py

1. reads the https://<random>.trycloudflare.com URL from the tunnel container's logs
2. sets PUBLIC_BASE_URL and N8N_WEBHOOK_URL in .env
3. recreates screener-api + n8n so they pick it up (interview links, n8n webhook URLs)
4. re-syncs the Vapi assistant so its end-of-call webhook points at n8n through the tunnel
Run it again whenever the tunnel container restarts (the quick-tunnel URL changes).
"""

import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import vapi  # noqa: E402  (reuses its .env helpers and the sync command)

COMPOSE = ["docker", "compose", "-f", "docker-compose.yml", "-f", "docker-compose.dev.yml"]
URL_RE = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")


def tunnel_url(timeout: float = 60) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        logs = subprocess.run(
            [*COMPOSE, "--profile", "tunnel", "logs", "tunnel"], cwd=ROOT, capture_output=True, text=True
        )
        found = URL_RE.findall(logs.stdout + logs.stderr)
        if found:
            return found[-1]  # latest URL if the container restarted
        time.sleep(3)
    sys.exit("No tunnel URL found. Is it running?  docker compose ... --profile tunnel up -d")


def main() -> None:
    url = tunnel_url()
    print(f"Tunnel: {url}")
    vapi.set_env_value("PUBLIC_BASE_URL", url)
    vapi.set_env_value("N8N_WEBHOOK_URL", url + "/")

    print("Recreating screener-api + n8n with the new URL...")
    subprocess.run([*COMPOSE, "up", "-d", "screener-api", "n8n"], cwd=ROOT, check=True)

    if vapi.read_env().get("VAPI_PRIVATE_KEY"):
        print("Re-syncing the Vapi assistant webhook...")
        vapi.cmd_sync(None)

    print(
        "\nPublic URLs:\n"
        f"  Interview pages : {url}/interview/<token>\n"
        f"  Vapi webhook    : {url}/webhook/vapi-end-of-call\n"
        f"  Meta webhook    : {url}/whatsapp/webhook   (set this in the Meta app)\n"
        f"  Health          : {url}/health"
    )


if __name__ == "__main__":
    main()
