"""Back up and verify the existing database before/after PPE migration.

Run from the project root. No restore, delete or reset is performed on failure.
Only databases with the prior camera/user constraints already in place qualify.
"""
from collections import Counter
from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import isg_database as db


def quote(name):
    return '"' + name.replace('"', '""') + '"'


def snapshot(connection, columns=None):
    if columns is None:
        tables = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        columns = {table: [row[1] for row in connection.execute(
            f"PRAGMA table_info({quote(table)})")] for table in tables}
    result = {}
    for table, names in columns.items():
        rows = connection.execute(
            f"SELECT {', '.join(map(quote, names))} FROM {quote(table)}").fetchall()
        # Compare every field, including hashes and notes, without printing any.
        result[table] = Counter(repr(row) for row in rows)
    return columns, result


def summary(rows):
    return {table: {"count": sum(values.values()), "sha256": hashlib.sha256(
        repr(sorted(values.items())).encode()).hexdigest()}
        for table, values in rows.items()}


def verify():
    if not db.DB_PATH.is_file():
        raise RuntimeError("Existing database required; refusing to initialize a new one")
    folder = ROOT / 'backups'
    folder.mkdir(exist_ok=True)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S') + '_' + uuid4().hex[:8]
    backup = folder / f'isg_pre_ppe_{stamp}.db'
    with closing(sqlite3.connect(db.DB_PATH.as_uri() + '?mode=ro', uri=True)) as source:
        with closing(sqlite3.connect(backup)) as target:
            source.backup(target)
            assert target.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
            assert target.execute('PRAGMA foreign_key_check').fetchall() == []
            columns, before = snapshot(target)
            if not db._kullanici_yetkileri_foreign_key_is_valid(target):
                raise RuntimeError(f'Legacy table rebuild required; stopped. Backup: {backup}')
            if db._kamera_index_kisitlari_gecerli(target) != (True, True):
                raise RuntimeError(f'Legacy camera constraints missing; stopped. Backup: {backup}')
    print(f'Backup: {backup}', flush=True)
    passes = []
    for iteration in (1, 2):
        db.prepare_database()
        with closing(db.connect_database()) as connection:
            _, after = snapshot(connection, columns)
            if after != before:
                raise RuntimeError('Existing records changed; stopped without automatic restore')
            assert connection.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
            assert connection.execute('PRAGMA foreign_key_check').fetchall() == []
            schema = connection.execute(
                'SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name').fetchall()
            if iteration == 1:
                first_schema = schema
                _, first_rows = snapshot(connection)
            else:
                assert schema == first_schema, 'Second migration changed schema'
                assert snapshot(connection)[1] == first_rows, 'Second migration changed rows'
            passes.append({"iteration": iteration, "old_records_identical": True,
                           "integrity_check": "ok", "foreign_key_errors": 0})
    report = {"database": str(db.DB_PATH), "backup": str(backup),
              "tables_before": summary(before), "passes": passes}
    report_path = folder / f'ppe_migration_{stamp}.json'
    report_path.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps(report, indent=2))
    print(f'Report: {report_path}')


if __name__ == '__main__':
    verify()
