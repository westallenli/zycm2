"""Table-first daily maintenance; all dialogs submit the existing named forms."""
from . import resource_directory as directory, resource_maintenance as maintenance
from .workbench import esc


def button(target,label,*,title=None):
    return '<button type="button" class="lz-btn lz-btn2" data-resource-open="'+esc(target)+'"'+(' aria-label="'+esc(title)+'"' if title else '')+'>'+esc(label)+'</button>'


def dialog(identifier,title,body):
    return '<dialog class="resource-dialog" id="'+identifier+'" aria-labelledby="'+identifier+'-title"><div class="resource-heading"><h2 id="'+identifier+'-title">'+esc(title)+'</h2><button class="resource-close" type="button" data-resource-close aria-label="关闭">×</button></div>'+body+'</dialog>'


def daily(state,token):
    from .resource_pages import STYLE,switch,table,base_form,field,select
    from .resource_directory_page import KINDS,columns,data_rows,import_box,status_form,person_form,squad_form,catalog_form
    from .material_catalog_page import FIELDS
    from .material_catalog import inspect
    rights=maintenance.permissions(state,token);deputy=bool(state.deputy_events(token))
    if not any(rights.values()) and not deputy: raise PermissionError('需要值班员或原维护权限。')
    spaces,missing=maintenance.event_spaces(state);inspect(state.ledger,spaces);maintenance.journal(state.ledger)
    l=state.ledger;dialogs=[]
    out=[STYLE,'<div class="resource-workspace resource-daily">',switch(),'<section class="lz-card resource-heading"><h1>日常数据维护</h1>'+('<small>演示资料</small>' if getattr(state,'resource_demo',None) else '')+'</section>']
    sections=[('person_directory','people','人员库'),('squad_directory','squads','中队编制'),('company','company','外协公司'),('item','item','物资种类'),('carry','carry','中队标配'),('supply_point','supply_point','物资库'),('stock','stock','库存内容')]
    out.append('<nav class="lz-card resource-tabs" aria-label="日常资料分类">'+''.join('<a href="#'+anchor+'">'+name+'</a>' for kind,anchor,name in sections)+'</nav>')
    if missing:out.append('<section class="lz-card">'+table(('资料状态','待核对事件'),[('历史事件空间缺失','、'.join(missing))])+'</section>')
    for kind,anchor,label in sections:
        if kind in {'person_directory','squad_directory'} and not (rights['people'] or deputy):continue
        editable=rights['people'] if kind in {'person_directory','squad_directory'} else rights['materials']
        raw=data_rows(state,kind);values=raw;heads=columns(kind)
        if kind=='person_directory':
            order=[2,3,4,0,1,5,6];heads=[heads[i] for i in order]+['状态']
            values=[[row[i] for i in order]+['在用' if directory.enabled(l,'people',row[1]) else '已停用'] for row in raw]
        elif kind=='squad_directory':
            heads=['中队','编制人数','名册人数','队长','队长电话','联系说明','状态']
            values=[(s.name,s.headcount_planned or '未填',len(s.member_phones),state.registry.library[s.leader_phone].name if s.leader_phone in state.registry.library else '待维护',s.leader_phone,s.contact,'在用' if directory.enabled(l,'squads',s.squad_id) else '已停用') for s in l.squads.values()]
        out.append('<section class="lz-card resource-section" id="'+anchor+'"><div class="resource-heading"><h2>'+label+' <small>'+str(len(raw))+(' 人' if kind=='person_directory' else ' 条')+'</small></h2>')
        if editable:
            add_id='resource-add-'+kind;import_id='resource-import-'+kind
            out.append('<div class="resource-toolbar">'+button(add_id,'新增'+('人员' if kind=='person_directory' else '中队' if kind=='squad_directory' else label))+button(import_id,'导入文件',title='导入'+label)+'<a class="lz-btn lz-btn2" href="/materials/daily/download?kind='+kind+'&amp;mode=template&amp;format=xlsx">下载模板</a></div>')
            form=person_form(state,token,['']*6+['否']) if kind=='person_directory' else squad_form(state,token,['','','','0','']) if kind=='squad_directory' else catalog_form(state,token,kind,['']*len(FIELDS[kind]))
            dialogs.append(dialog(add_id,'新增'+label,form));dialogs.append(dialog(import_id,'导入'+label,import_box(state,token,kind)))
        out.append('</div>')
        actions=[] if editable else None
        objects={'person_directory':state.registry.library,'squad_directory':l.squads,'company':l.companies,'item':l.items,'carry':l.carry_lines,'supply_point':l.points}.get(kind)
        status_kind={'person_directory':'people','squad_directory':'squads','company':'companies','item':'items','carry':'carry','supply_point':'points'}.get(kind)
        if editable:
            for index,row in enumerate(raw):
                key=row[1] if kind=='person_directory' else list(objects)[index] if objects is not None else None
                name=str(row[0]);edit_id='resource-edit-'+kind+'-'+str(index)
                form=person_form(state,token,row) if kind=='person_directory' else squad_form(state,token,row) if kind=='squad_directory' else catalog_form(state,token,kind,row)
                dialogs.append(dialog(edit_id,'修改'+label+' · '+name,form))
                action=button(edit_id,'修改',title='修改'+name)
                if status_kind:
                    active=directory.enabled(l,status_kind,key);word='停用' if active else '恢复';status_id='resource-status-'+kind+'-'+str(index)
                    action+=button(status_id,word,title=word+name);dialogs.append(dialog(status_id,word+name,status_form(state,token,status_kind,key)))
                actions.append('<div class="resource-actions">'+action+'</div>')
        out.append(table(heads,values,actions=actions,scroll=True)+'</section>')
    if rights['materials']:
        out.append('<section class="lz-card"><details><summary>位置、送达时间与预测参数</summary>')
        for kind,label,objects in [('points','物资库',l.points),('companies','外协公司',l.companies)]:
            if objects:out.append(base_form(state,token,'metadata',select('identifier',label,[(k,v.name) for k,v in objects.items()])+field('latitude','纬度')+field('longitude','经度')+field('lead_hours','预计送达小时')+field('parameter_basis','依据',required=True),extra={'metadata_kind':kind}))
        if l.items:out.append(base_form(state,token,'metadata',select('identifier','物资',[(k,v.name) for k,v in l.items.items()])+select('scope','用途',[('standard','队伍标配'),('public','公共保障'),('extra','额外需求')])+field('rate_per_person_hour','每人每小时用量')+field('parameter_basis','依据',required=True),extra={'metadata_kind':'items'}))
        out.append('</details></section>')
    out.append('<section class="lz-card"><details><summary>修改记录 · '+str(len(l.daily_resource_records))+'</summary>'+table(('时间','记录人','依据'),[(r['recorded_at'],state.registry.library[r['actor']].name if r['actor'] in state.registry.library else r['actor'],r['basis']) for r in reversed(l.daily_resource_records)],scroll=True)+'</details></section>')
    return ''.join(out)+''.join(dialogs)+'</div><script src="/static/resource-directory.js" defer></script>'
