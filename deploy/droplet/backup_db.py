"""
Daily on-box backup of paper_trading.sqlite. Uses sqlite3's built-in
online .backup() API (not a raw file copy) so a backup taken while
main.py is actively writing is still guaranteed consistent -- a plain
`cp` of a live SQLite file can capture it mid-write.

Backups are gzipped, which is not cosmetic: measured 2026-10-02, a
661MB snapshot compresses to 47MB (92.9% reduction), because the tables
are almost entirely numeric. Uncompressed, KEEP_LAST full snapshots of a
database growing ~390MB/day would have filled this Droplet's 24GB disk
around day 10 of a 14-day run -- 5 retained copies reach ~17GB on their
own (CONFIRMED by projection from three consecutive on-box backups:
269MB on 10-01, 662MB on 10-02, 939MB live on 10-03). When the disk
fills, main.py's writes fail and the run dies mid-flight, so this is a
run-ending bug, not a tidiness issue.

Keeps the last KEEP_LAST backups, deleting older ones.
"""

import os
import shutil
import sqlite3
import subprocess
import sys
import time
import pathlib
from pathlib import Path

DB_PATH = Path("/root/kalshi-market-maker/data/paper_trading.sqlite")
BACKUP_DIR = Path("/root/kalshi-market-maker/data/backups")
KEEP_LAST = 5

BACKUP_DIR.mkdir(parents=True, exist_ok=True)

# .backup() writes a full copy, then gzip needs room for the archive beside
# it. Refuse rather than be the process that fills the disk and takes
# main.py's writes down with it.
db_bytes = DB_PATH.stat().st_size
free_bytes = shutil.disk_usage(BACKUP_DIR).free
needed = int(db_bytes * 1.3)
if free_bytes < needed:
    print(
        f"ABORT: need ~{needed} bytes free for a {db_bytes}-byte backup, "
        f"have {free_bytes}. Not starting -- freeing space is now urgent.",
        file=sys.stderr,
    )
    sys.exit(1)

timestamp = time.strftime("%Y%m%d_%H%M%S")
backup_path = BACKUP_DIR / f"paper_trading_{timestamp}.sqlite"

source = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
target = sqlite3.connect(str(backup_path))
with target:
    source.backup(target)
source.close()
target.close()

# nice: this shares one vCPU with main.py, which must not be starved.
subprocess.run(["nice", "-n", "10", "gzip", "-6", str(backup_path)], check=True)
gz_path = pathlib.Path(str(backup_path) + ".gz")  # gzip appends .gz
print(
    f"Backed up to {gz_path} "
    f"({db_bytes} -> {gz_path.stat().st_size} bytes)"
)

# Retention covers legacy uncompressed backups too, so any left over from
# before this change get rotated out normally instead of sitting forever.
existing = sorted(
    list(BACKUP_DIR.glob("paper_trading_*.sqlite"))
    + list(BACKUP_DIR.glob("paper_trading_*.sqlite.gz")),
    key=lambda p: p.name,
)
for old in existing[:-KEEP_LAST]:
    old.unlink()
    print(f"Removed old backup {old}")
