#!/bin/bash
# Healthcheck for the whole Kalshi Liquidity Lab box, run from cron every
# 5 minutes. Checks the things whose silent failure would cost data:
#
#   1. kalshi-lab is active                       (the collector)
#   2. the DB is readable AND actually growing     (a hung-but-"active"
#      process looks fine to systemctl while producing nothing)
#   3. kalshi-dashboard is active                 (non-critical; no data loss)
#   4. kalshi-offbox-backup.timer is still armed
#   5. an off-box push has SUCCEEDED recently
#
# 4 and 5 exist because of the 2026-10-02 incident and its sequel. The
# off-box push alerts loudly when it FAILS, but nothing noticed when the
# old laptop-side pull simply stopped being run at all -- silence was
# read as success for two days. A dead timer, a masked unit, or a push
# that never fires produces no failure to alert on, so the only honest
# check is "when did a push last SUCCEED", which is what the marker file
# written by push_offbox_backup.sh records.
#
# Does NOT cover "the whole Droplet is unreachable" -- nothing running ON
# the box can report that the box is gone. DigitalOcean CPU/memory alert
# policies were enabled 2026-10-03 to cover that; a same-box cron
# structurally cannot.

set -u

# The topic is a shared secret (anyone holding it can read these alerts and
# post forged ones), so it lives in /etc/kalshi-alerts.env -- chmod 600, not
# in git. It used to be hardcoded here, which would have published it the
# moment this script was committed.
ALERTS_ENV="${KALSHI_ALERTS_ENV:-/etc/kalshi-alerts.env}"
# Environment wins over the file, matching push_offbox_backup.sh. A plain
# `.` assigns unconditionally, which silently beat any caller-supplied
# override -- in the push script that exact pattern made a documented
# forced-failure test a no-op, so both scripts now behave the same way and
# both are testable without editing /etc.
_env_topic="${NTFY_TOPIC:-}"
# shellcheck source=/dev/null
[ -r "$ALERTS_ENV" ] && . "$ALERTS_ENV"
[ -n "$_env_topic" ] && NTFY_TOPIC="$_env_topic"
if [ -z "${NTFY_TOPIC:-}" ]; then
    # Do NOT continue silently: a healthcheck that cannot alert is worse
    # than no healthcheck, because it looks like everything is fine. cron
    # redirects this into /root/healthcheck.log.
    echo "$(date -u +%FT%TZ) FATAL: NTFY_TOPIC unset (looked in $ALERTS_ENV) -- cannot alert" >&2
    exit 2
fi

DB_PATH="${DB_PATH:-/root/kalshi_liquidity_lab/data/paper_trading.sqlite}"
PYTHON="/root/kalshi_liquidity_lab/venv/bin/python3"
STALE_SECONDS=600          # no new row in 10 min -> treat as stuck, same as down
OFFBOX_MARKER="${OFFBOX_MARKER:-/var/lib/kalshi-offbox/last-success}"
OFFBOX_STALE_SECONDS=129600  # 36h: the timer is daily, so this tolerates one
                             # missed run plus jitter before complaining
STATE_DIR="${STATE_DIR:-/var/lib/kalshi-healthcheck}"
# Re-alert interval for a condition that stays broken. Without this, a
# 5-minute cron turns one real problem into 288 notifications a day for as
# long as it lasts, which trains you to ignore them -- the opposite of the
# point. The FIRST alert is still immediate.
NOTIFY_THROTTLE_SECONDS=3600

mkdir -p "$STATE_DIR" 2>/dev/null || true

# notify <key> <priority> <message>
# Throttled per key, so independent problems alert independently.
notify() {
    _key="$1"; _prio="$2"; _msg="$3"
    _stamp="${STATE_DIR}/${_key}.last"
    _now=$(date +%s)
    if [ -f "$_stamp" ]; then
        _last=$(cat "$_stamp" 2>/dev/null || echo 0)
        case "$_last" in ''|*[!0-9]*) _last=0 ;; esac
        if [ $((_now - _last)) -lt "$NOTIFY_THROTTLE_SECONDS" ]; then
            echo "$(date -u +%FT%TZ) [$_key] suppressed (throttled): $_msg" >&2
            return 0
        fi
    fi
    if curl -s -m 10 -H "Priority: ${_prio}" -H "Title: kalshi-lab: ${_key}" \
            -d "$_msg" "https://ntfy.sh/${NTFY_TOPIC}" >/dev/null 2>&1; then
        echo "$_now" > "$_stamp"
    else
        echo "$(date -u +%FT%TZ) WARNING: could not deliver alert [$_key]: $_msg" >&2
    fi
}

# Clear a key's throttle state once it recovers, so the NEXT occurrence
# alerts immediately rather than being suppressed by a stale timestamp.
clear_issue() { rm -f "${STATE_DIR}/${1}.last" 2>/dev/null || true; }

HOST=$(hostname)
PROBLEMS=0

# ---- 1 & 2: the collector --------------------------------------------------
if ! systemctl is-active --quiet kalshi-lab; then
    notify "collector-down" "urgent" \
        "kalshi-lab is NOT active on ${HOST} -- systemd says: $(systemctl is-active kalshi-lab 2>&1). DATA COLLECTION HAS STOPPED."
    PROBLEMS=1
else
    clear_issue "collector-down"
    AGE_INT=$("$PYTHON" -c "
import sqlite3, time
try:
    conn = sqlite3.connect('file:${DB_PATH}?mode=ro', uri=True, timeout=5)
    last_ts = conn.execute('SELECT MAX(ts) FROM inventory_marks').fetchone()[0]
    print('' if last_ts is None else int(time.time() - last_ts))
except Exception:
    print('')
" 2>/dev/null)

    if [ -z "$AGE_INT" ]; then
        notify "db-unreadable" "urgent" \
            "kalshi-lab on ${HOST}: service is active but could not read inventory_marks from the DB at all"
        PROBLEMS=1
    elif [ "$AGE_INT" -gt "$STALE_SECONDS" ]; then
        clear_issue "db-unreadable"
        notify "collector-stuck" "urgent" \
            "kalshi-lab on ${HOST}: service shows active but no new DB row in ${AGE_INT}s (stuck?)"
        PROBLEMS=1
    else
        clear_issue "db-unreadable"
        clear_issue "collector-stuck"
    fi
fi

# ---- 3: the dashboard (non-critical: losing it costs no data) ---------------
if ! systemctl is-active --quiet kalshi-dashboard; then
    notify "dashboard-down" "default" \
        "kalshi-dashboard is not active on ${HOST} (no data loss -- the collector is separate, this only affects viewing)"
    PROBLEMS=1
else
    clear_issue "dashboard-down"
fi

# ---- 4: is the off-box backup timer still armed? ---------------------------
if ! systemctl is-active --quiet kalshi-offbox-backup.timer; then
    notify "offbox-timer-down" "urgent" \
        "kalshi-offbox-backup.timer is NOT active on ${HOST} -- off-box backups will silently stop. systemd says: $(systemctl is-active kalshi-offbox-backup.timer 2>&1)"
    PROBLEMS=1
else
    clear_issue "offbox-timer-down"
fi

# ---- 5: has a push actually SUCCEEDED recently? ----------------------------
# The marker is written only on a verified success (upload + read-back size
# check), so a stale marker means no good off-box copy was made recently,
# whatever the reason -- failed run, dead timer, or never fired at all.
if [ ! -f "$OFFBOX_MARKER" ]; then
    notify "offbox-never-succeeded" "urgent" \
        "No off-box backup success marker on ${HOST} (${OFFBOX_MARKER} missing) -- no verified off-box copy has been recorded"
    PROBLEMS=1
else
    clear_issue "offbox-never-succeeded"
    MARKER_AGE=$(( $(date +%s) - $(stat -c %Y "$OFFBOX_MARKER") ))
    if [ "$MARKER_AGE" -gt "$OFFBOX_STALE_SECONDS" ]; then
        notify "offbox-stale" "urgent" \
            "Last successful off-box backup on ${HOST} was $((MARKER_AGE / 3600))h ago (limit $((OFFBOX_STALE_SECONDS / 3600))h). The daily push is not completing -- $(cat "$OFFBOX_MARKER" 2>/dev/null | head -1)"
        PROBLEMS=1
    else
        clear_issue "offbox-stale"
    fi
fi

exit "$PROBLEMS"
