"""Single-process action journal for SQLite/JSON bundles; sessions stay in memory.

Before-images are durable before any participating file is changed. An action
commits only after all file writes return and are flushed. On restart an
unfinished action is rolled back before ledgers are loaded. This is process
interruption recovery, not a distributed or power-loss durability guarantee.
"""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
import uuid

_local = threading.local()
_MEMORY = ('workspace', 'registry', 'ledger', 'review_ledger', 'imagery', 'imports',
           'alarm_inbox', 'active_event_id', 'db_path', 'member_sessions', 'voice_drafts', '_word_previews', '_pending_review_actor')


class RecoveryError(RuntimeError):
    pass


def _atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name+'.recovery-tmp')
    with temporary.open('wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _cleanup(directory):
    directory = Path(directory)
    if not directory.resolve().is_relative_to(directory.parent.resolve()):
        raise RecoveryError('恢复目录越界，拒绝清理。')
    shutil.rmtree(directory)
    if directory.parent.name == '.state-transactions':
        try:
            directory.parent.rmdir()
        except OSError:
            pass


def _allowed(path, files, directories):
    path = Path(path).resolve()
    if '熊猫智脑cola v4' in str(path):
        return False
    return path in files or any(path.is_relative_to(root) for root in directories)


def _restore(directory, files, directories):
    try:
        manifest = json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
        if manifest.get('schema') != 1 or not isinstance(manifest.get('entries'), list):
            raise ValueError('invalid manifest')
        checked = []
        seen = set()
        for index, entry in enumerate(manifest['entries']):
            path = Path(entry['path']).resolve()
            if path in seen or not _allowed(path, files, directories):
                raise ValueError('unapproved or duplicate recovery target')
            seen.add(path)
            value = None
            if entry['existed']:
                value = (directory/f'{index}.before').read_bytes()
                if hashlib.sha256(value).hexdigest() != entry['sha256']:
                    raise ValueError('before-image fingerprint mismatch')
            checked.append((path, value))
        # Validate every entry and fingerprint before restoring the first file.
        for path, value in checked:
            if value is None:
                path.unlink(missing_ok=True)
            else:
                _atomic(path, value)
        return [path for path, _ in checked]
    except Exception as exc:
        raise RecoveryError('保存中断恢复失败；保留恢复凭据，拒绝继续作业，请核对数据目录。') from exc


def recover(root, *, files=(), directories=()):
    root = Path(root)
    if not root.exists():
        return []
    files = {Path(p).resolve() for p in files if p is not None}
    files |= {Path(str(p)+suffix) for p in tuple(files) for suffix in ('-journal', '-wal', '-shm')}
    directories = tuple(Path(p).resolve() for p in directories)
    restored = []
    for directory in sorted(root.iterdir()):
        if not directory.is_dir() or not directory.name.startswith('txn-'):
            continue
        if (directory/'committed').is_file():
            # Cleanup after a completed commit is safe to retry.
            _cleanup(directory)
            continue
        if not (directory/'manifest.json').exists():
            # capture() has not returned, therefore no target was modified yet.
            _cleanup(directory)
            continue
        restored.extend(_restore(directory, files, directories))
        _cleanup(directory)
    return restored


def _paths(state):
    from . import workbench
    files = {Path(getattr(state, name)).resolve() for name in
             ('registry_path', 'materials_path', 'reviews_path', 'imagery_path',
              'imports_path', 'alarm_inbox_path', 'db_path') if getattr(state, name, None)}
    files |= {Path(str(p)+suffix) for p in tuple(files) for suffix in ('-journal', '-wal', '-shm')}
    files.update(snapshot_paths(state.registry, state.registry_path, state.db_path))
    files |= {Path(str(p)+suffix) for p in tuple(files) for suffix in ('-journal', '-wal', '-shm')}
    return files, (Path(workbench.SNAPSHOT_DIR).resolve(),)


def snapshot_paths(registry, registry_path, db_path=None):
    roots = [Path(registry_path).resolve().parent]
    if db_path:
        roots.append(Path(db_path).resolve().parent)
    records = {**registry.events, **registry.archived_test_events}
    return tuple(Path(row.snapshot_path).resolve() for row in records.values()
                 if row.snapshot_path and any(Path(row.snapshot_path).resolve().is_relative_to(root)
                                               for root in roots))


def recovery_snapshot_paths(registry_path, db_path=None):
    """Discover configured snapshots even if the registry write was interrupted."""
    target = Path(registry_path).resolve()
    payloads = []
    try:
        payloads.append(json.loads(target.read_text(encoding='utf-8')))
    except (OSError, ValueError):
        pass  # The journal may contain the intact registry before-image.
    root = target.parent / '.state-transactions'
    for directory in sorted(root.glob('txn-*')):
        if (directory/'committed').exists() or not (directory/'manifest.json').exists():
            continue
        try:
            manifest = json.loads((directory/'manifest.json').read_text(encoding='utf-8'))
            for index, entry in enumerate(manifest['entries']):
                if Path(entry['path']).resolve() == target and entry['existed']:
                    raw = (directory/f'{index}.before').read_bytes()
                    if hashlib.sha256(raw).hexdigest() != entry['sha256']:
                        raise ValueError('registry before-image fingerprint mismatch')
                    payloads.append(json.loads(raw))
        except Exception as exc:
            raise RecoveryError('事件注册表恢复凭据无效；拒绝加载或继续作业。') from exc
    roots = [target.parent]
    if db_path:
        roots.append(Path(db_path).resolve().parent)
    return tuple(Path(row['snapshot_path']).resolve()
                 for payload in payloads for row in {
                     **payload.get('events', {}), **payload.get('archived_test_events', {})}.values()
                 if row.get('snapshot_path') and any(
                     Path(row['snapshot_path']).resolve().is_relative_to(root) for root in roots))


class _Transaction:
    def __init__(self, state):
        self.state = state
        self.directory = Path(state.registry_path).parent/'.state-transactions'/('txn-'+uuid.uuid4().hex)
        self.entries = []
        self.seen = set()

    def capture(self, path):
        path = Path(path).resolve()
        if path in self.seen:
            return
        files, directories = _paths(self.state)
        if not _allowed(path, files, directories):
            raise RecoveryError('作业保存目标未在显式数据/快照配置内，拒绝写入。')
        value = path.read_bytes() if path.exists() else None
        self.directory.mkdir(parents=True, exist_ok=True)
        entry = dict(path=str(path), existed=value is not None,
                     sha256=hashlib.sha256(value).hexdigest() if value is not None else '')
        if value is not None:
            _atomic(self.directory/f'{len(self.entries)}.before', value)
        self.entries.append(entry)
        _atomic(self.directory/'manifest.json', json.dumps(dict(schema=1, entries=self.entries)).encode())
        self.seen.add(path)

    def commit(self):
        if not self.entries:
            return
        for entry in self.entries:
            path = Path(entry['path'])
            if path.is_file():
                with path.open('r+b') as stream:
                    os.fsync(stream.fileno())
        _atomic(self.directory/'committed', b'1')
        # Once committed, cleanup errors cannot change the acknowledged outcome.
        try:
            _cleanup(self.directory)
        except OSError:
            pass

    def rollback(self):
        if self.directory.exists():
            files, directories = _paths(self.state)
            _restore(self.directory, files, directories)
            _cleanup(self.directory)


def before_write(path, *, sqlite=False):
    transaction = getattr(_local, 'transaction', None)
    if transaction is not None:
        transaction.capture(path)
        if sqlite:
            for suffix in ('-journal', '-wal', '-shm'):
                transaction.capture(str(path)+suffix)


@contextmanager
def action(state):
    with state._event_authority_lock:
        parent = getattr(_local, 'transaction', None)
        if parent is not None:
            if parent.state is not state:
                raise RecoveryError('同一作业不能混用不同的数据工作区。')
            yield
            return
        if getattr(state, '_storage_interrupted', False):
            raise RecoveryError('保存曾中断；请重新启动并完成恢复后再作业。')
        originals = {name: getattr(state, name) for name in _MEMORY if hasattr(state, name)}
        memory = deepcopy(originals)
        transaction = _Transaction(state)
        _local.transaction = transaction
        try:
            yield
            transaction.commit()
        except Exception:
            try:
                transaction.rollback()
            except Exception:
                state._storage_interrupted = True
                raise
            for name, value in memory.items():
                original = originals[name]
                # Keep live application references bound to the rolled-back object.
                # Replacing the root would leave callers holding a stale workspace.
                if hasattr(original, '__dict__'):
                    original.__dict__.clear()
                    original.__dict__.update(value.__dict__)
                    value = original
                elif isinstance(original, dict):
                    original.clear()
                    original.update(value)
                    value = original
                setattr(state, name, value)
            state._chief = None
            raise
        except BaseException:
            # Match abrupt interruption: restart must recover, never keep writing.
            state._storage_interrupted = True
            raise
        finally:
            _local.transaction = None


def durable_action(function):
    @wraps(function)
    def wrapped(state, *args, **kwargs):
        with action(state):
            return function(state, *args, **kwargs)
    return wrapped


def install_state_actions(cls):
    for name, function in list(vars(cls).items()):
        if name.startswith('handle_') and callable(function):
            setattr(cls, name, durable_action(function))
