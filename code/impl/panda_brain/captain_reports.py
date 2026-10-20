"""Team field reports and protected, normalized photo attachments."""
import base64,hashlib,io,json,re,threading
from urllib.parse import urlencode,parse_qs,urlsplit
from . import captain_field as field,business_modules as business

KIND='CaptainFieldInformationReported'
MAX_PHOTO=1024*1024
MAX_BODY=4*MAX_PHOTO+16384
_uploads=threading.BoundedSemaphore(2)

def rows(space):
    if space.verify_audit_chain():raise ValueError('信息上报审计待核对。')
    out=[];seen=set()
    for e in space.events:
        if e.kind!=KIND:continue
        v=e.payload
        if set(v)!={'event_id','team_id','actor','request_id','data_kind','content','photos'} or v['event_id']!=space.event_id or v['data_kind']!='reported_unverified' or not isinstance(v['photos'],list) or len(v['photos'])>3 or v['request_id'] in seen:raise ValueError('信息上报来源不一致。')
        for p in v['photos']:
            if set(p)!={'sha256','bytes','width','height'} or not re.fullmatch('[0-9a-f]{64}',p['sha256']) or not 0<p['bytes']<=MAX_PHOTO:raise ValueError('上报照片引用不一致。')
        seen.add(v['request_id']);out.append(dict(v,id=e.id,time=e.recorded_at,status='队伍上报·待核实'))
    return out

def normalize(encoded):
    from PIL import Image,ImageOps,UnidentifiedImageError
    if not isinstance(encoded,str) or len(encoded)>((MAX_PHOTO+2)//3)*4:raise ValueError('单张照片须在1MB以内。')
    try:
        raw=base64.b64decode(encoded,validate=True)
        if not 0<len(raw)<=MAX_PHOTO:raise ValueError('单张照片须在1MB以内。')
        with Image.open(io.BytesIO(raw)) as original:
            if original.format!='JPEG' or not 0<original.width<=2048 or not 0<original.height<=2048 or original.width*original.height>4*1024*1024:raise ValueError('请上传压缩后的JPG照片，最长边不超过2048。')
            original.load();image=ImageOps.exif_transpose(original).convert('RGB');image.thumbnail((1600,1600))
            output=io.BytesIO()
            for quality in (85,75,65,55,45):
                output.seek(0);output.truncate();image.save(output,format='JPEG',quality=quality,exif=b'')
                if output.tell()<=MAX_PHOTO:break
            data=output.getvalue()
            if len(data)>MAX_PHOTO:raise ValueError('照片压缩后仍超过1MB，请缩小后重试。')
            return dict(sha256=hashlib.sha256(data).hexdigest(),bytes=len(data),width=image.width,height=image.height),data
    except (UnidentifiedImageError,OSError,Image.DecompressionBombError,ValueError) as e:
        raise ValueError('照片不可读取、格式错误或超过大小限制。') from e

def prepare(handler,state):
    """Bounded upload body and image decoding happen outside shared map locks."""
    from .http_rejection import request_length,reject_json
    from . import android_api
    if not _uploads.acquire(blocking=False):reject_json(handler,dict(ok=False,message='照片上传繁忙，请稍后重试，内容已保留。'),status=429);return False
    try:
        # Only verify the session here; the authoritative event/team check is
        # repeated in the usual scoped transaction after reception finishes.
        with state._event_authority_lock:android_api.session(handler,state)
        length=request_length(handler,MAX_BODY)
        if handler.headers.get('Content-Type','').split(';')[0]!='application/json':raise ValueError('上报格式不正确。')
        handler.connection.settimeout(15);raw=handler.rfile.read(length)
        if len(raw)!=length:raise ValueError('照片上传未完成，请保留内容后重试。')
        def unique(pairs):
            result={}
            for k,v in pairs:
                if k in result:raise ValueError('上报字段重复。')
                result[k]=v
            return result
        value=json.loads(raw.decode('utf-8'),object_pairs_hook=unique)
        if not isinstance(value,dict) or set(value)!={'team_id','content','request_id','photos'}:raise ValueError('信息上报字段不正确。')
        for key,limit in [('team_id',100),('content',2000),('request_id',100)]:field.checked_text(value[key],key,limit)
        if not isinstance(value['photos'],list) or len(value['photos'])>3:raise ValueError('每次最多附带3张照片。')
        handler._field_report=(value,[normalize(p) for p in value['photos']]);return True
    except PermissionError as e:reject_json(handler,dict(ok=False,message=str(e)),status=403)
    except (ValueError,TypeError,KeyError,UnicodeError) as e:reject_json(handler,dict(ok=False,message=str(e)),status=409)
    except (OSError,ImportError):reject_json(handler,dict(ok=False,message='照片处理暂未完成，请保留内容后重试。'),status=503)
    finally:_uploads.release()
    return False

def photo_path(state,eid,digest):
    from . import workbench
    from pathlib import Path
    root=Path(workbench.SNAPSHOT_DIR).resolve()
    path=root/'captain-photos'/hashlib.sha256(eid.encode()).hexdigest()/(digest+'.jpg')
    if not path.resolve().is_relative_to(root):raise ValueError('照片存储路径不正确。')
    return path

def submit(state,token,value,prepared):
    actor,eid,owned=field.context(state,token)
    if value['team_id'] not in owned:raise PermissionError('只能代表本人队伍上报信息。')
    values=dict(event_id=eid,team_id=value['team_id'],actor=actor.phone,request_id=value['request_id'],data_kind='reported_unverified',content=value['content'].strip(),photos=[p for p,data in prepared])
    for old in rows(state.workspace):
        if old['request_id']==value['request_id']:
            if any(old[k]!=v for k,v in values.items()):raise ValueError('该请求已提交不同内容，请先查看上报记录。')
            return dict(ok=True,message='这条信息已上报。',record_id=old['id'])
    from .durable_storage import before_write
    for p,data in prepared:
        path=photo_path(state,eid,p['sha256'])
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest()!=p['sha256']:raise ValueError('已存照片校验失败，请核对。')
        else:
            before_write(path);path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(data)
    event=state.workspace.append_event(KIND,**values);state.persist_if_db();state._persist_active_task_snapshot()
    return dict(ok=True,message='信息已上报，待上级核实。',record_id=event.id)

def list_for(state,token,*,all_event=False):
    actor,eid,owned=field.context(state,token)
    return [r for r in rows(state.workspace) if all_event or r['team_id'] in owned][-50:]

def photo_url(row,index,*,native=False):
    return ('/app/v2/report-photo' if native else '/captain-reports/photo')+'?'+urlencode(dict(event_id=row['event_id'],report_id=row['id'],index=index))

def serve_photo(handler,state,token,*,native=False):
    q=parse_qs(urlsplit(handler.path).query,keep_blank_values=True,max_num_fields=3)
    if set(q)!={'event_id','report_id','index'} or any(len(v)!=1 for v in q.values()) or not re.fullmatch('[012]',q['index'][0]):raise ValueError('照片参数不正确。')
    eid=q['event_id'][0]
    if eid!=state.active_event_id or state.workspace.event_id!=eid:raise PermissionError('照片不属于当前事件。')
    owner=not native and state.session_valid(handler._cookie_token())
    role=business.role(state,token)
    if not owner and (eid not in business.events(state,token) or role not in {'duty','deputy','captain'}):raise PermissionError('无权查看该照片。')
    row=next((r for r in rows(state.workspace) if r['id']==q['report_id'][0]),None)
    if not row or not owner and role=='captain' and row['team_id'] not in business.teams(state,token,eid):raise PermissionError('无权查看其他队伍照片。')
    index=int(q['index'][0])
    if index>=len(row['photos']):raise ValueError('照片不存在。')
    p=row['photos'][index];data=photo_path(state,eid,p['sha256']).read_bytes()
    if len(data)!=p['bytes'] or hashlib.sha256(data).hexdigest()!=p['sha256']:raise ValueError('照片文件缺失或校验失败。')
    handler.send_response(200)
    for k,v in [('Content-Type','image/jpeg'),('Content-Length',str(len(data))),('Cache-Control','no-store'),('X-Content-Type-Options','nosniff')]:handler.send_header(k,v)
    handler.end_headers();handler.wfile.write(data)

def intel_rows(state):
    return [dict(id=r['id'],title=field.team_name(state,r['team_id'])+' · '+r['time'],text=r['content']+'；'+r['status']+f"；照片{len(r['photos'])}张",photo_links=[photo_url(r,i) for i in range(len(r['photos']))]) for r in rows(state.workspace)]
