#!/usr/bin/env bash
# Pull-based continuous deployment (GitOps). Runs on the server from cron every 5 minutes:
#   */5 * * * * /opt/screener/deploy/auto-deploy.sh >> /opt/screener/backups/deploy.log 2>&1
#
# 1. fetch origin/main
# 2. if it's a new commit, ask the GitHub API whether ALL CI check runs passed for it
# 3. deploy: checkout the commit, rebuild + restart changed containers
# 4. health-check https://$DOMAIN/health; if unhealthy, roll back to the previous commit
#
# Why pull-based: no AWS credentials or SSH keys stored in GitHub, and SSH stays closed
# to the internet. The repo is public, so fetching and the API call need no token.
# .env, secrets/ and backups/ are git-ignored, so `git checkout` never touches them.
set -euo pipefail

APP_DIR=/opt/screener
LOCK=/tmp/screener-deploy.lock
cd "$APP_DIR"
exec 9>"$LOCK"
flock -n 9 || exit 0   # a previous run is still deploying

log() { echo "$(date -Is) $*"; }
env_get() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- | tr -d '\r'; }
COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.prod.yml --profile prod)

FAILED_FILE="$APP_DIR/backups/.failed-deploy"   # last commit that failed its health check

git fetch --quiet origin main
CURRENT=$(git rev-parse HEAD)
TARGET=$(git rev-parse origin/main)
[[ "$CURRENT" == "$TARGET" ]] && exit 0
# Don't retry a commit that was already rolled back (wait for the next fix to be pushed)
[[ -f "$FAILED_FILE" && "$(cat "$FAILED_FILE")" == "$TARGET" ]] && exit 0

# --- CI gate: every check run for TARGET must have completed successfully -------------
REPO=$(git remote get-url origin | sed -E 's#(git@github.com:|https://github.com/)##; s#\.git$##')
RUNS=$(curl -fsS -H "Accept: application/vnd.github+json" \
  "https://api.github.com/repos/${REPO}/commits/${TARGET}/check-runs?per_page=50")
read -r TOTAL PASSED PENDING < <(python3 -c '
import json, sys
runs = json.load(sys.stdin)["check_runs"]
print(len(runs), sum(r["conclusion"] == "success" for r in runs), sum(r["status"] != "completed" for r in runs))
' <<<"$RUNS")
if (( TOTAL == 0 || PENDING > 0 )); then
  log "CI for ${TARGET:0:7} not finished yet (${PASSED}/${TOTAL} passed) - will retry"
  exit 0
fi
if (( PASSED != TOTAL )); then
  log "CI FAILED for ${TARGET:0:7} (${PASSED}/${TOTAL} passed) - not deploying"
  exit 0
fi

# --- deploy -----------------------------------------------------------------------------
deploy() {
  git checkout --quiet --force "$1"
  "${COMPOSE[@]}" up -d --build --remove-orphans >/dev/null
}
healthy() {
  local url
  url="https://$(env_get DOMAIN)/health"
  for _ in $(seq 1 30); do
    curl -fsS --max-time 5 "$url" >/dev/null 2>&1 && return 0
    sleep 5
  done
  return 1
}

log "deploying ${CURRENT:0:7} -> ${TARGET:0:7} ($(git log -1 --format=%s "$TARGET"))"
deploy "$TARGET"
if healthy; then
  log "deployed ${TARGET:0:7} - healthy"
  docker image prune -f >/dev/null
else
  log "UNHEALTHY after deploying ${TARGET:0:7} - rolling back to ${CURRENT:0:7}"
  echo "$TARGET" > "$FAILED_FILE"
  deploy "$CURRENT"
  healthy && log "rollback ok" || log "ROLLBACK ALSO UNHEALTHY - check: docker compose ps / logs"
  exit 1
fi
