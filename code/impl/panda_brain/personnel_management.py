"""Named deputy's audited personnel library, roster and maintenance arrangements."""
from dataclasses import asdict
from . import audited_records as ar
from .authorization import Action, AuthorizationPolicy
from .event_authorization import event_grants

KIND = 'personnel_management'
SOURCE = 'named-deputy-personnel-management'
OPERATIONS = {'import_library','add_roster','assign_roster','review_maintenance','unlock_account','mark_state','squad'}


def require(space, actor):
    AuthorizationPolicy(event_grants(space)).require(actor,Action.ASSIGN_PERSONNEL,event_id=space.event_id)


def inspect(space):
    rows=ar.records(space,KIND)
    for index,r,v in rows:
        require(ar.prefix(space,index),r.actor_id)
        if r.source!=SOURCE or set(v)!={'operation','details','basis','before','after'} or v['operation'] not in OPERATIONS:
            raise ValueError('具名人员安排来源或字段异常。')
        ar.text(v['basis'])
    return tuple(rows)


def record(space, registry, actor, *, operation, details, basis, expected_version, request_id, ledger=None, spaces=None):
    require(space,actor); inspect(space); ar.text(basis)
    if operation not in OPERATIONS or not isinstance(details,dict): raise ValueError('人员动作越界。')
    old=next((r for _,r,v in ar.records(space,KIND) if r.request_id==request_id),None)
    if old:
        import json
        value=json.loads(old.content_json)
        if value['operation']!=operation or value['details']!=details or value['basis']!=basis:
            raise ValueError('人员请求号已用于不同内容。')
        return ar.append(space,kind=KIND,object_id='personnel-'+operation,source=SOURCE,actor=actor,
            values=value,expected_version=expected_version,request_id=request_id)
    before={}; after={}
    if operation=='squad':
        if ledger is None or set(details)!={'squad_id','name','member_phones','leader_phone','headcount_planned','contact'}:
            raise ValueError('中队安排字段不正确。')
        phones=details['member_phones']; sid=details['squad_id']
        if not isinstance(phones,list) or not phones or len(set(phones))!=len(phones):
            raise ValueError('须明确不重复的中队在库成员。')
        count=details['headcount_planned']
        if type(count) is not int or count<0: raise ValueError('计划人数须为非负整数。')
        if sid and sid not in ledger.squads: raise ValueError('修改的中队不存在。')
        if sid:
            from .callouts import callout_state
            for eid,other in (spaces or {}).items():
                if registry.events[eid].status in {'active','upgraded'} and sid in callout_state(other,eid):
                    old_team=ledger.squads[sid]
                    if tuple(phones)!=old_team.member_phones or details['leader_phone']!=old_team.leader_phone:
                        raise ValueError('活动事件已冻结该中队召集人员与队长，不能用长期资料修改替换；请保留原现场记录。')
        before={sid:asdict(ledger.squads[sid])} if sid else {}
        squad=ledger.add_squad(name=details['name'],member_phones=phones,leader_phone=details['leader_phone'],
            headcount_planned=count,contact=details['contact'],library=registry.library,squad_id=sid or None)
        after={squad.squad_id:asdict(squad)}
    elif operation=='import_library':
        from .workbench import parse_roster_csv
        if set(details)!={'csv_text'}: raise ValueError('人员导入字段不正确。')
        text=details['csv_text']
        lines=text.splitlines()
        if lines and lines[0].lstrip('\ufeff').split(',')[:2]==['姓名','手机号']:
            text='\n'.join(lines[1:])
        people,errors=parse_roster_csv(text)
        if errors or not people: raise ValueError('人员库未导入：'+'；'.join(errors or ['空批次']))
        if len({p.phone for p in people})!=len(people): raise ValueError('导入批次含重复人员，请先核对。')
        from .callouts import callout_state
        active_captains={call.leader_phone for eid,other in (spaces or {}).items()
            if registry.events[eid].status in {'active','upgraded'} for call in callout_state(other,eid).values()}
        if any(p.phone in active_captains and not p.is_leader for p in people):
            raise ValueError('活动事件已冻结队长职责，不能用长期库导入撤换；请保留原任命并核对换任规则。')
        before={p.phone:asdict(registry.library[p.phone]) if p.phone in registry.library else None for p in people}
        registry.import_person_library(people)
        after={p.phone:asdict(registry.library[p.phone]) for p in people}
    elif operation=='add_roster':
        if set(details)!={'phones'} or not isinstance(details['phones'],list) or not details['phones'] or len(set(details['phones']))!=len(details['phones']):
            raise ValueError('须明确选择不重复的在库人员。')
        if any(p not in registry.library for p in details['phones']): raise ValueError('人员不在可信长期库。')
        roster=registry.rosters.get(space.event_id)
        before={p:asdict(roster.entries[p]) if roster and p in roster.entries else None for p in details['phones']}
        for p in details['phones']: registry.add_roster_entry(space.event_id,p,added_by=actor)
        after={p:asdict(registry.rosters[space.event_id].entries[p]) for p in details['phones']}
    elif operation=='mark_state':
        from .personnel_states import mark_person_state
        if set(details)!={'phone','action','order_id','note'}: raise ValueError('人员状态字段不正确。')
        phone=details['phone']; roster=registry.rosters.get(space.event_id)
        if not (roster and phone in roster.entries or any(b.person.phone==phone for b in space.roster_bindings.values())):
            raise ValueError('人员不在本事件名册或签到记录。')
        before={phone:[e.payload for e in space.events if e.kind=='PersonStateMarked' and e.payload.get('phone')==phone]}
        mark_person_state(space,details['action'],phone=phone,operator=actor,order_id=details['order_id'],note=details['note'])
        after={phone:space.events[-1].payload}
    else:
        if set(details)!=({'phone','pos_code'} if operation=='assign_roster' else {'phone','active'} if operation=='review_maintenance' else {'phone'}):
            raise ValueError('人员安排字段不正确。')
        phone=details['phone']
        if phone not in registry.library: raise ValueError('人员不在可信长期库。')
        if operation=='assign_roster':
            roster=registry.rosters.get(space.event_id)
            if not roster or phone not in roster.entries: raise ValueError('先由副指挥长加入本事件名册。')
            before={phone:asdict(roster.entries[phone])}
            entry=registry.assign_roster_position(space.event_id,phone,details['pos_code'])
            after={phone:asdict(entry)}
        elif operation=='review_maintenance':
            if type(details['active']) is not bool: raise ValueError('安排须明确授予或撤销。')
            before={phone:phone in registry.maintenance_phones}
            if details['active']: registry.maintenance_phones.add(phone)
            else: registry.maintenance_phones.discard(phone)
            after={phone:details['active']}
        else:
            # Preserve no password/PIN in audit; only the prior locked state.
            before={phone:registry.credentials.get(phone).locked if phone in registry.credentials else None}
            credential=registry.unlock_account(phone,operator=actor)
            after={phone:credential.locked}
    r=ar.append(space,kind=KIND,object_id='personnel-'+operation,source=SOURCE,actor=actor,
        values=dict(operation=operation,details=details,basis=basis,before=before,after=after),
        expected_version=expected_version,request_id=request_id)
    inspect(space); return r
