#!/bin/bash
# Healthcheck for the kalshi-lab systemd service. Checks TWO things,
# not just "is the process alive" -- a hung-but-not-crashed process
# (e.g. stuck on a blocking network call outside the try/except guards)
# would still show "active" to systemctl while producing no data, so
# this also checks that a new row has actually landed recently.
#
# On failure, posts to a private ntfy.sh topic (https://ntfy.sh) --
# zero signup required on either end: this script just POSTs a
# message, and you get a push notification by installing the ntfy app
# (or opening https://ntfy.sh/<topic> in a browser) and subscribing to
# the topic below. Does NOT cover "the whole Droplet is unreachable" --
# nothing running ON the box can alert you if the box itself is down.
# For that, use DigitalOcean's own built-in Droplet monitoring/alerts
# (free, a few clicks in their web console) -- a same-box cron
# structurally cannot cover that failure mode.

set -u

# The topic is a shared secret (anyone holding it can read these alerts and
# post forged ones), so it lives in /etc/kalshi-alerts.env -- chmod 600, not
# in git. It used to be hardcoded here, which would have published it the
# moment this script was committed.
ALERTS_ENV="${KALSHI_ALERTS_ENV:-/etc/kalshi-alerts.env}"
# shellcheck source=/dev/null
[ -r "$ALERTS_ENV" ] && . "$ALERTS_ENV"
if [ -z "${NTFY_TOPIC:-}" ]; then
    # Do NOT continue silently: a healthcheck that cannot alert is worse
    # than no healthcheck, because it looks like everything is fine. cron
    # redirects this into /root/healthcheck.log.
    echo "$(date -u +%FT%TZ) FATAL: NTFY_TOPIC unset (looked in $ALERTS_ENV) -- cannot alert" >&2
    exit 2
fi
DB_PATH="/root/kalshi_liquidity_lab/data/paper_trading.sqlite"
STALE_SECONDS=600  # no new row in 10 min -> treat as stuck, same as down

notify() {
    curl -s -m 10 -d "$1" "https://ntfy.sh/${NTFY_TOPIC}" >/dev/null 2>&1
}

if ! systemctl is-active --quiet kalshi-lab; then
    notify "kalshi-lab service is NOT active on $(hostname) -- systemd status: $(systemctl is-active kalshi-lab 2>&1)"
    exit 1
fi

AGE_INT=$(/root/kalshi_liquidity_lab/venv/bin/python3 -c "
import sqlite3, time
try:
    conn = sqlite3.connect('$DB_PATH', timeout=5)
    last_ts = conn.execute('SELECT MAX(ts) FROM inventory_marks').fetchone()[0]
    print('' if last_ts is None else int(time.time() - last_ts))
except Exception:
    print('')
" 2>/dev/null)

if [ -z "$AGE_INT" ]; then
    notify "kalshi-lab on $(hostname): service is active but could not read inventory_marks from the DB at all"
    exit 1
fi

if [ "$AGE_INT" -gt "$STALE_SECONDS" ]; then
    notify "kalshi-lab on $(hostname): service shows active but no new DB row in ${AGE_INT}s (stuck?)"
    exit 1
fi

exit 0
