"""Create an SQLite-consistent backup without stopping independent workers."""
import argparse
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--database', default=os.getenv('MK_DB_PATH', '/www/wwwroot/mk-quant/data/mk.sqlite3'))
parser.add_argument('--directory', default='/www/backup/mk-quant')
args = parser.parse_args()
source = Path(args.database).resolve()
if not source.is_file():
    raise SystemExit('Database does not exist: ' + str(source))
os.umask(0o077)
directory = Path(args.directory)
directory.mkdir(parents=True, exist_ok=True)
target = directory / ('mk-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '.sqlite3')
if target.exists():
    raise SystemExit('Refusing to overwrite an existing backup: ' + str(target))
with sqlite3.connect(source.as_uri() + '?mode=ro', uri=True) as src, sqlite3.connect(target) as dst:
    src.backup(dst, pages=1000)
    if dst.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise SystemExit('Backup integrity check failed')
target.chmod(0o600)
print('Verified backup: ' + str(target))
