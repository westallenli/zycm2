"""Real event-role fixtures and form tokens for migrated legacy review tests."""
from panda_brain import review_meeting as meeting
from panda_brain.authorization import Role
from tests.duty_support import duty_token


def owner_token(state, phone='13912340002'):
    token,error=state.member_login(phone,'123456')
    if error: raise AssertionError(error)
    if not state.workspace.event_id: return token
    service=meeting.service(state)
    deputy=token
    if not meeting.is_deputy(state,token):
        from panda_brain.event_authorization import event_grants
        holder=next((g.subject_id for g in event_grants(state.workspace) if g.role==Role.DEPUTY),None)
        if holder:
            deputy,error=state.member_login(holder,'123456')
            if error:raise AssertionError(error)
        else:
            officer=next((g.subject_id for g in state._duty_grants),None)
            duty=duty_token(state,phone=officer)
            service=meeting.service(state)
            service.record_initial_appointment(duty,event_id=state.active_event_id,role=Role.DEPUTY,
                subject_id=phone,evidence='匿名测试副总指定',request_id='review-test-deputy')
    if not meeting.owner(state):
        state.handle_review_owner_assign(phone,deputy_token=deputy,evidence='匿名测试会议负责人',request_id='review-test-owner')
    return token


def submit(state,http,method,url,form=None,cookie=''):
    token=next((part.strip().split('=',1)[1] for part in cookie.split(';') if part.strip().startswith('member_session=')),None)
    if state.workspace.event_id and meeting.is_owner(state,token):
        form={**(form or {}),'event_id':state.active_event_id,'csrf':meeting.csrf(state,token),
              'expected_version':str(state.workspace.version)}
    return http(method,url,form,cookie=cookie)
