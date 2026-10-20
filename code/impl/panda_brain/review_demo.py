"""Append an explicitly fictional review continuation to the existing resource demo.

Never called by normal startup. Original audit entries, roster and material
ledgers stay intact. Build on an isolated copy, then install after verification.
"""
from dataclasses import asdict
from datetime import datetime, timezone
import json

from . import review_meeting as meeting, review_closure as closure, review_causality as causal
from .record_contracts import ObjectRevision, ObjectReference, RecordTimes
from .resource_demo import DEPUTY


def extend(state):
    root = state.registry_path.parent
    marker = root/'review-demo.json'
    if marker.exists():
        return json.loads(marker.read_text(encoding='utf-8'))
    eid = state.active_event_id
    if not getattr(state, 'resource_demo', {}).get('simulated') or eid != state.resource_demo['event_id']:
        raise ValueError('只允许在明确模拟且事件一致的独立演示根追加。')
    if state.workspace.verify_audit_chain():
        raise ValueError('原始审计链异常，停止追加。')
    before = [asdict(e) for e in state.workspace.events]
    registry_before = state.registry_path.read_bytes()
    deputy, error = state.member_login(DEPUTY, '123456')
    if error:
        raise ValueError(error)
    if meeting.owner(state) not in {'', DEPUTY}:
        raise ValueError('已指定其他负责人，不能覆盖。')
    if not meeting.owner(state):
        state.handle_review_owner_assign(DEPUTY, deputy_token=deputy,
            evidence='用户授权新增连贯模拟过程，演示副指挥兼任会议复盘负责人', request_id='review-demo-owner-v1')
    state._pending_review_actor = ('owner', state.registry.library[DEPUTY].name, eid, deputy)
    stamp = datetime.now(timezone.utc).isoformat()
    demand = [r for r in state.workspace.object_revisions.values() if r.reference.object_kind == 'task_material_inputs']
    receipt = next(r for r in state.workspace.object_revisions.values() if r.reference.object_kind == 'formal_supply_receipt')
    refs = tuple(r.id for r in demand) + (receipt.id,)
    raw = ObjectRevision(ObjectReference(eid, 'demo_field_observation', 'review-demo-field', 1),
        RecordTimes(occurred_at=stamp, recorded_at=stamp), 'review-demo-continuation-v1', 'simulated',
        DEPUTY, 'review-demo-field-v1', json.dumps(dict(
            subject='模拟现场保障复核', data_kind='simulated', evidence_refs=list(refs),
            statement='新增模拟演练：沿用原饮用水120瓶实收与当前餐食30份、照明4套缺口；不新增收货、不扣标配。',
            feedback='模拟会议核对：原保障任务已登记饮用水配送，餐食照明另有缺口，需要分项检查并跟踪。'), ensure_ascii=False))
    state.workspace._record_object_revision(raw, expected_version=0)
    from .map_annotations import MapAnnotation, AnnotationKind
    for ident, kind, title, points in [
        ('fire', 'fire_point', '模拟当前火点', ((30.004,110.003),)),
        ('supply', 'material', '模拟东岭保障点', ((30.0,110.0),)),
        ('route', 'route', '模拟饮水配送路线', ((29.998,109.998),(30.0,110.0))),
        ('area', 'area', '模拟作业范围', ((30.002,110.001),(30.006,110.001),(30.006,110.006),(30.002,110.006))),
    ]:
        state.workspace.record_annotation(MapAnnotation('review-demo-'+ident, AnnotationKind(kind), (), title,
            geo_points=points, event_id=eid, is_demo=True, source_ref=(raw.id,),
            created_at=stamp, updated_at=stamp, created_by=DEPUTY,
            note='本轮新增虚构演示位置，非原历史定位或专业核验。',
            demo_notice='模拟追加过程，非真实现场', details={'basis':raw.id}))
    state.persist_if_db()

    # Continue the already-published water task through the ordinary named gates.
    from .formal_task_receipts import published
    from .task_relations import reference
    item = next(iter(published(state.workspace).values()))
    publication, bundle, task = item
    oid = task['order_id']
    recipient, error = state.member_login(task['recipient_id'], '123456')
    if error:
        raise ValueError(error)
    verifier, error = state.member_login(task['verifier_id'], '123456')
    if error:
        raise ValueError(error)
    state.handle_formal_task_receipt(eid, member_token=recipient, order_id=oid,
        publication_ref=publication.id, expected_version=0, action='received',
        basis='新增模拟演练：队长确认接收原饮水保障任务', request_id='review-demo-ack')
    for index, action in enumerate(('conditions','begin')):
        state.handle_task_relations(eid, member_token=recipient, action=action,
            task=reference(item), basis='新增模拟：运输条件核对并开始配送记录复验',
            expected_version=index, request_id='review-demo-'+action)
    for index, action in enumerate(('progress','completed','verify')):
        state.handle_formal_task_feedback(eid, member_token=verifier if action=='verify' else recipient,
            order_id=oid, publication_ref=publication.id, expected_version=index, action=action,
            basis='新增模拟：核对原120瓶饮水实收单及本版任务；仅完成饮水配送，不代表餐食照明缺口解决',
            overall_basis='模拟核验：原饮水实收120瓶、欠收0，与任务范围一致' if action=='verify' else '',
            request_id='review-demo-feedback-'+action)

    note = meeting.record_note(state, deputy, dict(subject='模拟保障会议复核',
        provided_by='模拟物资供应组与队长', feedback='仅核对演示资料：饮水实收已有记录，餐食照明缺口仍需跟踪。',
        evidence_refs=' '.join(refs+(raw.id,)), occurred_at=stamp, request_id='review-demo-meeting'))
    from .review_domain import ReviewIssue
    issue_id = 'ISSUE-'+eid+'-demo-support'
    state.review_ledger.add_issue(ReviewIssue(issue_id, '模拟餐食照明保障缺口', '资源后勤', '现场保障', eid,
        '原任务保障需求与模拟会议核对', '餐食缺30份、照明缺4套；饮水实收不能替代其他物资保障。',
        evidence_ids=refs+(raw.id,)))
    state._save_reviews(); state._mirror_review('ReviewIssuesGenerated', issue_ids=[issue_id])
    hyp_id = state.handle_review_hyp_new('模拟演练：原饮水任务的完成范围未覆盖餐食照明需求',
        evidence=';'.join(refs+(raw.id,)), to_verify='核对原任务范围与各项需求', related_issues=issue_id)
    state.handle_review_hyp_resolve(hyp_id, '核对原任务范围与各项需求',
        '模拟会议逐项核对原饮水任务、3项需求与实收单；仅核实本模拟事项', operator='模拟复盘负责人')
    for kind, target in (('issue',issue_id),('hypothesis',hyp_id)):
        basis = causal.record_assessment(state, deputy, dict(target_kind=kind, target_id=target,
            support_refs=';'.join(refs+(raw.id,)), counter_refs=receipt.id,
            counter_note='原饮水实收证明部分保障完成，不支持将全部保障判为失败。',
            alternatives='也可能需求发生变化；本模拟仅核对明确登记的任务范围，不推断真实责任。',
            missing_info='', verify_targets='模拟物资供应组、原任务与需求记录', meeting_ref=note.id,
            meeting_finding='模拟会议核实本案例范围差异，非真实事故根因结论',
            causal_layer='direct' if kind=='hypothesis' else 'undetermined', expected_object_version='0',
            request_id='review-demo-assessment-'+kind))
        if kind=='issue':
            state.handle_review_issue_status(target,'confirmed',reason='模拟负责人核对原始引用',assessment_ref=basis.id)
        else:
            state.handle_review_hyp_status(target,'verified',reason='模拟负责人核对支持、反证与其他解释',assessment_ref=basis.id)
    # Keep a separate unproven deeper explanation visible as pending.
    state.handle_review_hyp_new('待验证：是否存在跨物资需求交接遗漏（仅模拟假设）',
        evidence=raw.id, to_verify='缺少完整交接原文，不作深层原因认定', related_issues=issue_id)
    rect = closure.create_rect_draft(state,deputy,issue_id,hyp_id)
    responsible = state.registry.library['13900000118']; checker=state.registry.library['13900000117']
    closure.publish_rect(state,deputy,rect.rect_id,
        measures='模拟整改：逐项列出饮水、餐食和照明需求，标明实收与仍缺项目；不得以饮水完成代替全部保障完成',
        owner_unit=responsible.unit,owner_person=responsible.name,deadline='2026-10-31',
        acceptance_standard='模拟清单3项均有原始引用，缺口保留，饮水实收不重复计入',
        verifier_name=checker.name,responsible_phone=responsible.phone,verifier_phone=checker.phone)
    closure.record_rect_feedback(state,deputy,rect.rect_id,provided_by=responsible.name,duty='模拟保障清单维护',
        content='模拟完成：逐项复核3项需求与原饮水实收，当前缺口继续保留。',evidence=';'.join(refs),completed=True)
    closure.record_rect_verification(state,deputy,rect.rect_id,verifier=checker.name,outcome='passed',
        finding='模拟独立核对：清单引用一致；验收的是核对措施，未冒充餐食照明已补齐。',evidence=';'.join(refs+(note.id,)))
    exp=closure.propose_experience(state,deputy,source_kind='rect',source_id=rect.rect_id)
    closure.revise_experience(state,deputy,exp.exp_id,
        content='模拟经验：多品类保障应逐项核对需求、任务与实收，单项完成不等于全部完成。',
        applicability='本模拟事件的多品类保障清单核对',limitations='仅模拟流程案例，不能替代现场核实或专业用量判断',
        condition_tags='保障范围=多品类',evidence=';'.join(refs),reason='明确本模拟经验的来源与限制')
    closure.decide_experience(state,deputy,exp.exp_id,target='confirmed',reason='模拟负责人核对来源和适用边界')
    rid=state.handle_report_generate(stage=True)
    state.handle_report_edit(rid,state.review_ledger.reports[rid].content_md+'\n\n模拟会议补充：保留深层原因待验证，未完成保障继续跟踪。')
    # The delivered version stays a draft for the user to review; no auto-publication.
    assert before == [asdict(e) for e in state.workspace.events[:len(before)]]
    if json.loads(registry_before) != json.loads(state.registry_path.read_bytes()):
        raise ValueError('追加过程意外改变原名册或账号，停止交付。')
    if state.registry_path.read_bytes() != registry_before:
        state.registry_path.write_bytes(registry_before)
    result=dict(schema='review-demo-continuation-v1',simulated=True,event_id=eid,
        original_audit_count=len(before),audit_count=state.workspace.version,
        original_audit_preserved=True,report_id=rid,issue_id=issue_id,rect_id=rect.rect_id,
        exp_id=exp.exp_id,task_order_id=oid,source_refs=list(refs),created_at=stamp,
        notice='仅本轮新增虚构演练；火情继续活动，报告为阶段草稿，原人员物资数据未改。')
    marker.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    return result
