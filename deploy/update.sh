#!/usr/bin/env bash
# runanalyze — update the running deployment to the latest committed code.
# Run as root (or sudo) on the instance.
#
# Never touches: .env, ~/.GarminDb/** (credentials, token cache),
# ~/HealthData/**, or <APP_DIR>/var/** (job state, single-flight lock).
# Every command below is scoped to the git-tracked repo tree and
# /etc/systemd/system — nothing here can reach those paths, by construction
# (grep this file for HealthData/GarminDb/.env if you want to confirm
# yourself before running it).

set -euo pipefail

SERVICE_USER="runanalyze"
APP_DIR="/home/${SERVICE_USER}/runanalyze"

log() { echo "[update] $*"; }

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root (sudo ./update.sh)." >&2
  exit 1
fi

cd "$APP_DIR"

before_rev="$(sudo -u "$SERVICE_USER" git rev-parse HEAD)"

log "Fetching..."
sudo -u "$SERVICE_USER" git fetch origin

branch="$(sudo -u "$SERVICE_USER" git rev-parse --abbrev-ref HEAD)"
log "Rebasing $branch onto origin/$branch..."
sudo -u "$SERVICE_USER" git rebase "origin/${branch}"

after_rev="$(sudo -u "$SERVICE_USER" git rev-parse HEAD)"

if [ "$before_rev" = "$after_rev" ]; then
  log "Already up to date (${before_rev:0:12}). Nothing to do."
  exit 0
fi

log "Updated ${before_rev:0:12} -> ${after_rev:0:12}."

if ! sudo -u "$SERVICE_USER" git diff --quiet "$before_rev" "$after_rev" -- requirements.txt; then
  log "requirements.txt changed — reinstalling..."
  sudo -u "$SERVICE_USER" "${APP_DIR}/.venv/bin/pip" install -q -r requirements.txt
else
  log "requirements.txt unchanged — skipping pip install."
fi

if ! sudo -u "$SERVICE_USER" git diff --quiet "$before_rev" "$after_rev" -- deploy/; then
  log "deploy/ changed — reinstalling systemd units..."
  cp "${APP_DIR}"/deploy/*.service "${APP_DIR}"/deploy/*.timer /etc/systemd/system/
  systemctl daemon-reload
fi

log "Restarting runanalyze-web.service..."
systemctl restart runanalyze-web.service

log "Re-enabling runanalyze.timer (schedule may have changed)..."
systemctl restart runanalyze.timer

systemctl --no-pager status runanalyze-web.service | head -5
log "Done."
