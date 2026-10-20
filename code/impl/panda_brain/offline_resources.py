"""Explicitly licensed, event-scoped immutable local map packages; no fetching.

ZIP envelope: manifest.json plus XYZ tiles/ or map.mbtiles (TMS raster).
Only declared resources are served. Legacy automatic directory probing is not
an authorization source. License/geography declarations remain reported facts.
"""
import base64
import hashlib
import io
import math
import re
import sqlite3
import struct
import zipfile
from datetime import datetime, timezone
from functools import lru_cache
from . import audited_records as ar
from .authorization import Action, AuthorizationPolicy
from .event_authorization import event_grants
from .record_contracts import explicit_time

KIND='offline_map_package'
SOURCE='named-deputy-licensed-local-map'
MAX_ARCHIVE=16*1024*1024
MAX_CONTENT=32*1024*1024
MAX_TILES=4096
TILE=re.compile(r'^tiles/(0|[1-9]\d?)/(0|[1-9]\d*)/(0|[1-9]\d*)\.(png|jpg|jpeg|webp)$')
REL=re.compile(r'^(0|[1-9]\d?)/(0|[1-9]\d*)/(0|[1-9]\d*)\.png$')
FIELDS={'name','source','license','license_ref','valid_until','cache_allowed','export_allowed',
        'coordinate_system','bounds','min_zoom','max_zoom','geography_basis','data_kind'}


def loads(raw):
    import json
    def pairs(rows):
        out={}
        for key,value in rows:
            if key in out:raise ValueError('字段重复。')
            out[key]=value
        return out
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=lambda value: (_ for _ in ()).throw(ValueError('数值非有限。')))


def require(space,actor):
    AuthorizationPolicy(event_grants(space)).require(actor,Action.PUBLISH_MAP,event_id=space.event_id)


def metadata(value):
    if not isinstance(value,dict) or set(value)!=FIELDS:raise ValueError('地图声明字段缺失或越界。')
    for key in ('name','source','license','license_ref','valid_until','geography_basis'):ar.text(value[key])
    explicit_time(value['valid_until'])
    if value['coordinate_system']!='EPSG:3857' or value['data_kind'] not in {'reported','simulated'}:
        raise ValueError('仅接受明确EPSG:3857栅格与报告/模拟标记，不自动纠正坐标偏移。')
    if any(type(value[k]) is not bool for k in ('cache_allowed','export_allowed')):
        raise ValueError('缓存、导出许可须分别明确。')
    if any(type(value[k]) is not int for k in ('min_zoom','max_zoom')) or not 0<=value['min_zoom']<=value['max_zoom']<=22:
        raise ValueError('瓦片层级声明不正确。')
    b=value['bounds']
    if not isinstance(b,list) or len(b)!=4 or any(type(v) not in {int,float} or not math.isfinite(v) for v in b):
        raise ValueError('覆盖范围须为明确经纬度边界。')
    if not (-180<=b[0]<b[2]<=180 and -85.05112878<=b[1]<b[3]<=85.05112878):
        raise ValueError('覆盖范围不正确。')
    return value


def image_type(blob):
    if len(blob)<12:raise ValueError('瓦片不是有效栅格资源。')
    if blob.startswith(b'\x89PNG\r\n\x1a\n') and len(blob)>=24:
        width,height=struct.unpack('>II',blob[16:24])
        if not 0<width<=4096 or not 0<height<=4096:raise ValueError('瓦片尺寸超界。')
        return 'image/png'
    if blob.startswith(b'\xff\xd8'):return 'image/jpeg'
    if blob[:4]==b'RIFF' and blob[8:12]==b'WEBP':return 'image/webp'
    raise ValueError('仅接受PNG/JPEG/WebP栅格；不提供可执行地图资源。')


def coordinates(z,x,y,m):
    if any(type(v) is not int for v in (z,x,y)) or not m['min_zoom']<=z<=m['max_zoom'] or not 0<=x<2**z or not 0<=y<2**z:
        raise ValueError('瓦片坐标或层级越界。')
    n=2**z;west=x/n*360-180;east=(x+1)/n*360-180
    north=math.degrees(math.atan(math.sinh(math.pi*(1-2*y/n))))
    south=math.degrees(math.atan(math.sinh(math.pi*(1-2*(y+1)/n))))
    b=m['bounds']
    if east<=b[0] or west>=b[2] or north<=b[1] or south>=b[3]:raise ValueError('瓦片与声明覆盖范围不相交。')
    return f'{z}/{x}/{y}'


@lru_cache(maxsize=2)
def archive(encoded):
    if not isinstance(encoded,str) or len(encoded)>MAX_ARCHIVE*4//3+8:raise ValueError('地图包超过16MiB边界。')
    try:raw=base64.b64decode(encoded,validate=True)
    except (ValueError,TypeError) as error:raise ValueError('地图包编码损坏。') from error
    if len(raw)>MAX_ARCHIVE:raise ValueError('地图包超过16MiB边界。')
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            entries=z.infolist();names=[i.filename for i in entries]
            if len(entries)>MAX_TILES*2+16 or len(names)!=len(set(names)) or sum(i.file_size for i in entries)>MAX_CONTENT:
                raise ValueError('地图包重复资源或解压大小超界。')
            if any(i.flag_bits&1 or (i.external_attr>>16)&0o170000==0o120000 or '\\' in i.filename or '..' in i.filename.split('/') or i.filename.startswith('/') for i in entries):
                raise ValueError('地图包含加密、链接或越界路径。')
            if 'manifest.json' not in names:raise ValueError('须提供地图资料与使用授权声明manifest.json。')
            if z.getinfo('manifest.json').file_size>16*1024:raise ValueError('地图声明过大。')
            m=metadata(loads(z.read('manifest.json').decode('utf-8')));tiles={}
            def add(zv,x,y,blob):
                key=coordinates(zv,x,y,m)
                if key in tiles or len(tiles)>=MAX_TILES:raise ValueError('重复瓦片或超过4096张边界。')
                if not isinstance(blob,bytes) or len(blob)>1024*1024:raise ValueError('单张瓦片过大。')
                tiles[key]=(blob,image_type(blob))
            files=[i.filename for i in entries if not i.is_dir() and i.filename!='manifest.json']
            if files==['map.mbtiles']:
                db=sqlite3.connect(':memory:')
                try:
                    db.deserialize(z.read('map.mbtiles'));db.execute('PRAGMA trusted_schema=OFF');db.execute('PRAGMA query_only=ON')
                    row=db.execute("SELECT type,sql FROM sqlite_schema WHERE name='tiles'").fetchone()
                    if not row or row[0]!='table' or not row[1] or 'VIRTUAL' in row[1].upper():
                        raise ValueError('MBTiles须提供普通栅格表，不能执行视图或虚表。')
                    # Never run SQL supplied in metadata, use fixed read-only raster rows.
                    for zv,x,row,blob in db.execute('SELECT zoom_level,tile_column,tile_row,tile_data FROM tiles LIMIT ?', (MAX_TILES+1,)):
                        if type(zv) is not int or not 0<=zv<=22 or type(row) is not int:raise ValueError('MBTiles坐标不正确。')
                        add(zv,x,2**zv-1-row,blob)
                finally:db.close()
            else:
                for name in files:
                    match=TILE.fullmatch(name)
                    if not match:raise ValueError('仅接受XYZ栅格路径或单份map.mbtiles。')
                    zv,x,y=map(int,match.groups()[:3]);add(zv,x,y,z.read(name))
            if not tiles:raise ValueError('地图包没有可用栅格瓦片。')
    except (zipfile.BadZipFile,RuntimeError,sqlite3.Error,UnicodeError,KeyError,OverflowError) as error:
        raise ValueError('地图包损坏或格式不支持。') from error
    return raw,m,tiles


def inspect(space):
    rows=ar.records(space,KIND)
    for ordinal,(index,r,v) in enumerate(rows):
        require(ar.prefix(space,index),r.actor_id)
        if r.source!=SOURCE or r.reference.object_id!='map-package' or set(v)!={'operation','basis','archive_base64','sha256','manifest','tile_hashes','previous'}:
            raise ValueError('地图授权记录来源不一致。')
        ar.text(v['basis'])
        if v['operation']=='import':
            raw,m,tiles=archive(v['archive_base64'])
            if hashlib.sha256(raw).hexdigest()!=v['sha256'] or m!=v['manifest'] or {k:hashlib.sha256(b).hexdigest() for k,(b,t) in tiles.items()}!=v['tile_hashes']:
                raise ValueError('地图原始资源与冻结摘要不一致。')
            if explicit_time(m['valid_until'])<=explicit_time(r.times.recorded_at):raise ValueError('导入当时地图使用许可已到期。')
        elif v['operation']=='revoke':
            if not index or not v['previous'] or any(v[k] for k in ('archive_base64','sha256','manifest','tile_hashes')):
                raise ValueError('撤销须引用明确原版，不改旧资源。')
        else:raise ValueError('地图授权动作不正确。')
        previous=rows[ordinal-1][1].id if ordinal else ''
        if v['previous']!=previous:raise ValueError('地图更新版本链断裂。')
    return tuple(rows)


def record(space,actor,*,operation,basis,archive_base64,expected_version,request_id):
    require(space,actor);rows=inspect(space);ar.text(basis)
    old=next((r for _,r,v in rows if r.request_id==request_id),None)
    if old:
        v=loads(old.content_json)
        if (v['operation'],v['basis'],v['archive_base64'])!=(operation,basis,archive_base64):raise ValueError('地图请求号已用于不同内容。')
        return ar.append(space,kind=KIND,object_id='map-package',source=SOURCE,actor=actor,values=v,expected_version=expected_version,request_id=request_id)
    value=dict(operation=operation,basis=basis,archive_base64='',sha256='',manifest={},tile_hashes={},previous=rows[-1][1].id if rows else '')
    if operation=='import':
        raw,m,tiles=archive(archive_base64)
        if explicit_time(m['valid_until'])<=datetime.now(timezone.utc):raise ValueError('地图使用许可已过期。')
        value.update(archive_base64=archive_base64,sha256=hashlib.sha256(raw).hexdigest(),manifest=m,
            tile_hashes={k:hashlib.sha256(b).hexdigest() for k,(b,t) in tiles.items()})
    elif operation=='revoke':
        if archive_base64 or not rows or rows[-1][2]['operation']!='import':raise ValueError('没有当前有效资源可撤销。')
    else:raise ValueError('地图授权动作越界。')
    r=ar.append(space,kind=KIND,object_id='map-package',source=SOURCE,actor=actor,values=value,expected_version=expected_version,request_id=request_id)
    inspect(space);return r


def current(space,*,now=None):
    rows=inspect(space)
    if not rows or rows[-1][2]['operation']!='import':return None
    r,v=rows[-1][1:]
    if explicit_time(v['manifest']['valid_until'])<=(now or datetime.now(timezone.utc)):return None
    return r,v


def tile(space,revision,rel):
    match=REL.fullmatch(rel);selected=current(space)
    if match is None or not selected or selected[0].id!=revision:return None
    _,_,tiles=archive(selected[1]['archive_base64'])
    return tiles.get('/'.join(match.groups()))


def active_for(state):
    eid=state.workspace.event_id or state.active_event_id
    if not eid or eid not in state.registry.events or state.registry.events[eid].status=='registered':return None
    if state.workspace.event_id==eid:return current(state.workspace)
    # Older unbound demonstration workspaces may name an event without an
    # independent snapshot. They have no authorized local map resource.
    if not state.registry.events[eid].snapshot_path:return None
    return current(state._appointment_space(eid)[0])


def view_status(state):
    """A damaged map source blocks supply, while the rest of the read page stays usable."""
    from .panel import PanelActionError
    try:return dict(resource=active_for(state),error='')
    except (ValueError,KeyError,TypeError,OSError,RuntimeError,PanelActionError):
        return dict(resource=None,error='离线地图来源或审计异常，停止供图；请核对恢复，不回退旧资源。')
