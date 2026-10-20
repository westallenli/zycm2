"""Scoped, journaled cleanup approved by the user on 2026-10-19.

Run prepare, inspect plan-summary.json, then apply <evidence-directory>.
Only old .p00-runs and four fully verified SDK download archives are removable.
"""
from pathlib import Path
from datetime import datetime
import gzip
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import urllib.request
import zipfile
import zlib
import psutil

W = Path(__file__).resolve().parents[1]
N = W.parent
RUNS = W / '.p00-runs'
RELEASE = N / 'release/秦岭北侧林火演示_20261019_153056'
ZIP_ROOTS = {
    'build-tools_r36_windows.zip': 'build-tools',
    'jdk21.zip': 'jdk',
    'platform-36_r02.zip': 'platform',
    'platform-tools_r37.0.1-win.zip': 'adb',
}
REMOVABLE = {'.py', '.pyc', '.js', '.cjs', '.mjs', '.css', '.yaml', '.yml',
             '.db', '.sqlite', '.sqlite3', '.mbtiles', '.db-wal', '.db-shm'}
ROOT_CACHE = {}
SAFE_DIRECTORIES = set()


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def guard(path, root):
    path = Path(os.path.abspath(path))
    root_key = str(root)
    if root_key not in ROOT_CACHE:
        ROOT_CACHE[root_key] = Path(root).resolve(strict=True)
    root = ROOT_CACHE[root_key]
    if path == root or not path.is_relative_to(root):
        raise RuntimeError(f'Outside scoped root: {path}')
    # Canonical root + normalized absolute descendants + no reparse components
    # establish resolved containment. Cache verified directories, never files.
    for part in [path, *path.parents]:
        if part in SAFE_DIRECTORIES:
            break
        try:
            s = part.lstat()
        except FileNotFoundError:
            continue
        if s.st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise RuntimeError(f'Reparse point forbidden: {part}')
        if stat.S_ISDIR(s.st_mode):
            SAFE_DIRECTORIES.add(part)
        if part == root:
            break
    return path


def files(root):
    for current, dirs, names in os.walk(root, followlinks=False):
        for name in dirs + names:
            p = Path(current) / name
            if p.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise RuntimeError(f'Reparse point forbidden: {p}')
        for name in names:
            yield Path(current) / name


def metadata(path):
    s = path.stat()
    if not s.st_dev or not s.st_ino:
        raise RuntimeError('Real NTFS file identity required; use approved elevated execution')
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns]


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def active_processes():
    result = []
    for p in psutil.process_iter(['name', 'cmdline', 'cwd']):
        if p.pid == os.getpid():
            continue
        try:
            args = p.info['cmdline'] or []
            # Only actual arguments/cwd under the QA root, not shell command text.
            paths = [x for x in args if x.lower().startswith(str(RUNS).lower())]
            cwd = p.info['cwd'] or ''
            if cwd.lower().startswith(str(RUNS).lower() + os.sep):
                paths.append(cwd)
            if paths:
                result.append({'pid': p.pid, 'name': p.info['name'], 'paths': paths})
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    if result:
        raise RuntimeError(f'QA process still running: {result}')
    return [{'pid': p.pid, 'args': p.info['cmdline']} for p in
            psutil.process_iter(['cmdline']) if any(
                a.lower().startswith(str(RELEASE).lower())
                for a in (p.info['cmdline'] or []))]


def sdk_check():
    checks = []
    for name, extracted in ZIP_ROOTS.items():
        print('SDK checking:', name, flush=True)
        archive = N / '.android-tools' / name
        root = N / '.android-tools' / extracted
        count = total = 0
        with zipfile.ZipFile(archive) as z:
            for info in z.infolist():
                if info.is_dir():
                    continue
                target = guard(root / info.filename, root)
                if not target.is_file() or target.stat().st_size != info.file_size:
                    raise RuntimeError(f'Extraction missing/different: {target}')
                crc = 0
                with target.open('rb') as stream:
                    while block := stream.read(1024 * 1024):
                        crc = zlib.crc32(block, crc)
                if crc & 0xffffffff != info.CRC:
                    raise RuntimeError(f'Extraction CRC mismatch: {target}')
                count += 1
                total += info.file_size
        checks.append({'archive': str(archive.relative_to(N)), 'metadata': metadata(archive),
                       'sha256': sha(archive), 'extracted_root': str(root.relative_to(N)),
                       'verified_files': count, 'extracted_bytes': total})
        print('SDK verified:', name, count, flush=True)
    return checks


def state(evidence, old_names):
    unique = {}
    protected = {}
    cache = {}
    count = logical = 0
    for p in files(N):
        if p.is_relative_to(evidence):
            continue
        rel = p.relative_to(N).as_posix()
        m = metadata(p)
        key = tuple(m[:2])
        unique[key] = m[2]
        count += 1
        if count % 50000 == 0:
            print('inventory files:', count, flush=True)
        logical += m[2]
        old = p.is_relative_to(RUNS) and p.relative_to(RUNS).parts[0] in old_names
        sdk = p.parent == N / '.android-tools' and p.name in ZIP_ROOTS
        if old or sdk:
            continue
        # Existing formal evidence is outside mutation roots; file identity,
        # size and mtime verify it without rehashing tens of thousands of copies.
        # Installed SDK contents have already received full CRC verification.
        metadata_only = not (p.is_relative_to(W) or p.is_relative_to(RELEASE)
                             or p.is_relative_to(N / '.android-signing'))
        if not metadata_only and key not in cache:
            cache[key] = sha(p)
        protected[rel] = {'metadata': m, 'sha256': None if metadata_only else cache[key]}
    return {'file_count': count, 'logical_bytes': logical,
            'unique_bytes': sum(unique.values()), 'protected': protected,
            'disk_free': shutil.disk_usage(N).free}


def prepare():
    guard(RUNS, N)
    old = sorted(p.name for p in RUNS.iterdir() if p.is_dir() and '20261019' not in p.name)
    today = sorted(p.name for p in RUNS.iterdir() if p.is_dir() and '20261019' in p.name)
    if len(old) != 888 or len(today) != 4:
        raise RuntimeError(f'Scope changed: old={len(old)}, retained={today}')
    live = active_processes()
    sdk = sdk_check()
    evidence = N / 'docs/开发' / ('旧测试环境清理证据_' + datetime.now().strftime('%Y%m%d_%H%M%S'))
    evidence.mkdir()
    print('evidence:', evidence, flush=True)
    (evidence / 'retained-runs').mkdir()
    before = state(evidence, old)
    with gzip.open(evidence / 'protected-before.json.gz', 'wt', encoding='utf-8') as stream:
        json.dump(before['protected'], stream, ensure_ascii=False)
    del before['protected']
    totals = {'retain_files': 0, 'retain_logical_bytes': 0, 'delete_files': 0,
              'delete_logical_bytes': 0, 'suffixes': {}}
    identities = {}
    with gzip.open(evidence / 'plan.jsonl.gz', 'wt', encoding='utf-8') as stream:
        for name in old:
            root = guard(RUNS / name, RUNS)
            for p in files(root):
                if '.git' in p.relative_to(root).parts:
                    raise RuntimeError(f'Unexpected Git metadata in old QA: {p}')
                m = metadata(p)
                suffix = p.suffix.lower()
                action = 'delete' if suffix in REMOVABLE else 'retain'
                rel = p.relative_to(RUNS).as_posix()
                record = {'path': rel, 'action': action, 'metadata': m}
                if action == 'retain':
                    record['sha256'] = sha(p)
                stream.write(json.dumps(record, ensure_ascii=False) + '\n')
                totals[action + '_files'] += 1
                totals[action + '_logical_bytes'] += m[2]
                identities[tuple(m[:2])] = m[2]
                entry = totals['suffixes'].setdefault(suffix, {'files': 0, 'bytes': 0, 'action': action})
                entry['files'] += 1
                entry['bytes'] += m[2]
    plan = {'approved_scope': '888 pre-20261019 QA roots and 4 verified SDK download ZIPs only',
            'old_run_names': old, 'retained_today': today, 'live_before': live,
            'sdk_checks': sdk, 'before': before, 'totals': totals,
            'old_run_unique_bytes_including_shared_anchors': sum(identities.values()),
            'plan_sha256': sha(evidence / 'plan.jsonl.gz')}
    write(evidence / 'plan-summary.json', plan)
    print(json.dumps({'evidence': str(evidence), 'before': before, 'totals': totals,
                      'sdk_files_verified': sum(x['verified_files'] for x in sdk)}, ensure_ascii=False), flush=True)


def load_plan(evidence):
    with gzip.open(evidence / 'plan.jsonl.gz', 'rt', encoding='utf-8') as stream:
        return [json.loads(line) for line in stream]


def apply(evidence):
    evidence = guard(evidence, N / 'docs/开发')
    plan = json.loads((evidence / 'plan-summary.json').read_text(encoding='utf-8'))
    if (evidence / 'applied.json').exists() or (evidence / 'actions.jsonl').exists():
        raise RuntimeError('Apply already started; inspect journal instead of rerunning blindly')
    if sha(evidence / 'plan.jsonl.gz') != plan['plan_sha256']:
        raise RuntimeError('Plan was changed')
    active_processes()
    if sdk_check() != plan['sdk_checks']:
        raise RuntimeError('SDK extraction/archive changed')
    records = load_plan(evidence)
    old_names = set(plan['old_run_names'])
    for record in records:
        p = guard(RUNS / record['path'], RUNS)
        if p.relative_to(RUNS).parts[0] not in old_names or '20261019' in p.relative_to(RUNS).parts[0]:
            raise RuntimeError('Out of approved scope')
        if metadata(p) != record['metadata']:
            raise RuntimeError(f'File changed after planning: {p}')
    free_before = shutil.disk_usage(N).free
    moved = deleted = 0
    # All evidence is relocated and verified BEFORE the first deletion.
    with (evidence / 'actions.jsonl').open('x', encoding='utf-8', buffering=1) as journal:
        for record in records:
            if record['action'] != 'retain':
                continue
            p = guard(RUNS / record['path'], RUNS)
            target = guard(evidence / 'retained-runs' / record['path'], evidence)
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                raise RuntimeError(f'Archive collision: {target}')
            p.rename(target)
            if metadata(target) != record['metadata'] or sha(target) != record['sha256']:
                raise RuntimeError(f'Archive verification failed: {target}')
            journal.write(json.dumps({'action': 'retained', 'path': record['path']}, ensure_ascii=False) + '\n')
            moved += 1
            if moved % 5000 == 0:
                print('verified retained:', moved, flush=True)
        active_processes()
        print('All evidence preserved; deleting reproducible environments.', flush=True)
        for record in records:
            if record['action'] != 'delete':
                continue
            p = guard(RUNS / record['path'], RUNS)
            if metadata(p) != record['metadata'] or p.suffix.lower() not in REMOVABLE:
                raise RuntimeError(f'Deletion guard mismatch: {p}')
            p.unlink()
            journal.write(json.dumps({'action': 'deleted', 'path': record['path']}, ensure_ascii=False) + '\n')
            deleted += 1
            if deleted % 25000 == 0:
                print('deleted:', deleted, flush=True)
        for name in plan['old_run_names']:
            root = guard(RUNS / name, RUNS)
            for current, dirs, names in os.walk(root, topdown=False, followlinks=False):
                if names:
                    raise RuntimeError(f'Unexpected residual files: {current}')
                guard(Path(current), RUNS).rmdir()
        for check in plan['sdk_checks']:
            p = guard(N / check['archive'], N / '.android-tools')
            if metadata(p) != check['metadata'] or sha(p) != check['sha256']:
                raise RuntimeError(f'SDK archive changed: {p}')
            p.unlink()
            journal.write(json.dumps({'action': 'deleted-sdk-zip', 'path': check['archive']}, ensure_ascii=False) + '\n')
    result = {'retained_files': moved, 'deleted_test_files': deleted,
              'removed_run_dirs': len(old_names), 'removed_sdk_zips': len(plan['sdk_checks']),
              'free_before_mutation': free_before, 'free_after_mutation': shutil.disk_usage(N).free}
    result['observed_drive_free_increase'] = result['free_after_mutation'] - free_before
    write(evidence / 'applied.json', result)
    print(json.dumps(result, ensure_ascii=False), flush=True)


def verify(evidence):
    evidence = guard(evidence, N / 'docs/开发')
    plan = json.loads((evidence / 'plan-summary.json').read_text(encoding='utf-8'))
    applied = json.loads((evidence / 'applied.json').read_text(encoding='utf-8'))
    after = state(evidence, set())
    with gzip.open(evidence / 'protected-before.json.gz', 'rt', encoding='utf-8') as stream:
        before = json.load(stream)
    differences = []
    for name in set(before) | set(after['protected']):
        if before.get(name) != after['protected'].get(name):
            differences.append(name)
    retained_verified = 0
    for r in load_plan(evidence):
        original = RUNS / r['path']
        if original.exists():
            raise RuntimeError(f'Residual original: {original}')
        if r['action'] == 'retain':
            target = evidence / 'retained-runs' / r['path']
            if metadata(target) != r['metadata'] or sha(target) != r['sha256']:
                raise RuntimeError(f'Retained evidence changed: {target}')
            retained_verified += 1
    for name in plan['old_run_names']:
        if (RUNS / name).exists():
            raise RuntimeError(f'Old run still exists: {name}')
    live = active_processes()
    http = {}
    for url in ['http://127.0.0.1:18703/login', 'http://192.168.1.37:18705/login']:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(url, timeout=10) as response:
            http[url] = response.status
    sockets = [{'address': str(c.laddr), 'pid': c.pid} for c in psutil.net_connections('tcp')
               if c.status == psutil.CONN_LISTEN and c.laddr.port in (18703, 18705)]
    del after['protected']
    result = {'applied': applied, 'after_excluding_cleanup_evidence': after,
              'protected_files_before': len(before), 'protected_differences': sorted(differences),
              'retained_evidence_verified': retained_verified, 'live_after': live,
              'http': http, 'listeners': sockets,
              'retained_today': sorted(p.name for p in RUNS.iterdir() if p.is_dir()),
              'all_protected_unchanged': not differences,
              'same_live_process': live == plan['live_before']}
    write(evidence / 'verification.json', result)
    print(json.dumps(result, ensure_ascii=False), flush=True)
    if differences or live != plan['live_before']:
        raise RuntimeError('Review protected differences or service process change')


if __name__ == '__main__':
    operation = sys.argv[1]
    if operation == 'prepare':
        prepare()
    elif operation == 'apply':
        apply(Path(sys.argv[2]))
    elif operation == 'verify':
        verify(Path(sys.argv[2]))
    else:
        raise SystemExit('prepare | apply <evidence> | verify <evidence>')
