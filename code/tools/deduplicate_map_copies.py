"""Explicit local maintenance: retain two independent maps and deduplicate copies.

No recursive deletion, no business/source changes, and no service restart.
"""
from pathlib import Path
import collections
import ctypes
import hashlib
import json
import os
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / 'docs/开发/地图副本去重证据_20261018'
CURRENT = ROOT / 'release/北山林火连贯演示_20261018_103550/maps/google-satellite.sqlite'
ANCHOR = ROOT / 'release/P16_匿名实物_20261012_001507/maps/google-satellite.sqlite'
EXPECTED = '36f867608ae168b173c68c94f6b6e5a45fafe682c81bd7e42f2556c679487c21'
NAME = 'google-satellite.sqlite'


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def signature(path):
    s = path.stat()
    return [s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns]


def relative(path):
    return path.relative_to(ROOT).as_posix()


def allowed(path):
    path = path.resolve(strict=True)
    assert path.is_relative_to(ROOT) and path.name == NAME
    assert path.parent.name == 'maps'
    assert any(path.is_relative_to(base) for base in (
        ROOT / 'code/.p00-runs', ROOT / 'docs/开发'))
    return path


def scan():
    files = []
    for cur, dirs, names in os.walk(ROOT, followlinks=False):
        for directory in list(dirs):
            path = Path(cur) / directory
            assert not path.is_symlink() and not os.path.isjunction(path), path
            if path == EVIDENCE:
                dirs.remove(directory)
        for name in names:
            path = Path(cur) / name
            assert not path.is_symlink(), path
            files.append(path)
    return files


def inventory(files):
    result = {}
    for i, path in enumerate(files, 1):
        before = signature(path)
        # Preserve every non-map file identity/size/time; additionally hash
        # development implementation and all delivery source/business files.
        protected = any(path.is_relative_to(base) for base in (
            ROOT / 'code/impl', ROOT / 'release'))
        value = digest(path) if protected else None
        assert signature(path) == before, ('Concurrent file change', str(path))
        result[relative(path)] = dict(signature=before, sha256=value)
        if i % 50000 == 0:
            print(json.dumps({'inventory_progress': i}), flush=True)
    return result


def allocated(path):
    call = ctypes.windll.kernel32.GetCompressedFileSizeW
    call.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32)]
    call.restype = ctypes.c_uint32
    high = ctypes.c_uint32()
    low = call(str(path), ctypes.byref(high))
    assert low != 0xFFFFFFFF, path
    return (high.value << 32) | low


def prepare():
    assert ROOT == Path(r'D:\熊猫开发项目\智援参谋智能体')
    EVIDENCE.mkdir(exist_ok=True)
    assert not (EVIDENCE / 'before.json').exists()
    assert not (EVIDENCE / 'applied.json').exists()
    files = scan()
    maps = [p for p in files if p.name == NAME]
    assert len(maps) == 135, len(maps)
    identities = collections.defaultdict(list)
    for path in maps:
        identities[tuple(signature(path)[:2])].append(path)
    assert len(identities) == 127
    assert signature(CURRENT)[:2] != signature(ANCHOR)[:2]
    for i, group in enumerate(identities.values(), 1):
        before = signature(group[0])
        assert digest(group[0]) == EXPECTED, str(group[0])
        assert signature(group[0]) == before
        assert group[0].stat().st_nlink == len(group), ('External hard links', group)
        if i % 25 == 0:
            print(json.dumps({'map_hash_progress': i, 'total': len(identities)}), flush=True)
    candidates = [allowed(p) for p in maps if p not in (CURRENT, ANCHOR)]
    nonmaps = inventory([p for p in files if p.name != NAME])
    write(EVIDENCE / 'nonmap-before.json', nonmaps)
    manifest = dict(current=relative(CURRENT), anchor=relative(ANCHOR), sha256=EXPECTED,
        maps=[dict(path=relative(p), signature=signature(p), links=p.stat().st_nlink,
                   allocated=allocated(p)) for p in maps],
        candidate_count=len(candidates), unique_before=len(identities),
        nonmap_count=len(nonmaps),
        nonmap_sha256_count=sum(v['sha256'] is not None for v in nonmaps.values()),
        free_before=shutil.disk_usage(ROOT).free)
    write(EVIDENCE / 'before.json', manifest)
    print(json.dumps({k: manifest[k] for k in ('candidate_count','unique_before','nonmap_count','free_before')}), flush=True)


def apply():
    before = json.loads((EVIDENCE / 'before.json').read_text(encoding='utf-8'))
    assert not (EVIDENCE / 'applied.json').exists(), 'Already applied'
    assert digest(ANCHOR) == EXPECTED and digest(CURRENT) == EXPECTED
    for row in before['maps']:
        assert signature(ROOT / row['path']) == row['signature'], row['path']
    records = []
    with (EVIDENCE / 'operations.jsonl').open('x', encoding='utf-8') as log:
        for row in before['maps']:
            path = ROOT / row['path']
            if path in (CURRENT, ANCHOR):
                continue
            path = allowed(path)
            if path.samefile(ANCHOR):
                records.append(dict(path=row['path'], action='already_shared'))
                continue
            assert path.stat().st_nlink == 1
            stage = path.with_name(NAME + '.dedup-staging-20261018')
            assert stage.parent.resolve().is_relative_to(ROOT)
            assert not stage.exists()
            log.write(json.dumps(dict(path=row['path'], action='begin'), ensure_ascii=False)+'\n')
            log.flush()
            os.link(ANCHOR, stage)
            assert stage.samefile(ANCHOR)
            os.replace(stage, path)
            assert path.samefile(ANCHOR)
            record = dict(path=row['path'], action='deduplicated', released=row['allocated'])
            records.append(record)
            log.write(json.dumps(record, ensure_ascii=False)+'\n')
            log.flush()
    result = dict(records=records,
        replaced=sum(r['action']=='deduplicated' for r in records),
        already_shared=sum(r['action']=='already_shared' for r in records),
        released_allocated_bytes=sum(r.get('released',0) for r in records),
        free_after=shutil.disk_usage(ROOT).free)
    write(EVIDENCE / 'applied.json', result)
    print(json.dumps({k:v for k,v in result.items() if k!='records'}), flush=True)


def verify():
    before = json.loads((EVIDENCE / 'before.json').read_text(encoding='utf-8'))
    assert digest(CURRENT) == digest(ANCHOR) == EXPECTED
    assert signature(CURRENT)[:2] != signature(ANCHOR)[:2]
    current_record = next(r for r in before['maps'] if r['path']==relative(CURRENT))
    assert signature(CURRENT) == current_record['signature']
    for row in before['maps']:
        path = ROOT / row['path']
        assert path.exists() and path.stat().st_size == row['signature'][2]
        if path != CURRENT:
            assert path.samefile(ANCHOR), row['path']
    files = scan()
    nonmaps = inventory([p for p in files if p.name != NAME])
    previous = json.loads((EVIDENCE / 'nonmap-before.json').read_text(encoding='utf-8'))
    changes = sorted(k for k in previous.keys() | nonmaps.keys() if previous.get(k)!=nonmaps.get(k))
    assert not changes, changes[:20]
    result = dict(map_paths_preserved=len(before['maps']), unique_maps_after=2,
        independent_current_preserved=True, map_hash_verified=EXPECTED,
        nonmap_files_unchanged=len(nonmaps), nonmap_changes=changes,
        nonmap_sha256_verified=sum(v['sha256'] is not None for v in nonmaps.values()),
        free_after_verify=shutil.disk_usage(ROOT).free)
    write(EVIDENCE / 'verification.json', result)
    print(json.dumps(result), flush=True)


if __name__ == '__main__':
    {'prepare':prepare, 'apply':apply, 'verify':verify}[sys.argv[1]]()
