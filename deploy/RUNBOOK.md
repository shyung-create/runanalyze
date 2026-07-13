# runanalyze — operations runbook

Assumes `deploy/bootstrap.sh` has been run and this doc's checklist followed.
Exposure model: Tailscale. Service user: `runanalyze`. App dir:
`/home/runanalyze/runanalyze`.

## Bootstrap

```bash
sudo REPO_URL=https://github.com/<owner>/runanalyze.git \
     TIMEZONE=<Region/City> \
     ./deploy/bootstrap.sh
```
Idempotent — re-run any time (e.g. after `tailscale up`, to pick up the
`tailscale serve` step it skips on the first pass if Tailscale wasn't
authenticated yet). Installs and enables the systemd units but does **not**
start them — that's step 5 below, after secrets exist.

## Place secrets by hand

1. `cp deploy/runanalyze.env.example /home/runanalyze/runanalyze/.env && chmod 600 /home/runanalyze/runanalyze/.env`
2. Fill in `DEEPSEEK_*`, `TELEGRAM_*`.
3. Generate and fill `SESSION_SECRET_KEY`:
   ```bash
   python3 -c "import secrets; print(secrets.token_hex(32))"
   ```
4. `sudo tailscale up` if not already authenticated, then re-run `bootstrap.sh`.
5. Start the web app: `sudo systemctl start runanalyze-web.service`.

No dashboard login to set up — Tailscale's tailnet-only reachability is
the access control (deliberate tradeoff, see `webapp/auth.py`'s module
docstring). Anything reachable at the tailnet URL below is usable
immediately, no credentials beyond Tailscale itself.

## First Garmin auth

Two equivalent paths — pick one:

**A. Through the UI** (recommended — this is what the credential form exists for):
1. Open `https://<instance>.<tailnet>.ts.net/` from a tailnet device.
2. Admin tab → Garmin connection panel → enter your Garmin Connect email + password → Save.
3. SSH in and run the sync once interactively, so any first-time MFA challenge (see CLAUDE.md's decision: MFA disabled on this account — if that ever changes, this manual step becomes necessary every time the cached session dies, not just once) has somewhere to go:
   ```bash
   sudo -u runanalyze bash -c "cd /home/runanalyze/runanalyze && .venv/bin/python .venv/bin/garmindb_cli.py --activities --download --import --analyze --latest"
   ```
   `garmindb_cli.py` installs as a standalone script in the venv's `bin/`, not an importable module — `python -m garmindb_cli` doesn't work. The `cd` matters too: it writes `garmindb.log` to a relative path in whatever directory it's launched from.
4. Confirm `~/.GarminDb/garmin_tokens.json` now exists (`sudo ls -la /home/runanalyze/.GarminDb/`) and the Admin tab's status shows "Connected". (As of garmindb 3.8.0, the session cache is `garmin_tokens.json` — it replaced the older `garth_session` file when garmindb dropped the deprecated `garth` library for a `garminconnect`-based adapter.)

**B. By hand**, if you'd rather not use the web form for the first auth:
```bash
sudo -u runanalyze cp /home/runanalyze/runanalyze/deploy/GarminConnectConfig.example.json \
  /home/runanalyze/.GarminDb/GarminConnectConfig.json
sudo -u runanalyze $EDITOR /home/runanalyze/.GarminDb/GarminConnectConfig.json   # fill credentials.user/password
sudo chmod 600 /home/runanalyze/.GarminDb/GarminConnectConfig.json
```
Then run the interactive sync from step A.3 to confirm it works and clear any MFA prompt.

**Either path — check `settings.metric` before trusting any numbers.** Neither the web form nor the example file above sets this correctly for you; it defaults to `false` (miles), and the web form *never* touches it at all (it only writes `credentials.user`/`password`). If your Garmin account/device actually records in km, `settings.metric` must be `true`, or every distance gets inflated `×1.609344` and every elevation gets shrunk `×0.3048` (`pipeline/garmin_extract.py`'s `detect_garmindb_units()` reads this field to decide whether GarminDB's raw storage is km or miles — get it wrong and `convert_units()` in `refresh.py` "corrects" data that was already right). Symptom to recognize this by if it happens anyway: lap distances landing suspiciously close to `1.61` (a runner's real 1-mile auto-lap, mis-relabeled as km) and paces that look implausibly fast compared to your other runs.
```bash
sudo -u runanalyze $EDITOR /home/runanalyze/.GarminDb/GarminConnectConfig.json   # settings.metric: true if your account is km
```
If you fix this after already publishing wrong data, re-run a refresh (`--no-sync`, since GarminDB itself doesn't need re-syncing) to regenerate corrected JSON — the bad conversion only affects `docs/data/*.json`, never the raw GarminDB databases.

Once everything above succeeds:
```bash
sudo systemctl start runanalyze.timer
```

## Trigger a refresh manually

Either the Admin tab's "Refresh now" button, or directly:
```bash
sudo systemctl start runanalyze.service   # fires the same oneshot the timer uses
```
Both go through the same single-flight lock (`<APP_DIR>/var/refresh.lock`) as
the web-triggered path — starting this while a web-triggered refresh is
running is a harmless no-op (`refresh.py` logs "already running" and exits 0).

## Logs

```bash
journalctl -u runanalyze-web -f      # the always-on dashboard process
journalctl -u runanalyze -f          # the most recent (or currently running) refresh
journalctl -u runanalyze.timer       # schedule / last-triggered info
systemctl list-timers | grep runanalyze
```
Per-job logs (what the Admin tab's log tail is reading) also live at
`<APP_DIR>/var/logs/<job_id>.log` — scrubbed of anything password-shaped
before it ever reaches the browser, but the on-disk file is the raw output,
readable only by `runanalyze` (dir is `0700`).

## Token expiry / auth failure — what it looks like, how to recover

- **Symptom in the UI**: Admin tab's Garmin status shows "Configured, not
  yet verified" or a red "Last failed auth" line instead of "Connected".
- **Symptom in logs**: `journalctl -u runanalyze` shows `GarminSyncError:
  GarminDB sync failed (exit N)`, and no `docs/data/*.json` files change —
  confirmed by design: `refresh.py` now aborts *before* writing/publishing
  anything if the sync fails (this was a real gap fixed in the web-app
  change — it used to log and continue with stale data).
- **You'll also get a Telegram alert** — "⚠️ runanalyze: Garmin sync/auth
  failed: ...", fired from `refresh.py` itself, so this reaches you whether
  the failure happened on the nightly timer or a manually-triggered run.
- **Recovery**: almost always means the cached `~/.GarminDb/garth_session`
  died (OAuth1 tokens run roughly a year) and a fresh login is needed.
  Since MFA is disabled on this account (the chosen option), the *stored*
  password in `GarminConnectConfig.json` should be enough for GarminDB to
  self-heal on the **next** run automatically — no action needed most of
  the time. If it doesn't recover after one nightly cycle, SSH in and run
  the interactive sync command from "First Garmin auth" step A.3 by hand —
  this surfaces any real problem (wrong/changed password, Garmin's own SSO
  endpoint blocking non-browser clients — a known live risk, see the recon
  notes) directly in your terminal instead of through a crashed background job.

## Rotating the Garmin password

If you change your Garmin Connect password (on Garmin's side), update it
here the same way you set it up: Admin tab's Garmin connection panel
(preferred), or hand-edit `~/.GarminDb/GarminConnectConfig.json` and
`chmod 600` it again. Either path is an atomic overwrite — no restart of
`runanalyze-web.service` or `runanalyze.timer` needed, the next sync just
picks up the new value from the file.

## TLS / tunnel health

Tailscale, not Caddy — there's no cert to renew by hand. Verify:
```bash
tailscale status                 # confirms the node is authenticated and online
sudo tailscale serve status      # confirms the 443 -> 127.0.0.1:8000 mapping is active
```
If `tailscale serve` shows nothing, re-run:
```bash
sudo tailscale serve --bg --https=443 8000
```
Tailscale's own certs (via its MagicDNS HTTPS feature) renew automatically;
nothing in this repo manages TLS material.

## HealthData retention / backup

`~/HealthData/DBs` holds raw GarminDB SQLite databases — kept indefinitely
by design (GarminDB's `--latest` incremental sync needs its own history to
know what's new; deleting it forces a full re-download every time). No
backup system exists in this deployment today. If you add one later:

- `~/.GarminDb/GarminConnectConfig.json` (the credential file) and
  `~/.GarminDb/garth_session` (the token cache) **must never** land in a
  backup destination with weaker protection than the `0600` they have on
  the instance — an S3 bucket with default ACLs, an unencrypted rsync
  target, or a backup tool that normalizes permissions on restore would
  all quietly undo the guarantee this whole design is built around.
- `~/HealthData` is raw personal health data — same bar: encrypted at rest
  at the destination, access-controlled, or don't back it up at all.
- Simplest safe default until you actually need backups: exclude both
  paths explicitly from whatever backup tool you adopt, and treat
  `docs/data/*.json` (already derived, aggregated, GPS-free, and — if you
  kept git publishing — already versioned in git) as the thing worth
  backing up instead.

## Rollback

```bash
cd /home/runanalyze/runanalyze
sudo -u runanalyze git log --oneline -5     # find the commit to roll back to
sudo -u runanalyze git checkout <commit>    # detached HEAD at the known-good commit
sudo ./deploy/update.sh                     # NOTE: update.sh assumes a branch, not detached HEAD —
                                             # see below
```
`deploy/update.sh` rebases the current branch onto its origin tracking
branch, which assumes you're moving *forward*. For an actual rollback:
```bash
sudo -u runanalyze git reset --hard <known-good-commit>   # moves the branch pointer back
sudo -u runanalyze /home/runanalyze/runanalyze/.venv/bin/pip install -q -r requirements.txt
sudo cp deploy/*.service deploy/*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart runanalyze-web.service runanalyze.timer
```
This never touches `.env`, `~/.GarminDb`, `~/HealthData`, or `var/` —
same guarantee as `update.sh`, since a code rollback has no business
touching runtime state or credentials. If you kept the git-publish path
and the bad commit already got pushed live, the rollback here only affects
the *instance's* code — you'd separately want to revert the published
commit if `docs/data/*.json` itself was corrupted by the bad run.
