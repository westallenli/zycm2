"""后台刷新调度：只在数据源真的变化时重跑，避免白跑。

迁移自已归档的原始实现。元数据只是唤醒提示，永远不作为证据或授权缓存；
即使输入没变，也由调用方的 interval 兜底定期重核（时效类规则）。

不改变任何权限与判定：这里只决定"要不要现在刷新"，不决定"看到什么"。
"""
from pathlib import Path


def source_stamp(state):
    """给数据源拍一个"变化指纹"：文件名、大小、修改时间。"""
    paths = {Path(p) for name in ('registry_path', 'materials_path', 'reviews_path',
             'imagery_path', 'imports_path', 'alarm_inbox_path', 'db_path')
             if (p := getattr(state, name, None)) is not None}
    records = tuple(getattr(getattr(state, 'registry', None), 'events', {}).items())
    paths.update(Path(record.snapshot_path) for _, record in records if record.snapshot_path)
    files = []
    for path in sorted(paths, key=str):
        for item in (path, Path(str(path) + '-wal')):
            try:
                stat = item.stat()
                files.append((str(item), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
            except FileNotFoundError:
                files.append((str(item), None))
    return (getattr(state, '_background_revision', 0),
            getattr(state, 'workspace', None),
            getattr(getattr(state, 'workspace', None), 'version', None),
            tuple(getattr(state, '_duty_grants', ())),
            tuple((eid, repr(record)) for eid, record in records), tuple(files))


def due(runtime, stamp):
    """该不该现在刷新。手动 last_tick=None 会强制检查一次。"""
    try:
        inputs = source_stamp(runtime.state)
    except OSError:
        # 源不可访问时不要用"没变化"把问题盖住。
        inputs = object()
    except Exception:  # noqa: BLE001 — 指纹算不出来就按"到点"处理
        return True
    if (runtime.last_tick is not None and stamp - runtime.last_tick < runtime.interval
            and inputs == getattr(runtime, '_source_stamp', None)):
        return False
    runtime._source_stamp = inputs
    runtime.last_tick = stamp
    return True


def invalidate(state):
    """有写入发生时调用：让下一次刷新不再被"没变化"跳过。"""
    state._background_revision = getattr(state, '_background_revision', 0) + 1
