"""Explicit anonymous named-deputy fixtures for legacy workflow HTTP tests.

Only tests import this helper. Runtime legacy routes remain forbidden.
"""
import secrets
from urllib.parse import urlsplit
from panda_brain.authorization import Role
from panda_brain.event_authorization import event_grants
from panda_brain.personnel_management import inspect
from .duty_support import duty_token


def named_actor(state,event_id):
    from panda_brain.panel import PanelActionError
    def open_space():
        return state._appointment_space(event_id)[0]
    try:
        space=open_space()
    except PanelActionError:
        # 现行规则：事件未启动时连"任命副指挥"的写入口都不开放。
        # 夹具按值班员身份先启动事件，再取写入空间。
        officer=next((g.subject_id for g in state._duty_grants),None) or next(iter(state.registry.library))
        event=state.registry.events.get(event_id)
        if event is None or event.status in {'active','upgraded','closed'}:
            raise
        state.handle_event_action(event_id,'start',duty_token=duty_token(state,phone=officer))
        space=open_space()
    holder=next((g.subject_id for g in event_grants(space) if g.role==Role.DEPUTY),None)
    if not holder:
        from panda_brain.models import PersonRecord
        holder='13999990098'
        if holder not in state.registry.library:
            state.registry.import_person_library([PersonRecord(name='匿名夹具副指挥长',phone=holder,unit='模拟指挥部')])
        officer=next((g.subject_id for g in state._duty_grants),None) or next(iter(state.registry.library))
        # 现行规则：副总指挥只能在事件启动后任命。夹具先按值班员身份启动事件，
        # 否则 appointment 会报"事件尚未启动"。
        event=state.registry.events.get(event_id)
        if event is not None and event.status not in {'active','upgraded','closed'}:
            state.handle_event_action(event_id,'start',duty_token=duty_token(state,phone=officer))
            space=state._appointment_space(event_id)[0]
        state.handle_deputy_appointment(event_id,duty_token=duty_token(state,phone=officer),subject_id=holder,
            evidence='匿名夹具明确副指挥长指定',request_id='legacy-fixture-deputy-'+event_id,
            occurred_at='2026-09-01T08:00:00+08:00')
    token,error=state.member_login(holder,'123456')
    if error:raise AssertionError(error)
    return token


def personnel_submit(state,http,method,url,form=None,cookie='',**kwargs):
    route=urlsplit(url).path; parts=route.strip('/').split('/')
    if method!='POST' or not cookie:return http(method,url,form,cookie=cookie,**kwargs)
    event_id=parts[1]
    operation={'add':'add_roster','all-add':'add_roster','assign':'assign_roster'}[parts[-1]]
    details=dict(form or {})
    if operation=='add_roster':
        phones=details.get('phones',list(state.registry.library))
        details={'phones':phones if isinstance(phones,list) else [phones]}
    token=named_actor(state,event_id);space=state._appointment_space(event_id)[0]
    version=sum(r.reference.object_id=='personnel-'+operation for _,r,_ in inspect(space))
    values=dict(csrf=state.member_session(token).custody_csrf,event_id=event_id,operation=operation,
        expected_version=str(version),request_id='legacy-personnel-'+secrets.token_hex(12),basis='匿名具名副指挥长安排',**details)
    # Older helpers do not all enable urlencode(doseq=True); one explicit HTTP
    # operation per selected phone retains their response interface.
    phones=values.pop('phones',None)
    if phones is not None:
        result=None
        for offset,phone in enumerate(phones):
            result=http(method,url.split('/events/')[0]+'/personnel/manage',values|dict(phones=phone,expected_version=str(version+offset),request_id=values['request_id']+'-'+str(offset)),cookie='member_session='+token,**kwargs)
        return result
    return http(method,url.split('/events/')[0]+'/personnel/manage',values,cookie='member_session='+token,**kwargs)
