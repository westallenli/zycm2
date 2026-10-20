"""Replaceable radio input boundary; only authenticated, explicit reports debit.

No HTTP route supplies identity. A trusted integration must bind its identity
result to this exact capture; absent adapters and simulations remain pending.
The deliberately narrow grammar is a software fixture, not general ASR/NLP.
"""
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import json
import re

from . import manual_material_reports
from .record_contracts import explicit_time

CAPTURED='RadioMaterialCaptureReceived'
LINKED='RadioMaterialLossLinked'
PREFIX='对讲结构化报告：'

def _json(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False)

def _text(value,optional=False):
    if not isinstance(value,str) or value!=value.strip() or not optional and not value:
        raise ValueError('对讲来源字段须明确，未知字段留空。')

@dataclass(frozen=True)
class MaterialCapture:
    event_id:str
    source_call_id:str
    source_ref:str
    channel_id:str
    device_id:str
    call_sign:str
    transcript:str
    audio_ref:str
    occurred_at:str
    report_id:str
    expected_version:int=0
    reason:str=''
    collected_at:str=''
    received_at:str=''
    data_kind:str='reported'

    def __post_init__(self):
        for name in ('event_id','source_call_id','source_ref','channel_id','report_id'):
            _text(getattr(self,name))
        for name in ('device_id','call_sign','transcript','audio_ref','reason'):
            _text(getattr(self,name),True)
        if not self.transcript and not self.audio_ref:
            raise ValueError('对讲须保留转写或音频来源。')
        if type(self.expected_version) is not int or self.expected_version<0:
            raise ValueError('对讲更正版本须为非负整数。')
        if self.expected_version and not self.reason:
            raise ValueError('更正须保留理由与同一报告号。')
        if self.data_kind not in {'reported','simulated'}:
            raise ValueError('对讲来源性质不受支持。')
        times=[explicit_time(t) for t in (self.occurred_at,self.collected_at,self.received_at) if t]
        if not self.occurred_at or times!=sorted(times) or times[-1]>datetime.now(timezone.utc):
            raise ValueError('对讲发生/采集/接收时间须有效，不推测缺失时刻。')

    @property
    def digest(self):
        return hashlib.sha256(_json(asdict(self)).encode()).hexdigest()

    @property
    def loss_id(self):
        return 'radio-loss-'+hashlib.sha256(_json([self.event_id,self.report_id]).encode()).hexdigest()

@dataclass(frozen=True)
class AuthenticatedIdentity:
    """Trusted adapter result; exact event/capture/device/channel binding."""
    subject_id:str
    event_id:str
    source_digest:str
    device_id:str
    channel_id:str
    identity_ref:str

    def __post_init__(self):
        for value in asdict(self).values():
            _text(value)

@dataclass(frozen=True)
class CaptureAssessment:
    capture:MaterialCapture
    state:str
    issues:tuple[str,...]
    loss_record_id:str=''

def _statement(capture,ledger):
    # Full-string matching rejects negatives, guesses, mixed statements and
    # quoted fragments. Catalog IDs or unique exact names; no guessed aliases.
    match=re.fullmatch(r'([^，,：:\n]+)[，,：:]\s*([^，,：:\n]+)[，,：:]\s*(消耗|损坏)\s*(0|[1-9]\d*)(\.\d+)?\s*([^\d\s，,。；;]+)[。]?',capture.transcript)
    if not match:
        return None
    group,item,kind,whole,fraction,unit=match.groups()
    squads=[s for s in ledger.squads.values() if group.strip() in {s.squad_id,s.name}]
    items=[i for i in ledger.items.values() if item.strip() in {i.item_id,i.name} and i.unit==unit.rstrip('。')]
    if len(squads)!=1 or len(items)!=1:
        return None
    return dict(squad_id=squads[0].squad_id,item_id=items[0].item_id,
                kind='consumed' if kind=='消耗' else 'damaged',amount=float(whole+(fraction or '')))

def _evidence(capture,link):
    return PREFIX+_json(dict(capture=asdict(capture),identity_ref=link['identity_ref'],
                            subject_id=link['actor_id'],statement=link['statement']))

def _history(workspace,ledger,event_id):
    if workspace.event_id!=event_id or workspace.verify_audit_chain():
        raise ValueError('对讲物资来源与事件审计不一致。')
    captures={}; links={}; mirrors={}
    for event in workspace.events:
        payload=event.payload
        if event.kind==manual_material_reports.REPORTED:
            mirrors[payload.get('flow_record_id')]=event.sequence
        if event.kind==CAPTURED:
            if payload.get('event_id')!=event_id or set(payload)!={'event_id','capture'}:
                raise ValueError('对讲接收来源格式或归属异常。')
            capture=MaterialCapture(**payload['capture'])
            if asdict(capture)!=payload['capture'] or capture.event_id!=event_id or capture.source_call_id in captures:
                raise ValueError('对讲来源重复、改变或跨事件。')
            captures[capture.source_call_id]=capture
        if event.kind==LINKED:
            if set(payload)!={'event_id','source_call_id','source_digest','actor_id','identity_ref','statement','loss_record_id'}:
                raise ValueError('对讲耗损关联字段异常。')
            capture=captures.get(payload['source_call_id']); record=ledger.flow_records.get(payload['loss_record_id'])
            if (capture is None or capture.data_kind!='reported' or record is None
                    or payload['event_id']!=event_id or payload['source_digest']!=capture.digest
                    or payload['source_call_id'] in links or record.reference.event_id!=event_id
                    or record.reference.object_kind!='material_loss' or record.reference.object_id!=capture.loss_id
                    or record.reference.version!=capture.expected_version+1 or record.actor_id!=payload['actor_id']
                    or record.data_kind!='reported' or not payload['identity_ref']
                    or record.times.occurred_at!=capture.occurred_at
                    or record.request_id!='radio-material-'+capture.digest
                    or record.source!=manual_material_reports.SOURCE_PREFIX+_evidence(capture,payload)
                    or payload['loss_record_id'] not in mirrors):
                raise ValueError('对讲原始来源、身份依据与耗损镜像不一致。')
            content=json.loads(record.content_json)
            if {k:content[k] for k in ('squad_id','item_id','kind','amount')}!=payload['statement'] or content['reason']!=capture.reason:
                raise ValueError('对讲耗损内容与冻结关联不一致。')
            links[capture.source_call_id]=payload
    linked={link['loss_record_id'] for link in links.values()}
    for record in ledger.flow_records.values():
        if (record.reference.event_id==event_id and record.source.startswith(manual_material_reports.SOURCE_PREFIX+PREFIX)
                and record.id not in linked):
            raise ValueError('对讲耗损缺少原始接收关联，不回退数量。')
    return captures,links

def history(workspace,ledger,event_id):
    try:
        return _history(workspace,ledger,event_id)
    except (TypeError,KeyError,AttributeError,OverflowError) as error:
        raise ValueError('对讲物资来源格式不完整。') from error

def assessments(workspace,ledger,event_id):
    captures,links=history(workspace,ledger,event_id)
    return tuple(CaptureAssessment(c,'registered_report' if c.source_call_id in links else 'pending_review',
        () if c.source_call_id in links else ('身份或内容尚未通过可信接入核对；未计耗损。',),
        links.get(c.source_call_id,{}).get('loss_record_id','')) for c in captures.values())

def ingest(workspace,ledger,registry,capture,*,identity_resolver=None):
    """Automatic internal ingestion; caller saves the complete candidate batch."""
    if not isinstance(capture,MaterialCapture):
        raise ValueError('需要明确的对讲物资采集契约。')
    captures,links=history(workspace,ledger,capture.event_id)
    prior=captures.get(capture.source_call_id)
    if prior is not None and prior!=capture:
        raise ValueError('原始通话号已用于不同内容，不覆盖旧报告。')
    if capture.source_call_id in links:
        return CaptureAssessment(capture,'registered_report',(),links[capture.source_call_id]['loss_record_id'])
    event=registry.events.get(capture.event_id)
    from .fire_events import ONBOARDABLE_STATUSES
    if event is None or event.status not in ONBOARDABLE_STATUSES:
        raise ValueError('已关闭事件不接受新的对讲耗损。')
    statement=_statement(capture,ledger)
    issues=[]
    if statement is not None:
        from .resource_maintenance import standard_line
        if standard_line(ledger, statement['squad_id'], statement['item_id']):
            issues.append('消防队伍自带标配由外部维护；保留通话来源，本系统不统计耗损或提出补充。')
    if capture.data_kind=='simulated': issues.append('模拟身份不自动扣账。')
    if statement is None: issues.append('队伍、物资、单位或单一消耗/损坏语句有歧义，须人工补录核对。')
    identity=None
    if not issues and identity_resolver is not None:
        try:
            identity=identity_resolver(capture)
        except OSError:
            issues.append('可信身份接入暂不可用，未计耗损。')
    if (not isinstance(identity,AuthenticatedIdentity) or identity.event_id!=capture.event_id
            or identity.source_digest!=capture.digest or not capture.device_id or not capture.call_sign
            or identity.device_id!=capture.device_id or identity.channel_id!=capture.channel_id
            or not identity.identity_ref or not identity.subject_id):
        issues.append('未获得绑定本事件、设备、频道及原文的可信身份依据。')
    if prior is None:
        workspace.append_event(CAPTURED,event_id=capture.event_id,capture=asdict(capture))
    if issues:
        return CaptureAssessment(capture,'pending_review',tuple(issues))
    link=dict(event_id=capture.event_id,source_call_id=capture.source_call_id,source_digest=capture.digest,
              actor_id=identity.subject_id,identity_ref=identity.identity_ref,statement=statement,loss_record_id='')
    try:
        revision=manual_material_reports.report(workspace,ledger,registry,token=identity,
            resolve_subject=lambda proof:proof.subject_id,event_id=capture.event_id,
            loss_id=capture.loss_id,expected_version=capture.expected_version,
            evidence=_evidence(capture,link),occurred_at=capture.occurred_at,
            reason=capture.reason,request_id='radio-material-'+capture.digest,**statement)
    except PermissionError:
        return CaptureAssessment(capture,'pending_review',('可信身份不是该队本事件现任队长，未计耗损。',))
    link['loss_record_id']=revision.id
    workspace.append_event(LINKED,**link)
    return CaptureAssessment(capture,'registered_report',(),revision.id)
