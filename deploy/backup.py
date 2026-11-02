"""Back up an InnoDB snapshot and immutable Parquet artifacts without stopping workers."""
import argparse
from datetime import datetime, timezone
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile

PUBLISHED_ARTIFACT = re.compile(r'(?:candles|curve|validation_curve|orders|decisions)/[a-f0-9]{64}\.parquet')


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def option(value):
    return '"' + str(value).replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r') + '"'


def safe_artifact(root, relative):
    if not PUBLISHED_ARTIFACT.fullmatch(relative):
        raise RuntimeError('Database references an unsupported artifact path')
    path = root / relative
    if any(item.is_symlink() for item in (path, path.parent)) or not path.resolve().is_relative_to(root):
        raise RuntimeError('Artifact symlinks are not supported in backups')
    if not path.is_file():
        raise RuntimeError('A referenced artifact is missing; backup was not published')
    return path


def validate_references(root):
    """Catch wrong roots and damaged referenced files without loading Arrow.

    SQL is snapshotted first. Published files are never garbage-collected, so
    taking all final filenames later also retains earlier snapshot references.
    This current-reference check adds a fail-closed check for operator mistakes.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from backend import db
    references = []
    with db.connect() as con:
        for row in con.execute('SELECT candles FROM datasets').fetchall():
            value = db.parse(row['candles'], {})
            if value.get('storage') == 'parquet':
                references.append(value)
        for row in con.execute('SELECT result FROM experiments WHERE result IS NOT NULL').fetchall():
            references.extend(db.parse(row['result'], {}).get('artifacts', {}).values())
    verified = {}
    for manifest in references:
        relative = manifest['path']
        if relative not in verified:
            verified[relative] = sha256(safe_artifact(root, relative))
        if verified[relative] != manifest.get('sha256'):
            raise RuntimeError('A referenced artifact failed its checksum; backup was not published')
    return verified


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory', default='/www/backup/mk-quant')
    parser.add_argument('--mysqldump', default=os.getenv('MK_MYSQLDUMP') or ('/www/server/mysql/bin/mysqldump' if Path('/www/server/mysql/bin/mysqldump').exists() else shutil.which('mysqldump')))
    args = parser.parse_args()
    required = ['MK_DB_NAME', 'MK_DB_USER', 'MK_DB_PASSWORD']
    if not all(os.getenv(k) for k in required) or not args.mysqldump:
        parser.error('Set MySQL environment variables and provide a compatible mysqldump executable')
    os.umask(0o077)
    directory = Path(args.directory).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    name = 'mk-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    destination = directory / (name + '.tar.gz')
    artifact_root = Path(os.getenv('MK_ARTIFACT_ROOT', 'data/artifacts')).resolve()
    with tempfile.TemporaryDirectory(prefix='.mk-backup-', dir=directory) as temporary:
        temporary = Path(temporary)
        credentials = temporary / 'client.cnf'
        client_options = {
            'host': os.getenv('MK_DB_HOST', '127.0.0.1'), 'port': os.getenv('MK_DB_PORT', '3306'),
            'user': os.environ['MK_DB_USER'], 'password': os.environ['MK_DB_PASSWORD'],
            'default-character-set': 'utf8mb4',
        }
        mode = os.getenv('MK_DB_SSL_MODE', 'disabled' if client_options['host'] in {'127.0.0.1', 'localhost', '::1'} else 'verify_identity')
        if mode == 'disabled':
            client_options['ssl-mode'] = 'DISABLED'
        elif mode == 'verify_identity' and os.getenv('MK_DB_SSL_CA'):
            client_options.update({'ssl-mode': 'VERIFY_IDENTITY', 'ssl-ca': os.environ['MK_DB_SSL_CA']})
        else:
            parser.error('Remote MySQL requires MK_DB_SSL_CA; private connections may explicitly use MK_DB_SSL_MODE=disabled')
        credentials.write_text('[client]\n' + ''.join(k + '=' + option(v) + '\n' for k, v in client_options.items()))
        credentials.chmod(0o600)
        dump = temporary / 'database.sql.gz'
        command = [args.mysqldump, '--defaults-extra-file=' + str(credentials), '--single-transaction', '--quick', '--hex-blob', '--set-gtid-purged=OFF', '--skip-lock-tables', '--no-tablespaces', os.environ['MK_DB_NAME']]
        # Do not expose option contents or command stderr (which may contain connection data).
        with gzip.open(dump, 'wb', compresslevel=6) as compressed, tempfile.TemporaryFile() as errors:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors)
            with process.stdout as output:
                shutil.copyfileobj(output, compressed)
            if process.wait() != 0:
                raise RuntimeError('mysqldump failed; inspect database availability and user privileges locally')
        # Files are immutable and written before their MySQL references commit. Reading
        # the file set AFTER the SQL snapshot contains every referenced object, plus
        # possibly newer harmless objects. No automatic artifact garbage collection.
        verified = validate_references(artifact_root)
        # Only final content-addressed names are visible objects. In particular,
        # .writing-*.parquet files can be changing or disappearing concurrently.
        files = sorted(safe_artifact(artifact_root, str(p.relative_to(artifact_root)))
                       for p in artifact_root.rglob('*.parquet')
                       if PUBLISHED_ARTIFACT.fullmatch(str(p.relative_to(artifact_root)))) if artifact_root.exists() else []
        manifest = {'format': 1, 'created_at': datetime.now(timezone.utc).isoformat(), 'database': os.environ['MK_DB_NAME'], 'sql_sha256': sha256(dump), 'artifacts': []}
        pending = temporary / 'backup.tar.gz'
        with tarfile.open(pending, 'w:gz') as archive:
            archive.add(dump, arcname='database.sql.gz')
            for path in files:
                relative = str(path.relative_to(artifact_root))
                digest = sha256(path)
                if relative in verified and digest != verified[relative]:
                    raise RuntimeError('An artifact changed during backup; backup was not published')
                manifest['artifacts'].append({'path': relative, 'bytes': path.stat().st_size, 'sha256': digest})
                archive.add(path, arcname='artifacts/' + relative, recursive=False)
                if sha256(path) != digest:
                    raise RuntimeError('An artifact changed during backup; backup was not published')
            metadata = temporary / 'manifest.json'
            metadata.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
            archive.add(metadata, arcname='manifest.json')
        # Verify the complete compressed SQL stream before publishing the backup.
        with gzip.open(dump, 'rb') as source:
            for _ in iter(lambda: source.read(1024 * 1024), b''):
                pass
        with pending.open('rb') as stream:
            os.fsync(stream.fileno())
        os.replace(pending, destination)
        destination.chmod(0o600)
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    print(json.dumps({'backup': str(destination), 'parquet_files': len(files), 'sha256': sha256(destination)}))


if __name__ == '__main__':
    main()
