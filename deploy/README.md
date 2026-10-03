# Droplet-side deployment artifacts

Everything in here runs on the **Droplet**, not on a laptop. It exists
because of the 2026-10-02 incident: the off-box backup was a *laptop*
job, it died silently for two days during an unattended run, and the
scripts that keep the Droplet alive (`backup_db.py`,
`push_latest_backup.sh`, the healthcheck cron, the systemd units) were
only ever created directly on the box and were therefore in no
repository at all. If that disk failed, the recovery procedure was gone
with it.

Target state: the Droplet is the whole system, the laptop is a viewing
terminal. See "Target architecture: nothing runs on the laptop" in
`../PROJECT_STATUS.md`.

## STATUS: written, not yet deployed or executed

These files were authored on 2026-10-02 on macOS and are **syntax-checked
only** (`bash -n`). The script targets Linux (`stat -c`, `stat -f
--format`, `logger`) and cannot run on macOS, so it has not been
executed end to end. Do the first run manually and watch it, per below,
before trusting the timer.

## Install

```bash
# 0. From the laptop, copy this directory up (needs the admin key added first)
rsync -av --rsync-path="sudo rsync" deploy/ root@<droplet-ip>:/opt/kalshi_liquidity_lab/deploy/

# 1. Dependencies
apt-get update && apt-get install -y rclone sqlite3

# 2. Configure the object-storage remote. Prefer a provider that is NOT
#    DigitalOcean: this copy's whole job is to survive the Droplet's own
#    provider failing. Backblaze B2 and any S3-compatible target work.
#    Scope the credential to PUT/LIST on one bucket -- no delete.
rclone config          # creates /root/.config/rclone/rclone.conf
chmod 600 /root/.config/rclone/rclone.conf

# 3. Config file
cp /opt/kalshi_liquidity_lab/deploy/kalshi-offbox.env.example /etc/kalshi-offbox.env
chmod 600 /etc/kalshi-offbox.env
vi /etc/kalshi-offbox.env     # set RCLONE_REMOTE and NTFY_TOPIC at minimum

# 4. FIRST RUN BY HAND -- do not skip. Confirms free space, snapshot
#    integrity, upload, and the read-back size check all pass on real data.
/opt/kalshi_liquidity_lab/deploy/push_offbox_backup.sh; echo "exit=$?"

# 5. Prove the alerting works before relying on it. A backup you cannot
#    see fail is the bug this replaces.
NTFY_TOPIC=<topic> DB_PATH=/nonexistent \
  /opt/kalshi_liquidity_lab/deploy/push_offbox_backup.sh   # expect exit=1 + a push notification

# 6. Arm the timer
cp /opt/kalshi_liquidity_lab/deploy/kalshi-offbox-backup.{service,timer} /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now kalshi-offbox-backup.timer
systemctl list-timers kalshi-offbox-backup.timer

# 7. Retention: set a lifecycle rule on the BUCKET (provider console), not
#    a cron here. Suggested: expire objects after 30 days. A shell `rm`
#    can only run while the host it protects against is still healthy.
```

## Only after step 6 is confirmed uploading

Retire the laptop side, in this order (never before, or there is a window
with exactly one copy of the data):

```bash
# on the laptop
launchctl unload ~/Library/LaunchAgents/com.kalshi.droplet-backup-pull.plist
rm ~/Library/LaunchAgents/com.kalshi.droplet-backup-pull.plist
./manage.sh stop server
# then drop the KALSHI_DB_PATH line from .env
```

`../pull_droplet_backup.sh` becomes a manual-only recovery tool at that
point. Keep it or delete it, but it should no longer be scheduled.

## Still to bring into this directory

The Droplet currently holds these, untracked anywhere:

- `backup_db.py` (on-box daily backup via sqlite3's online `.backup()`)
- `push_latest_backup.sh` (the forced command the restricted key runs)
- the 5-minute healthcheck + its ntfy alerting
- `kalshi-lab.service`, `kalshi-dashboard.service`
- `root`'s crontab and the `authorized_keys` forced-command entry

Copy them here and commit, so the Droplet is reproducible from the repo.
