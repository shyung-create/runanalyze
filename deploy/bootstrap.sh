#!/usr/bin/env bash
# runanalyze — idempotent instance bootstrap. Safe to re-run: every step
# checks whether it's already done before acting. Run as root (or via
# sudo) on the Oracle instance. Exposure model: Tailscale — no inbound
# ports are opened anywhere by this script.
#
# This script is documentation you can execute, not something to trust
# blindly. Read it before running it. It does NOT start runanalyze-web or
# the timer — that's a deliberate last step in RUNBOOK.md, done only after
# secrets are placed by hand.

set -euo pipefail

SERVICE_USER="runanalyze"
SERVICE_HOME="/home/${SERVICE_USER}"
APP_DIR="${SERVICE_HOME}/runanalyze"
REPO_URL="${REPO_URL:-https://github.com/<owner>/runanalyze.git}"   # override: REPO_URL=... ./bootstrap.sh
TIMEZONE="${TIMEZONE:-}"                                            # override: TIMEZONE=America/Los_Angeles ./bootstrap.sh

log()  { echo "[bootstrap] $*"; }
warn() { echo "[bootstrap] WARNING: $*" >&2; }

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root (sudo ./bootstrap.sh)." >&2
  exit 1
fi

# ---------------------------------------------------------------- 1. OS packages
log "Installing OS packages..."
apt-get update -qq
apt-get install -y -qq \
  build-essential libffi-dev libssl-dev python3-dev python3-venv \
  git cargo curl
# cargo covers `cryptography` falling back to a source build if no aarch64
# wheel matches the exact Python/OpenSSL combo — verify at venv-build time
# (step 4) whether this was actually needed; harmless if unused.

if [ -n "$TIMEZONE" ]; then
  current_tz="$(timedatectl show -p Timezone --value)"
  if [ "$current_tz" != "$TIMEZONE" ]; then
    log "Setting system timezone to $TIMEZONE (was $current_tz)..."
    timedatectl set-timezone "$TIMEZONE"
  else
    log "Timezone already $TIMEZONE."
  fi
else
  warn "No TIMEZONE given — system is on $(timedatectl show -p Timezone --value)." \
       "The nightly timer's midnight schedule is evaluated in this timezone." \
       "Re-run with TIMEZONE=Region/City ./bootstrap.sh to set it."
fi

# ---------------------------------------------------------------- 2. Service user + directories
if id "$SERVICE_USER" &>/dev/null; then
  log "User $SERVICE_USER already exists."
else
  log "Creating user $SERVICE_USER..."
  useradd -m -s /bin/bash "$SERVICE_USER"
fi

for dir in "${SERVICE_HOME}/.GarminDb" "${SERVICE_HOME}/HealthData" "${APP_DIR}"; do
  if [ ! -d "$dir" ]; then
    log "Creating $dir (0700, ${SERVICE_USER}:${SERVICE_USER})..."
    sudo -u "$SERVICE_USER" mkdir -p "$dir"
  fi
  chmod 700 "$dir"
  chown "${SERVICE_USER}:${SERVICE_USER}" "$dir"
done

# ---------------------------------------------------------------- 3. Git clone
if [ -d "${APP_DIR}/.git" ]; then
  log "Repo already cloned at ${APP_DIR}."
else
  log "Cloning ${REPO_URL} to ${APP_DIR}..."
  # OPTIONAL, only if you kept the git-publish path (see README's
  # publishing note — the default recommendation was to drop it, since
  # Tailscale + this web app already serves the live dashboard). If kept,
  # generate a write-capable deploy key instead of HTTPS:
  #   sudo -u runanalyze ssh-keygen -t ed25519 -N "" -f ${SERVICE_HOME}/.ssh/id_ed25519
  #   sudo -u runanalyze ssh-keyscan github.com >> ${SERVICE_HOME}/.ssh/known_hosts
  #   # then add the printed public key at GitHub -> Settings -> Deploy keys,
  #   # "Allow write access" checked, and clone via git@github.com:... instead.
  sudo -u "$SERVICE_USER" git clone "$REPO_URL" "$APP_DIR"
fi

# ---------------------------------------------------------------- 4. Python venv
if [ -x "${APP_DIR}/.venv/bin/python" ]; then
  log "venv already exists — reinstalling requirements to pick up any changes."
else
  log "Creating venv..."
  sudo -u "$SERVICE_USER" python3 -m venv "${APP_DIR}/.venv"
fi
sudo -u "$SERVICE_USER" "${APP_DIR}/.venv/bin/pip" install -q --upgrade pip
sudo -u "$SERVICE_USER" "${APP_DIR}/.venv/bin/pip" install -q -r "${APP_DIR}/requirements.txt"
sudo -u "$SERVICE_USER" "${APP_DIR}/.venv/bin/pip" install -q garmindb
log "venv ready. Check the output above for anything that compiled from source" \
    "(expected to resolve to prebuilt aarch64 wheels — confirm, don't assume)."

# ---------------------------------------------------------------- 5. var/ dir (job state, lock, logs)
sudo -u "$SERVICE_USER" mkdir -p "${APP_DIR}/var/logs"
chmod 700 "${APP_DIR}/var" "${APP_DIR}/var/logs"

# ---------------------------------------------------------------- 6. Tailscale (exposure model)
if command -v tailscale &>/dev/null; then
  log "Tailscale already installed."
else
  log "Installing Tailscale..."
  curl -fsSL https://tailscale.com/install.sh | sh
fi

if tailscale status &>/dev/null; then
  log "Tailscale already authenticated — configuring tailscale serve..."
  tailscale serve --bg 443 http://127.0.0.1:8000
else
  warn "Tailscale is installed but not authenticated yet. Run:" \
       "    sudo tailscale up" \
       "then re-run this script (idempotent) to configure 'tailscale serve'."
fi

# Proof there is zero inbound exposure beyond SSH — check every time.
log "Checking for anything unexpectedly listening on non-loopback interfaces..."
if ss -tlnp 2>/dev/null | grep -q "0.0.0.0:8000\|:::8000"; then
  warn "Something is listening on 8000 on a non-loopback interface — this should never happen." \
       "uvicorn must always be started with --host 127.0.0.1 (see runanalyze-web.service)."
fi

# ---------------------------------------------------------------- 7. systemd units (install, don't start)
log "Installing systemd units (not starting them yet)..."
cp "${APP_DIR}/deploy/runanalyze-web.service" /etc/systemd/system/
cp "${APP_DIR}/deploy/runanalyze.service" /etc/systemd/system/
cp "${APP_DIR}/deploy/runanalyze.timer" /etc/systemd/system/
cp "${APP_DIR}/deploy/runanalyze-notify-failure@.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable runanalyze-web.service >/dev/null
systemctl enable runanalyze.timer >/dev/null
log "Units installed and enabled (autostart on boot), but NOT started —" \
    "start them only after secrets are placed (see checklist below)."

# ---------------------------------------------------------------- checklist
cat <<EOF

============================================================
Bootstrap done. Place these BY HAND before starting anything
(nothing below is generated by this script):

  1. ${APP_DIR}/.env  (chmod 600, owned by ${SERVICE_USER})
     Copy from deploy/runanalyze.env.example and fill in:
       - DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL
       - DASHBOARD_USER, DASHBOARD_PASSWORD_HASH
           generate with: sudo -u ${SERVICE_USER} \\
             ${APP_DIR}/.venv/bin/python ${APP_DIR}/webapp/scripts/hash_password.py
       - SESSION_SECRET_KEY
           generate with: python3 -c "import secrets; print(secrets.token_hex(32))"
       - TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

  2. Garmin credentials — EITHER:
       a) start runanalyze-web.service (step below) and use the Admin
          tab's Garmin connection panel, OR
       b) seed ${SERVICE_HOME}/.GarminDb/GarminConnectConfig.json by hand
          from deploy/GarminConnectConfig.example.json (chmod 600).

  3. If Tailscale isn't authenticated yet: sudo tailscale up,
     then re-run this script.

  4. Once .env is in place:
       sudo systemctl start runanalyze-web.service
       sudo systemctl start runanalyze.timer
       systemctl status runanalyze-web.service

  5. See deploy/RUNBOOK.md for first-Garmin-auth, log locations, and
     what auth failure looks like.
============================================================
EOF
