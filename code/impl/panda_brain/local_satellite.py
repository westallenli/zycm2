"""Host-installed OSMDroid/ATAK raster source, always opened read-only.

Installation is explicit in the local launcher. No client-supplied file paths,
downloads, exports, event writes or inferred geographic corrections.
"""
import hashlib
import math
import re
import sqlite3
from pathlib import Path
from urllib.parse import parse_qs, quote
from .offline_resources import image_type

REL = re.compile(r'([a-f0-9]{64})/(0|[1-9]\d?)/(0|[1-9]\d*)/(0|[1-9]\d*)\.jpg')


def index(z, x, y):
    return (z << (2*z)) + (x << z) + y


def decode(key):
    for z in range(23):
        if (z << (2*z)) <= key < ((z+1) << (2*z)):
            n = 1 << z
            return z, (key >> z) % n, key % n
    raise ValueError('瓦片索引越界。')


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def load_installed(root):
    """Explicit host installation; filenames cannot be selected by HTTP clients."""
    from .offline_resources import loads
    root = Path(root).resolve()
    manifest = root / 'maps/local-satellite.json'
    if not manifest.is_file():
        return None
    value = loads(manifest.read_text(encoding='utf-8'))
    if (set(value) != {'file', 'sha256', 'provider', 'basis'}
            or value['file'] != 'google-satellite.sqlite' or not isinstance(value['basis'], str)
            or not value['basis'].strip()):
        raise ValueError('本机地图安装声明不正确。')
    path = (manifest.parent / value['file']).resolve(strict=True)
    if not path.is_relative_to(root):
        raise ValueError('本机地图路径越界。')
    return LocalSatellite(path, sha256=value['sha256'], provider=value['provider'])


class LocalSatellite:
    def __init__(self, path, *, sha256, provider='Google Satellite Only'):
        self.path = Path(path).resolve(strict=True)
        if not re.fullmatch('[a-f0-9]{64}', sha256) or digest(self.path) != sha256:
            raise ValueError('本机地图来源摘要不一致。')
        self.revision, self.provider = sha256, provider
        self.fingerprint = self._stat()
        with self._db() as db:
            for table in ('tiles', 'ATAK_metadata'):
                row = db.execute('SELECT type FROM sqlite_schema WHERE name=?', (table,)).fetchone()
                if row != ('table',):
                    raise ValueError('地图须使用普通数据表。')
            if {r[1] for r in db.execute('PRAGMA table_info(tiles)')} != {'key', 'provider', 'tile'}:
                raise ValueError('不支持此地图数据库结构。')
            if db.execute("SELECT value FROM ATAK_metadata WHERE key='srid'").fetchall() != [('3857',)]:
                raise ValueError('地图未声明EPSG:3857，不猜测投影。')
            levels = set(); count = 0
            for key, source in db.execute('SELECT key,provider FROM tiles'):
                if type(key) is not int or source != provider:
                    raise ValueError('地图索引或来源不一致。')
                levels.add(decode(key)[0]); count += 1
        if not count:
            raise ValueError('地图没有瓦片。')
        self.min_zoom, self.max_zoom, self.count = min(levels), max(levels), count
        self._check()

    def _stat(self):
        s = self.path.stat()
        return s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns

    def _check(self):
        if self._stat() != self.fingerprint:
            raise ValueError('本机地图文件已变化，请重新核对来源后安装。')

    def _db(self):
        # Connection context managers do not close a SQLite handle.
        from contextlib import contextmanager
        @contextmanager
        def connection():
            db = sqlite3.connect(self.path.as_uri()+'?mode=ro', uri=True)
            try:
                db.execute('PRAGMA trusted_schema=OFF')
                db.execute('PRAGMA query_only=ON')
                yield db
            finally:
                db.close()
        return connection()

    def tile(self, z, x, y):
        self._check()
        if any(type(v) is not int for v in (z, x, y)) or not self.min_zoom <= z <= self.max_zoom or not 0 <= x < 2**z or not 0 <= y < 2**z:
            return None
        with self._db() as db:
            row = db.execute('SELECT tile FROM tiles WHERE key=? AND provider=? AND length(tile)<=1048576',
                             (index(z, x, y), self.provider)).fetchone()
        self._check()
        if not row or not isinstance(row[0], bytes):
            return None
        return row[0], image_type(row[0])

    def coverage_zoom(self, lat, lon, preferred=13):
        """Highest initial level with the centre and its adjacent tiles present."""
        self._check()
        with self._db() as db:
            for z in range(min(preferred, self.max_zoom), self.min_zoom-1, -1):
                n = 2**z
                x = min(n-1, max(0, int((lon+180)/360*n)))
                latitude = max(-85.05112878, min(85.05112878, lat))
                mercator = math.asinh(math.tan(math.radians(latitude)))
                y = min(n-1, max(0, int((1-mercator/math.pi)/2*n)))
                keys = {index(z, a, b) for a in range(max(0,x-1),min(n,x+2)) for b in range(max(0,y-1),min(n,y+2))}
                sql = 'SELECT count(*) FROM tiles WHERE provider=? AND key IN ('+','.join('?' for _ in keys)+')'
                if db.execute(sql, (self.provider, *keys)).fetchone()[0] == len(keys):
                    return z
        return None


def description(state, center):
    source = getattr(state, 'local_satellite', None)
    if source is None:
        return None
    from . import offline_resources as maps
    # A current event resource is selected by the renderer first. Explicit
    # revocation still blocks this source. An expired package is never served;
    # it does not prevent a separately approved new host source being used.
    eid = state.active_event_id
    record = state.registry.events.get(eid)
    space = state.workspace
    if record and record.snapshot_path and space.event_id != eid:
        space = state._appointment_space(eid)[0]
    history = maps.inspect(space)
    if history and history[-1][2]['operation'] == 'revoke':
        return None
    zoom = source.coverage_zoom(*(center or [32.0, 110.0]), preferred=13 if center else 9)
    note = '用户提供的本机离线卫星影像；地理位置尚未经控制点核对。'
    if zoom is None:
        note += '当前中心附近没有完整覆盖，需补充该区域地图。'
    elif zoom < (13 if center else 9):
        note += f'当前中心附近完整覆盖至第{zoom}级；放大后可能缺图。'
    return dict(url='/sand/local-map/'+source.revision+'/{z}/{x}/{y}.jpg?event_id='+quote(eid, safe=''),
                revision=source.revision, min=source.min_zoom, max=source.max_zoom,
                zoom=zoom, note=note)


def serve(handler, state, rel, query):
    from . import unified_ui as ui, offline_resources as maps
    from .offline_http import binary
    from .panel import PanelActionError
    try:
        params = parse_qs(query, keep_blank_values=True)
        match = REL.fullmatch(rel)
        eid = state.active_event_id
        record = state.registry.events.get(eid)
        if (not match or params != {'event_id': [eid]} or not eid or not record
                or record.status == 'registered'
                or not (state.command_mode(handler._cookie_token()) or ui.can_read(state, handler._member_cookie()))):
            binary(handler, 404, b'', 'text/plain'); return
        if maps.view_status(state)['error'] or maps.active_for(state):
            binary(handler, 404, b'', 'text/plain'); return
        source = getattr(state, 'local_satellite', None)
        if not source or source.revision != match[1] or not description(state, None):
            binary(handler, 404, b'', 'text/plain'); return
        hit = source.tile(*(int(v) for v in match.groups()[1:]))
        binary(handler, 200 if hit else 404, hit[0] if hit else b'', hit[1] if hit else 'text/plain')
    except (ValueError, KeyError, TypeError, OSError, RuntimeError, PermissionError, sqlite3.Error, PanelActionError):
        binary(handler, 404, b'', 'text/plain')
