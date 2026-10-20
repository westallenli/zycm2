"""Daily directory extensions; group membership always lives in the squad ledger."""
import csv
import io
from copy import deepcopy
from dataclasses import asdict

PEOPLE_HEADERS = ['姓名','手机号','地市','单位','中队','本职岗位','是否队长']
SQUAD_HEADERS = ['中队名称','成员手机号','队长手机号','编制人数','联系说明']


def enabled(ledger, kind, identifier):
    return ledger.resource_metadata.get('directory_status', {}).get(kind+':'+identifier, {}).get('enabled', True)


def metadata_change(ledger, kind, key, value):
    target = ledger.resource_metadata.setdefault(kind, {})
    before = deepcopy(target.get(key)); target[key] = deepcopy(value)
    return dict(table='metadata:'+kind, key=key, before=before, after=deepcopy(value))


def rows(text, headers):
    raw = list(csv.reader(io.StringIO(text.lstrip('\ufeff')), strict=True))
    if not raw or raw[0] != headers: raise ValueError('表头须为：'+','.join(headers))
    if len(raw)>2001: raise ValueError('每次最多导入2000行。')
    result=[]
    for index, row in enumerate(raw[1:], 2):
        if not any(row): continue
        if len(row)!=len(headers): raise ValueError(f'第{index}行列数不正确。')
        result.append(dict(zip(headers, (v.strip() for v in row))))
    if not result: raise ValueError('没有数据行。')
    return result


def csv_text(headers, values):
    out=io.StringIO(); writer=csv.writer(out); writer.writerow(headers); writer.writerows(values)
    return out.getvalue()


def check_squad_edit(state, sid):
    from .callouts import callout_state
    from .resource_maintenance import event_spaces
    for eid, space in event_spaces(state)[0].items():
        event=state.registry.events.get(eid)
        if event and event.status in {'active','upgraded'} and sid in callout_state(space,eid):
            raise ValueError('活动火情已使用该中队编制，结束后再修改或停用。')


def apply_people(state, ledger, registry, text):
    from .resource_maintenance import _people
    data=rows(text,PEOPLE_HEADERS)
    phones=[r['手机号'] for r in data]
    if len(phones)!=len(set(phones)): raise ValueError('人员手机号重复。')
    for r in data:
        if not r['地市'] or not r['单位']: raise ValueError('地市和单位不能为空。')
        if r['中队']=='未编队':r['中队']=''
    legacy=csv_text(['姓名','手机号','单位','本职岗位','是否队长'],
        [[r[k] for k in ['姓名','手机号','单位','本职岗位','是否队长']] for r in data])
    changes=_people(state,registry,legacy)
    targets={r['手机号']:r['中队'] for r in data}
    names=({s.name for s in ledger.squads.values()} | set(targets.values()))-{''}
    for name in sorted(names):
        matches=[s for s in ledger.squads.values() if s.name==name]
        if len(matches)>1: raise ValueError('中队重名，请先核对：'+name)
        old=matches[0] if matches else None
        if old and not set(old.member_phones)&set(phones) and name not in targets.values(): continue
        members=[p for p in (old.member_phones if old else ()) if p not in targets or targets[p]==name]
        members+= [p for p in phones if targets[p]==name and p not in members]
        if not members:
            if old and set(old.member_phones)&set(phones): raise ValueError('不能通过导入移空中队，请先调整编制：'+name)
            continue
        leaders=[p for p in members if registry.library[p].is_leader]
        if len(leaders)!=1: raise ValueError(name+'须有且只有一名队长。')
        cities={r['地市'] for r in data if r['中队']==name}
        cities.update(ledger.resource_metadata.get('people',{}).get(p,{}).get('city') for p in members if p not in targets)
        cities.discard(None)
        units={registry.library[p].unit for p in members}
        if len(cities)>1 or len(units)>1: raise ValueError('同一中队的地市和单位须一致：'+name)
        if old and tuple(members)==old.member_phones and leaders[0]==old.leader_phone: continue
        if old: check_squad_edit(state,old.squad_id)
        new=ledger.add_squad(name=name,member_phones=members,leader_phone=leaders[0],
            headcount_planned=max(len(members),old.headcount_planned if old else 0),
            contact=old.contact if old else '',position_slots=old.position_slots if old else (),squad_id=old.squad_id if old else None,library=registry.library)
        changes.append(dict(table='squads',key=new.squad_id,before=asdict(old) if old else None,after=asdict(new)))
    for r in data:
        changes.append(metadata_change(ledger,'people',r['手机号'],dict(city=r['地市'])))
    return changes


def apply_squads(state,ledger,registry,text):
    changes=[]; seen=set()
    for r in rows(text,SQUAD_HEADERS):
        name=r['中队名称']
        if not name or name in seen: raise ValueError('中队名称为空或重复。')
        seen.add(name)
        matches=[s for s in ledger.squads.values() if s.name==name]
        if len(matches)>1: raise ValueError('中队重名。')
        old=matches[0] if matches else None
        phones=tuple(p.strip() for p in r['成员手机号'].replace('，',',').split(',') if p.strip())
        if not phones or len(phones)!=len(set(phones)): raise ValueError('中队成员为空或重复。')
        for s in ledger.squads.values():
            if s!=old and set(s.member_phones)&set(phones): raise ValueError('成员已属于其他中队：'+s.name)
        count=int(r['编制人数'])
        if count<0 or count>0 and count<len(phones): raise ValueError('编制人数为0表示未填；已填人数不能少于成员人数。')
        new=ledger.add_squad(name=name,member_phones=phones,leader_phone=r['队长手机号'],headcount_planned=count,
            contact=r['联系说明'],position_slots=old.position_slots if old else (),library=registry.library,squad_id=old.squad_id if old else None)
        if not new.leader_phone: raise ValueError('请指定中队队长。')
        if len({registry.library[p].unit for p in phones})>1: raise ValueError('中队成员须属于同一单位。')
        if old and old!=new: check_squad_edit(state,old.squad_id)
        changes.append(dict(table='squads',key=new.squad_id,before=asdict(old) if old else None,after=asdict(new)))
    return changes


def set_status(state,ledger,registry,details,actor):
    if set(details)!={'kind','identifier','enabled'} or type(details['enabled']) is not bool: raise ValueError('停用字段不正确。')
    kind,key=details['kind'],details['identifier']
    tables=dict(people=registry.library,squads=ledger.squads,companies=ledger.companies,points=ledger.points,items=ledger.items,carry=ledger.carry_lines)
    if kind not in tables or key not in tables[kind]: raise ValueError('资料不存在。')
    if kind=='people' and not details['enabled']:
        if key==actor or key in {g.subject_id for g in state._duty_grants}: raise ValueError('不能停用本人或配置中的值班账号。')
        for s in ledger.squads.values():
            if key in s.member_phones: check_squad_edit(state,s.squad_id)
    if kind=='squads': check_squad_edit(state,key)
    return [metadata_change(ledger,'directory_status',kind+':'+key,dict(enabled=details['enabled']))]


def import_data(state,ledger,registry,kind,text,actor):
    if kind=='person_directory': return apply_people(state,ledger,registry,text)
    if kind=='squad_directory': return apply_squads(state,ledger,registry,text)
    from . import material_catalog as catalog
    _, mapped, errors=catalog.preview(kind,text,ledger,actor)
    if errors: raise ValueError('；'.join(errors))
    for row in mapped: catalog._apply(ledger,kind,row,actor)
    return []


def person_rows(state):
    squads={p:s for s in state.ledger.squads.values() for p in s.member_phones}
    result=[]
    for p in state.registry.library.values():
        squad=squads.get(p.phone)
        result.append([p.name,p.phone,state.ledger.resource_metadata.get('people',{}).get(p.phone,{}).get('city','待维护'),
            p.unit,squad.name if squad else '未编队',p.home_position,'是' if p.is_leader else '否'])
    return sorted(result,key=lambda r:(r[2],r[3],r[4],r[6]!='是',r[0]))
