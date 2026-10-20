"""Editable province directory and bounded file import/download UI."""
import io
import json
import zipfile
from copy import deepcopy
from html import escape
from urllib.parse import parse_qs
from . import resource_directory as directory
from . import resource_maintenance as maintenance

KINDS=dict(person_directory='全省应急人员',squad_directory='中队编制',company='外协公司',item='物资种类',carry='中队标配',supply_point='物资库',stock='库存内容')


def columns(kind):
    if kind=='person_directory': return directory.PEOPLE_HEADERS
    if kind=='squad_directory': return directory.SQUAD_HEADERS
    return {'item':['物资名称','分类','规格','单位','默认携带数量'],
        'carry':['中队名称','物资名称','数量','单位'],
        'supply_point':['物资点','位置','所属单位','联系人','联系电话'],
        'stock':['物资点','物资名称','数量'],
        'company':['公司名称','服务/物资','服务区域','联系人','联系电话','资料来源','来源更新时间']}[kind]


def data_rows(state,kind):
    l=state.ledger
    if kind=='person_directory': return directory.person_rows(state)
    if kind=='squad_directory': return [[s.name,','.join(s.member_phones),s.leader_phone,s.headcount_planned,s.contact] for s in l.squads.values()]
    if kind=='carry': return [[l.squads[c.squad_id].name,l.items[c.item_id].name,c.quantity,c.unit] for c in l.carry_lines.values()]
    if kind=='stock': return [[p.name,l.items[i].name,'未知' if q is None else q] for p in l.points.values() for i,q in p.supplies.items()]
    objects={'company':l.companies,'item':l.items,'supply_point':l.points}[kind]
    from .material_catalog_page import FIELDS
    return [[getattr(o,'default_carry_qty' if k=='quantity' else k) for k,label in FIELDS[kind]] for o in objects.values()]


def xlsx(headers,rows):
    """Text cells preserve IDs and never execute spreadsheet formulas."""
    from xml.sax.saxutils import escape as xml
    def column(i):
        out=''
        while i: i,r=divmod(i-1,26);out=chr(65+r)+out
        return out
    sheet='<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" state="frozen"/></sheetView></sheetViews><cols><col min="1" max="40" width="24" customWidth="1"/></cols><sheetData>'
    for n,row in enumerate([headers]+rows,1):
        sheet+='<row r="'+str(n)+'">'+''.join('<c r="'+column(i)+str(n)+'" t="inlineStr"><is><t xml:space="preserve">'+xml(str(v))+'</t></is></c>' for i,v in enumerate(row,1))+'</row>'
    sheet+='</sheetData></worksheet>'
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml','<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/></Types>')
        z.writestr('_rels/.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('xl/workbook.xml','<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="资料表" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr('xl/worksheets/sheet1.xml',sheet)
    return buffer.getvalue()


def download(handler,state,query):
    try:
        fields=parse_qs(query,strict_parsing=True)
        if set(fields)!={'kind','mode','format'} or any(len(v)!=1 for v in fields.values()): raise ValueError('下载参数不正确。')
        kind,mode,fmt=(fields[k][0] for k in ('kind','mode','format'))
        if kind not in KINDS or mode not in {'template','current'} or fmt not in {'csv','xlsx'}: raise ValueError('模板类型不正确。')
        maintenance.authority(state,handler._member_cookie(),'people' if kind in {'person_directory','squad_directory'} else 'catalog')
        headers=columns(kind); rows=data_rows(state,kind) if mode=='current' else []
        if fmt=='xlsx': payload=xlsx(headers,rows)
        else:
            # Escape formula-like text for spreadsheet consumers; numbers and phones are unchanged.
            safe=[["'"+str(v) if str(v).startswith(('=','+','-','@')) else v for v in r] for r in rows]
            payload=directory.csv_text(headers,safe).encode('utf-8-sig')
        handler.send_response(200)
        handler.send_header('Content-Type','application/vnd.openxmlformats-officedocument.spreadsheetml.sheet' if fmt=='xlsx' else 'text/csv; charset=utf-8')
        handler.send_header('Content-Disposition','attachment; filename="'+kind+'-'+mode+'.'+fmt+'"')
        handler.send_header('Cache-Control','no-store');handler.send_header('Content-Length',str(len(payload)))
        handler.end_headers();handler.wfile.write(payload)
    except PermissionError: handler._respond(403,'<h1>请以有维护权限的本人账号登录</h1>')
    except (ValueError,KeyError): handler._respond(400,'<h1>下载参数不正确</h1>')


def import_box(state,token,kind):
    from .resource_pages import base_form
    form=base_form(state,token,'directory_import','<label class="resource-wide">选择 Excel 或 CSV 文件<input type="file" name="file" accept=".xlsx,.csv" required></label>',
        extra={'data_type':kind},action='/materials/daily/preview',label='上传并预览')
    form=form.replace('<form method="post"','<form enctype="multipart/form-data" method="post"')
    links=' · '.join('<a href="/materials/daily/download?kind='+kind+'&amp;mode='+mode+'&amp;format='+fmt+'">'+label+'</a>' for mode,fmt,label in [('template','xlsx','空白Excel模板'),('current','xlsx','下载当前Excel（含示例）'),('template','csv','CSV模板'),('current','csv','当前CSV')])
    return '<div class="resource-toolbar">'+links+'</div>'+form


def status_form(state,token,kind,key):
    from .resource_pages import base_form
    active=directory.enabled(state.ledger,kind,key)
    return base_form(state,token,'status','',extra={'kind':kind,'identifier':key,'enabled':'false' if active else 'true'},label='确认停用' if active else '确认恢复')


def daily(state,token):
    from .resource_directory_tables import daily as render
    return render(state,token)


def person_form(state,token,row):
    from .resource_pages import base_form,field
    keys=['name','phone','city','unit','squad','position','leader']
    return base_form(state,token,'person_edit',''.join(field(k,label,v,required=k not in {'position','squad'},readonly=k=='phone' and row[1] in state.registry.library) for k,label,v in zip(keys,directory.PEOPLE_HEADERS,row)),label='预览人员修改',action='/materials/daily/preview')


def squad_form(state,token,row):
    from .resource_pages import base_form,field
    return base_form(state,token,'squad_edit',''.join(field(k,label,v,required=k!='contact',readonly=k=='name' and bool(row[0])) for k,label,v in zip(['name','phones','leader','count','contact'],directory.SQUAD_HEADERS,row)),label='预览中队修改',action='/materials/daily/preview')


def catalog_form(state,token,kind,row):
    from .resource_pages import base_form,field
    from .material_catalog_page import FIELDS
    return base_form(state,token,'catalog',''.join(field(k,label,v,required=k in {'name','source','squad','item_name','point','quantity'},readonly=bool(row[0]) and k in {'name','spec','squad','item_name','point'}) for (k,label),v in zip(FIELDS[kind],row)),extra={'data_type':kind})
