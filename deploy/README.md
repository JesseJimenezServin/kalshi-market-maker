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

## STATUS: deployed and verified 2026-10-03

Live on the Droplet. `kalshi-offbox-backup.timer` is armed (daily
03:30 UTC + up to 600s jitter), and the whole path has been exercised:

- normal run: 975MB -> 72.7MB in ~1m40s, read-back size verified
- run via `systemctl start` (not just the shell), exits clean
- forced failure, missing source DB: exit 1, ntfy alert sent
- forced failure, wrong bucket: exit 1, ntfy alert sent
- **restore tested off-box**: downloaded the uploaded object to a laptop
  with the restricted key, gunzipped to 956022784 bytes,
  `PRAGMA integrity_check` = ok, 726967 snapshot_scores rows. A backup
  that has never been restored is a hypothesis, not a backup.

## Install

```bash
# 0. From the laptop, copy this directory up (needs the admin key added first)
rsync -av --rsync-path="sudo rsync" deploy/ root@<droplet-ip>:/root/kalshi_liquidity_lab/deploy/

# 1. Dependencies
apt-get update && apt-get install -y rclone sqlite3   # neither was present

# 2. Configure the object-storage remote. Provider is Backblaze B2 (NOT
#    DigitalOcean: this copy's whole job is to survive the Droplet's own
#    provider failing, so it must not share a blast radius with it).
#
#    USE THE NATIVE b2 BACKEND, NOT THE S3-COMPATIBLE ENDPOINT. With an
#    application key restricted to a single bucket, B2's S3 layer returns
#    403 Forbidden on PUT (confirmed 2026-10-03: the same key authorizes
#    fine via b2_authorize_account with writeFiles, lists fine, and
#    uploads fine through the b2 backend; dropping the ACL header changed
#    nothing). Bucket-scoping the key matters more than using S3, so:
#
#      [offbox]
#      type = b2
#      account = <applicationKeyId>
#      key = <applicationKey>
#
#    Key capabilities: writeFiles, listFiles, readFiles -- scoped to the
#    one bucket. Deliberately NO deleteFiles: the Droplet must not be able
#    to erase the backups that exist to survive the Droplet. readFiles is
#    included so a restore can be done with this same key.
#    Do NOT put a master/account key here -- it can delete every bucket
#    and bypass object-lock governance.
chmod 600 /root/.config/rclone/rclone.conf

# 3. Config file
cp /root/kalshi_liquidity_lab/deploy/kalshi-offbox.env.example /etc/kalshi-offbox.env
chmod 600 /etc/kalshi-offbox.env
vi /etc/kalshi-offbox.env     # set RCLONE_REMOTE and NTFY_TOPIC at minimum

# 4. FIRST RUN BY HAND -- do not skip. Confirms free space, snapshot
#    integrity, upload, and the read-back size check all pass on real data.
/root/kalshi_liquidity_lab/deploy/push_offbox_backup.sh; echo "exit=$?"

# 5. Prove the alerting works before relying on it. A backup you cannot
#    see fail is the bug this replaces.
NTFY_TOPIC=<topic> DB_PATH=/nonexistent \
  /root/kalshi_liquidity_lab/deploy/push_offbox_backup.sh   # expect exit=1 + a push notification

# 6. Arm the timer
cp /root/kalshi_liquidity_lab/deploy/kalshi-offbox-backup.{service,timer} /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now kalshi-offbox-backup.timer
systemctl list-timers kalshi-offbox-backup.timer

# 7. Retention: set a lifecycle rule on the BUCKET (provider console), not
#    a cron here. Suggested: expire objects after 30 days. A shell `rm`
#    can only run while the host it protects against is still healthy.
```

## Laptop retirement -- DONE 2026-10-03

Carried out only after the push above was verified, including the
off-box restore. For reference, the order mattered: removing the pull
first would have left a window with exactly one copy of the data.

```bash
# on the laptop
launchctl unload ~/Library/LaunchAgents/com.kalshi.droplet-backup-pull.plist
rm ~/Library/LaunchAgents/com.kalshi.droplet-backup-pull.plist
./manage.sh stop server
# then drop the KALSHI_DB_PATH line from .env
```

`../pull_droplet_backup.sh` becomes a manual-only recovery tool at that
point. Keep it or delete it, but it should no longer be scheduled.

## Droplet infrastructure now in `droplet/`

`backup_db.py`, `push_latest_backup.sh`, `healthcheck.sh`,
`kalshi-lab.service`, `kalshi-dashboard.service` and root's crontab are
all under `droplet/` as of 2026-10-02/03, so the box is reproducible
from the repo rather than only from its own disk.

Still only on the box, not here: the `authorized_keys` forced-command
entry (contains a public key, harmless but not yet captured), and the
two secret files `/etc/kalshi-alerts.env` and `/etc/kalshi-offbox.env`
plus `/root/.config/rclone/rclone.conf` -- those must stay out of git.
