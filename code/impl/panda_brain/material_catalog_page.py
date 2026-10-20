"""Named material records and real document uploads with explicit review."""
import base64
import json
import secrets
from email.parser import BytesParser
from email.policy import default
from urllib.parse import parse_qs
from . import material_catalog as service
from .workbench import esc

LABELS = dict(item='物资档案', carry='固定随队清单', supply_point='物资点资料', stock='登记库存', company='支援公司资料')
FIELDS = {
    'item': [('name','物资名称'),('category','分类'),('spec','规格'),('unit','单位'),('quantity','默认携带数量')],
    'carry': [('squad','队伍名称'),('item_name','物资名称'),('quantity','清单数量'),('unit','单位')],
    'supply_point': [('name','物资点名称'),('location','位置'),('org','所属单位'),('contact_name','联系人'),('contact_phone','联系电话')],
    'stock': [('point','物资点名称'),('item_name','物资名称'),('quantity','登记数量（未知填“未知”）')],
    'company': [('name','公司名称'),('services','服务/物资'),('region','区域'),('contact_name','联系人'),('contact_phone','联系电话'),('source','资料来源'),('source_updated_at','来源更新时间')],
}
FIELD_LABELS = {kind: dict(fields) for kind, fields in FIELDS.items()}
OPERATIONS = dict(edit='修改', upload='上传预览', draft='生成草稿', publish='明确发布', rollback='回滚', closeout='收尾关联调整')


def _hidden(member, oid, version, operation, data_type=''):
    values = dict(csrf=member.custody_csrf,object_id=oid,expected_version=version,operation=operation,request_id=secrets.token_urlsafe(18))
    if data_type: values['data_type'] = data_type
    return ''.join(f'<input type="hidden" name="{k}" value="{esc(str(v))}">' for k,v in values.items())


def body(state, token):
    actor = state.authorization_subject(token); member = state.member_session(token); spaces = state.supply_event_spaces()
    rows = service.inspect(state.ledger, spaces); allowed = False
    try: state.require_material_maintenance(token); allowed = True
    except PermissionError: pass
    out = ['<p><a href="/materials/accounts">收尾核实与底账关联调整 →</a></p><p>固定清单、物资点与公司资料的长期维护。登记库存不代表已核实实物，公司清单不代表供货承诺；历史到场基线保持原版。</p>']
    out.append('<details><summary>当前长期资料</summary>')
    for kind, title in LABELS.items():
        current = []
        if kind == 'item':
            current = [dict(name=i.name,category=i.category,spec=i.spec,unit=i.unit,quantity=i.default_carry_qty) for i in state.ledger.items.values()]
        elif kind == 'carry':
            current = [dict(squad=state.ledger.squads[c.squad_id].name,item_name=state.ledger.items[c.item_id].name,quantity=c.quantity,unit=state.ledger.items[c.item_id].unit) for c in state.ledger.carry_lines.values()]
        elif kind == 'supply_point':
            current = [{k:getattr(p,k) for k,label in FIELDS[kind]} for p in state.ledger.points.values()]
        elif kind == 'stock':
            current = [dict(point=p.name,item_name=state.ledger.items[item].name,quantity='未知' if quantity is None else quantity) for p in state.ledger.points.values() for item,quantity in p.supplies.items()]
        elif kind == 'company':
            current = [{k:getattr(c,k) for k,label in FIELDS[kind]} for c in state.ledger.companies.values()]
        out.append('<h3>'+esc(title)+'</h3>'+(''.join('<p>'+esc(' · '.join(f'{FIELD_LABELS[kind][k]}：{v}' for k,v in row.items()))+'</p>' for row in current) or '<p>尚无登记资料。</p>'))
    out.append('</details>')
    out.append('<p>'+('本人具名长期维护权有效。' if allowed else '本人未获有效长期维护权，只读。')+'</p>')
    if allowed:
        for kind, fields in FIELDS.items():
            oid = 'edit-'+kind; version = sum(r.reference.object_id==oid for r,v in rows)
            out.append(f'<details><summary>修改{LABELS[kind]}</summary><p>请核对上方现有资料并填写完整一行；表单中的空白文字字段会清空相应资料。物资按名称与规格匹配。</p><form method="post" action="/materials/records">'+_hidden(member,oid,version,'edit',kind))
            for name,label in fields:
                out.append(f'<label>{label}<input class="lz-input" name="{name}" aria-label="{label}"></label>')
            out.append('<label>修改依据<input class="lz-input" name="basis" required aria-label="修改依据"></label><button class="lz-btn">保存具名修改</button></form></details>')
        out.append('<section class="lz-card"><h2>文件导入</h2><p>支持CSV/TXT、Word .docx表格、Excel .xlsx单表、文本PDF；扫描件OCR未接入。解析不会自动发布。</p>'
            '<form method="post" enctype="multipart/form-data" action="/materials/records">'+_hidden(member,'import-'+secrets.token_urlsafe(12),0,'upload')+
            '<label>资料类型<select name="data_type" aria-label="资料类型">'+''.join(f'<option value="{k}">{v}</option>' for k,v in LABELS.items())+'</select></label>'
            '<label>原始文件<input type="file" name="file" accept=".csv,.txt,.docx,.xlsx,.pdf" aria-label="原始文件" required></label>'
            '<label>导入依据<input class="lz-input" name="basis" required aria-label="导入依据"></label><button class="lz-btn">上传并解析预览</button></form></section>')
    latest = {r.reference.object_id:(r,v) for r,v in rows if r.reference.object_id.startswith('import-')}
    for oid,(r,v) in latest.items():
        d = v['details']; out.append(f'<section class="lz-card"><h2>{esc(d["filename"])} · {esc(d["state"])} · v{v["version"]}</h2>'
            f'<p>类型 {esc(LABELS[d["data_type"]])} · 原文件SHA256 {esc(d["content_sha256"])}</p>'
            '<p>字段映射：'+esc('；'.join(f'{c} → {FIELD_LABELS[d['data_type']].get(f,f)}' for f,c in d['mapping'].items()))+'</p>'
            '<p>逐行预览：</p><div style="overflow:auto">'+''.join('<p>'+esc(' · '.join(f'{FIELD_LABELS[d['data_type']].get(k,k)}：{val}' for k,val in row.items()))+'</p>' for row in d['rows'])+'</div>'
            +''.join('<p role="alert">'+esc(e)+'</p>' for e in d['errors']))
        operation = {'预览':'draft','已生成草稿':'publish','已发布':'rollback'}.get(d['state'])
        if allowed and operation:
            out.append('<form method="post" action="/materials/records">'+_hidden(member,oid,v['version'],operation)+
                '<label>处理依据<input class="lz-input" name="basis" required aria-label="处理依据"></label>'
                '<button class="lz-btn">'+dict(draft='确认预览并生成草稿',publish='明确发布到长期库',rollback='按发布前版本回滚')[operation]+'</button></form>')
        out.append('<details><summary>版本对比与修改记录</summary>'+''.join(f'<p>v{q["version"]} · {esc(OPERATIONS[q["operation"]])} · {esc(rec.actor_id)} · {esc(q["basis"])} · {esc(rec.times.recorded_at)}</p><pre style="white-space:pre-wrap;overflow-wrap:anywhere">'+esc(json.dumps(q['changes'],ensure_ascii=False,indent=2))+'</pre>' for rec,q in rows if rec.reference.object_id==oid)+'</details></section>')
    out.append('<details><summary>全部具名资料修改历史</summary>'+''.join(f'<p>{esc(r.actor_id)} · {esc(OPERATIONS[v["operation"]])} · {esc(v["basis"])} · {esc(r.times.recorded_at)}</p>' for r,v in rows)+'</details>')
    return '<div style="overflow-wrap:anywhere;min-width:0">'+''.join(out)+'</div>'


def post(handler, state):
    from .http_rejection import request_length
    from .panel import PanelActionError
    member = state.member_session(handler._member_cookie())
    if member is None: handler._respond(403,'<h1>403</h1>'); return
    try:
        state.require_material_maintenance(handler._member_cookie())
        length = request_length(handler,4*1024*1024); handler.connection.settimeout(15)
        content_type = handler.headers.get('Content-Type',''); raw = handler.rfile.read(length)
        if len(raw) != length: raise ValueError('文件上传不完整。')
        file = None; filename = ''; values = {}
        if content_type.startswith('multipart/form-data;'):
            message = BytesParser(policy=default).parsebytes(('Content-Type: '+content_type+'\r\n\r\n').encode()+raw)
            if not message.is_multipart(): raise ValueError('文件表单边界不正确。')
            for part in message.iter_parts():
                name = part.get_param('name',header='content-disposition')
                if name in values or name == 'file' and file is not None: raise ValueError('字段重复。')
                if name == 'file': file = part.get_payload(decode=True); filename = part.get_filename()
                else: values[name] = part.get_payload(decode=True).decode('utf-8')
        elif content_type.split(';')[0] == 'application/x-www-form-urlencoded':
            form = parse_qs(raw.decode('utf-8'),keep_blank_values=True,errors='strict',max_num_fields=32)
            if any(len(v)!=1 for v in form.values()): raise ValueError('字段重复。')
            values = {k:v[0] for k,v in form.items()}
        else: raise ValueError('表单格式不正确。')
        common = {'csrf','operation','object_id','expected_version','request_id','basis'}
        operation = values.get('operation'); fields = set(values)-common
        if not common <= set(values): raise ValueError('必需字段缺失。')
        if not secrets.compare_digest(values.pop('csrf').encode(),member.custody_csrf.encode()): raise PermissionError('会话变化。')
        if operation == 'edit':
            kind = values.pop('data_type'); allowed = {k for k,l in FIELDS.get(kind,())}
            if fields != allowed|{'data_type'}: raise ValueError('修改字段缺失或越权。')
            details = dict(data_type=kind,row={k:values.pop(k) for k in allowed})
        elif operation == 'upload':
            if fields != {'data_type'} or not file or not filename: raise ValueError('须选择真实原文件。')
            details = dict(data_type=values.pop('data_type'),filename=filename,original_base64=base64.b64encode(file).decode())
        else:
            if fields or file is not None: raise ValueError('字段不受支持。')
            details = {}
        raw_version = values['expected_version']
        if not raw_version.isascii() or not raw_version.isdigit() or len(raw_version)>9: raise ValueError('版本不正确。')
        values['expected_version'] = int(raw_version)
        state.handle_material_catalog(member_token=handler._member_cookie(),details=details,**values)
    except PermissionError: handler._respond(403,'<h1>403</h1>'); return
    except (ValueError,KeyError,TypeError,PanelActionError) as error:
        handler._respond(409,'<h1>资料未保存</h1><p>'+esc(str(error))+'</p><a href="/materials/records">返回资料核对</a>'); return
    handler._redirect('/materials/records')
