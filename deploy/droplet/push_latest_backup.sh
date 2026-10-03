#!/bin/bash
# Forced command for the dedicated backup-pull SSH key (see authorized_keys).
# Dumps the most recent on-box backup's bytes to stdout -- that's ALL this
# key can ever do, regardless of what command a client requests, because
# it's bound with command="..." in authorized_keys.
#
# Backups are gzipped as of 2026-10-02 (see backup_db.py: uncompressed
# retention would have filled the disk mid-run). This still streams RAW
# sqlite bytes so existing pullers keep working unchanged -- it just
# decompresses on the way out. Handles legacy uncompressed backups too.
LATEST=$(ls -t /root/kalshi_liquidity_lab/data/backups/paper_trading_*.sqlite.gz \
                /root/kalshi_liquidity_lab/data/backups/paper_trading_*.sqlite \
         2>/dev/null | head -1)
if [ -z "$LATEST" ]; then
    exit 1
fi
case "$LATEST" in
    *.gz) exec zcat -- "$LATEST" ;;
    *)    exec cat -- "$LATEST" ;;
esac
