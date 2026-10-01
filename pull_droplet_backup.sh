#!/bin/bash
# Opportunistic off-box backup pull: fetches the Droplet's latest on-box
# backup (see backup_db.py there) down to this machine. Uses a dedicated,
# restricted SSH key (~/.ssh/kalshi_droplet_backup) that can ONLY run the
# Droplet's forced push_latest_backup.sh -- see authorized_keys there --
# no password/full-access credential is stored on this laptop for this.
#
# Best-effort by design: this laptop isn't always on, so this can't
# guarantee a backup every N hours the way the Droplet's own on-box
# daily backup can -- it's a second, independent copy for when the
# Droplet's disk itself is the thing that fails.

set -u

DEST_DIR="/Users/jessejimenez/Downloads/kalshi_liquidity_lab/droplet_backups"
KEEP_LAST=7
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
DEST_FILE="${DEST_DIR}/paper_trading_${TIMESTAMP}.sqlite"
LOG_FILE="${DEST_DIR}/pull.log"

mkdir -p "$DEST_DIR"

if ssh -i ~/.ssh/kalshi_droplet_backup -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new \
    root@<droplet-ip> > "$DEST_FILE" 2>>"$LOG_FILE"; then
    SIZE=$(stat -f%z "$DEST_FILE" 2>/dev/null || echo 0)
    if [ "$SIZE" -lt 1000 ]; then
        echo "$(date): pull produced a suspiciously small file (${SIZE} bytes) -- removing" >> "$LOG_FILE"
        rm -f "$DEST_FILE"
        exit 1
    fi
    echo "$(date): pulled ${DEST_FILE} (${SIZE} bytes)" >> "$LOG_FILE"
else
    echo "$(date): pull FAILED (Droplet unreachable or key rejected)" >> "$LOG_FILE"
    rm -f "$DEST_FILE"
    exit 1
fi

# Keep only the last KEEP_LAST local copies (ls -t newest-first, tail
# past the first KEEP_LAST to get the stale ones to remove).
ls -t "$DEST_DIR"/paper_trading_*.sqlite 2>/dev/null | tail -n +$((KEEP_LAST + 1)) | xargs -r rm -f
